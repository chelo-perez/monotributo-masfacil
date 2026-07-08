"""
Emisión en paralelo para Monotributo Más Fácil.

La diferencia clave con Facturo Más Fácil:
- Aquí un lote contiene facturas de MÚLTIPLES monotributistas.
- Usamos asyncio.gather para emitir todos los CUITs en paralelo.
- Cada CUIT se emite de forma secuencial internamente (preserva correlativo).
- Si un CUIT falla con error ARCA, se detiene solo ese CUIT (no afecta a los demás).
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Monotributista, Certificado
from app.facturas.models import (
    Factura, FilaExcel, LoteEmision, EstadoFactura, EstadoLote
)


# ---------------------------------------------------------------------------
# Resultado de emisión
# ---------------------------------------------------------------------------

@dataclass
class ResultadoFactura:
    fila_id: int
    cliente_nombre: str
    importe: Decimal
    cae: Optional[str] = None
    error: Optional[str] = None
    aprobada: bool = False


@dataclass
class ResultadoMonotributista:
    monotributista_id: int
    razon_social: str
    cuit: str
    aprobadas: int = 0
    rechazadas: int = 0
    facturas: list[ResultadoFactura] = field(default_factory=list)
    error_general: Optional[str] = None  # error de credenciales, etc.


@dataclass
class ResultadoLote:
    lote_id: int
    total_aprobadas: int
    total_rechazadas: int
    por_monotributista: list[ResultadoMonotributista]
    duracion_segundos: float = 0.0


# ---------------------------------------------------------------------------
# Emisión de un CUIT (secuencial internamente)
# ---------------------------------------------------------------------------

async def _emitir_cuit(
    monotributista: Monotributista,
    filas: list[FilaExcel],
    db: AsyncSession,
    wsfe_module,  # se inyecta para facilitar testing
    fernet_key: bytes,
) -> ResultadoMonotributista:
    """
    Emite todas las facturas de un monotributista de forma secuencial.
    Si ARCA rechaza una, se detiene (preserva el correlativo numérico).
    """
    resultado = ResultadoMonotributista(
        monotributista_id=monotributista.id,
        razon_social=monotributista.razon_social,
        cuit=monotributista.cuit,
    )

    # Cargar certificado
    try:
        cert_pem, key_pem = wsfe_module.load_credentials(monotributista, fernet_key)
    except Exception as e:
        resultado.error_general = f"Error al cargar credenciales: {e}"
        return resultado

    # Obtener ticket de acceso ARCA
    try:
        token, sign = await wsfe_module.get_token_sign(cert_pem, key_pem)
    except Exception as e:
        resultado.error_general = f"Error de autenticación ARCA: {e}"
        return resultado

    # Emitir secuencialmente
    for fila in filas:
        res_factura = ResultadoFactura(
            fila_id=fila.id,
            cliente_nombre=fila.cliente_raw,
            importe=fila.importe_resuelto or Decimal("0"),
        )

        try:
            # Último comprobante autorizado para este punto de venta
            ultimo = await wsfe_module.get_ultimo_cbte(
                token, sign, monotributista.cuit,
                monotributista.afip_punto_venta, cbte_tipo=11,
                environment=monotributista.afip_environment,
            )
            nuevo_nro = (ultimo or 0) + 1

            fecha_cbte = fila.fecha_resuelta or date.today()

            # Llamada a FECAESolicitar
            cae, cae_vto, obs = await wsfe_module.solicitar_cae(
                token=token,
                sign=sign,
                cuit=monotributista.cuit,
                punto_venta=monotributista.afip_punto_venta,
                cbte_tipo=11,
                cbte_nro=nuevo_nro,
                cbte_fecha=fecha_cbte,
                imp_total=float(fila.importe_resuelto),
                concepto=1,  # Productos=1, Servicios=2, P+S=3
                fch_serv_desde=fecha_cbte.replace(day=1),
                fch_serv_hasta=fecha_cbte,
                cliente_nombre=fila.cliente_raw,
                cliente_dni=fila.dni_cliente_raw,
                environment=monotributista.afip_environment,
            )

            if cae:
                # Guardar factura aprobada
                factura = Factura(
                    tenant_id=monotributista.tenant_id,
                    lote_id=fila.lote_id,
                    monotributista_id=monotributista.id,
                    cliente_id=fila.cliente_id,
                    fila_excel_id=fila.id,
                    cbte_tipo=11,
                    cbte_nro=nuevo_nro,
                    punto_venta=monotributista.afip_punto_venta,
                    cbte_fecha=fecha_cbte,
                    fch_serv_desde=fecha_cbte.replace(day=1),
                    fch_serv_hasta=fecha_cbte,
                    imp_total=fila.importe_resuelto,
                    concepto=fila.concepto_raw,
                    cae=cae,
                    cae_vto=cae_vto,
                    afip_result=EstadoFactura.aprobada,
                )
                db.add(factura)

                res_factura.cae = cae
                res_factura.aprobada = True
                resultado.aprobadas += 1

            else:
                # ARCA rechazó — detener este CUIT
                res_factura.error = obs or "ARCA rechazó la factura sin observaciones"
                resultado.rechazadas += 1
                resultado.facturas.append(res_factura)

                # Actualizar FilaExcel
                fila.valida = False
                fila.error = res_factura.error
                break  # <-- preserva correlativo, igual que en Facturo Más Fácil

        except Exception as e:
            res_factura.error = f"Error técnico: {e}"
            resultado.rechazadas += 1
            resultado.facturas.append(res_factura)
            break  # también detenemos en errores técnicos

        resultado.facturas.append(res_factura)

    await db.flush()
    return resultado


# ---------------------------------------------------------------------------
# Emisión del lote completo (paralela entre CUITs)
# ---------------------------------------------------------------------------

async def emitir_lote(
    lote_id: int,
    tenant_id: int,
    db: AsyncSession,
    wsfe_module,
    fernet_key: bytes,
) -> ResultadoLote:
    """
    Emite todas las facturas del lote en paralelo, un task por monotributista.

    asyncio.gather() permite que los N CUITs tramiten su ticket ARCA
    y esperen respuesta de forma concurrente. El promedio de tiempo
    pasa de N×T segundos a ~T segundos (tiempo del más lento).
    """
    inicio = datetime.now()

    # Actualizar estado del lote
    await db.execute(
        update(LoteEmision)
        .where(LoteEmision.id == lote_id)
        .values(estado=EstadoLote.emitiendo, emitido_at=datetime.utcnow())
    )
    await db.flush()

    # Obtener filas válidas del lote, agrupadas por monotributista
    result = await db.execute(
        select(FilaExcel).where(
            FilaExcel.lote_id == lote_id,
            FilaExcel.valida == True,
            FilaExcel.monotributista_id.is_not(None),
        ).order_by(FilaExcel.monotributista_id, FilaExcel.fila_numero)
    )
    filas = result.scalars().all()

    # Agrupar por monotributista
    por_mono: dict[int, list[FilaExcel]] = {}
    for fila in filas:
        por_mono.setdefault(fila.monotributista_id, []).append(fila)

    # Cargar monotributistas
    mono_ids = list(por_mono.keys())
    result = await db.execute(
        select(Monotributista).where(Monotributista.id.in_(mono_ids))
    )
    monos = {m.id: m for m in result.scalars().all()}

    # Lanzar todos en paralelo
    tasks = [
        _emitir_cuit(
            monotributista=monos[mono_id],
            filas=filas_del_mono,
            db=db,
            wsfe_module=wsfe_module,
            fernet_key=fernet_key,
        )
        for mono_id, filas_del_mono in por_mono.items()
        if mono_id in monos
    ]

    resultados: list[ResultadoMonotributista] = await asyncio.gather(*tasks)

    # Consolidar
    total_aprobadas = sum(r.aprobadas for r in resultados)
    total_rechazadas = sum(r.rechazadas for r in resultados)

    # Actualizar lote
    await db.execute(
        update(LoteEmision)
        .where(LoteEmision.id == lote_id)
        .values(
            estado=EstadoLote.completado,
            aprobadas=total_aprobadas,
            rechazadas=total_rechazadas,
        )
    )
    await db.commit()

    duracion = (datetime.now() - inicio).total_seconds()

    return ResultadoLote(
        lote_id=lote_id,
        total_aprobadas=total_aprobadas,
        total_rechazadas=total_rechazadas,
        por_monotributista=resultados,
        duracion_segundos=duracion,
    )
