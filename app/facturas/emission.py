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
from ..fechas import hoy_ar
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Monotributista, Certificado
from app.facturas.models import (
    Factura, FilaExcel, LoteEmision, EstadoFactura, EstadoLote
)


ARCA_MAX_DIAS_ATRAS = 10


def _resolver_fecha_cbte(fecha_pago: date, ultima_fecha_cbte: date | None = None) -> date:
    """
    Determina la fecha del comprobante según la fecha del pago.
    ARCA permite fechas hasta 10 días hacia atrás.
    - Si el pago está dentro del rango: usar la fecha real
    - Si es más antiguo: usar hoy - 10 días (mínimo permitido)
    - Nunca retroceder antes del último comprobante emitido
    """
    from datetime import timedelta
    hoy = date.today()
    min_valida = hoy - timedelta(days=ARCA_MAX_DIAS_ATRAS)

    if fecha_pago < min_valida:
        cbte_fecha = min_valida
    else:
        cbte_fecha = fecha_pago

    # Respetar secuencia: no retroceder antes del último comprobante
    if ultima_fecha_cbte and cbte_fecha < ultima_fecha_cbte:
        cbte_fecha = ultima_fecha_cbte

    if cbte_fecha > hoy:
        cbte_fecha = hoy

    return cbte_fecha


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

    # Cache de condición IVA por CUIT receptor (una consulta al padrón por lote)
    _cond_iva_cache: dict[str, int] = {}

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

            _fecha_pago = fila.fecha_resuelta or hoy_ar()
            fecha_cbte = _resolver_fecha_cbte(_fecha_pago)
            import calendar as _cal
            _ult = _cal.monthrange(fecha_cbte.year, fecha_cbte.month)[1]
            _fch_hasta = fecha_cbte.replace(day=_ult)

            # ── RG 5700/2025: umbral de identificación del receptor ──
            # Se valida antes de llamar a ARCA para no quemar el intento.
            from ..config import UMBRAL_CF
            _dni_raw = (fila.dni_cliente_raw or "").replace("-", "").replace(" ", "")
            _sin_identificar = not (_dni_raw.isdigit() and int(_dni_raw or "0") > 0)
            if (_sin_identificar and UMBRAL_CF
                    and float(fila.importe_resuelto) >= UMBRAL_CF):
                res_factura.error = (
                    f"El importe alcanza el umbral de identificación del receptor "
                    f"(RG 5700/2025, ${UMBRAL_CF:,.0f}). Cargá el DNI o CUIT del "
                    f"cliente en el Excel y reintentá."
                )
                resultado.rechazadas += 1
                resultado.facturas.append(res_factura)
                fila.valida = False
                fila.error = res_factura.error
                continue  # no consume numeración: nunca se llamó a ARCA

            # ── Cond. IVA del receptor con CUIT (padrón ARCA, RG 5616) ──
            _cond_iva = None
            if len(_dni_raw) == 11 and _dni_raw.isdigit():
                if _dni_raw in _cond_iva_cache:
                    _cond_iva = _cond_iva_cache[_dni_raw]
                else:
                    try:
                        from ..afip.padron import consultar_constancia
                        _cons = await consultar_constancia(
                            _dni_raw, monotributista.cuit,
                            cert_pem, key_pem,
                            environment=monotributista.afip_environment,
                        )
                        if not _cons.error:
                            _cond_iva = 6 if _cons.es_monotributo else 1
                            _cond_iva_cache[_dni_raw] = _cond_iva
                    except Exception:
                        _cond_iva = None  # fallback: 5 (CF) en el WSFE

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
                fch_serv_hasta=_fch_hasta,
                cliente_nombre=fila.cliente_raw,
                cliente_dni=fila.dni_cliente_raw,
                environment=monotributista.afip_environment,
                cond_iva_receptor=_cond_iva,
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

    # Emitir secuencialmente por monotributista
    # (asyncio.gather con la misma sesión DB causa errores en SQLAlchemy async)
    resultados: list[ResultadoMonotributista] = []
    for mono_id, filas_del_mono in por_mono.items():
        if mono_id not in monos:
            continue
        resultado_mono = await _emitir_cuit(
            monotributista=monos[mono_id],
            filas=filas_del_mono,
            db=db,
            wsfe_module=wsfe_module,
            fernet_key=fernet_key,
        )
        resultados.append(resultado_mono)

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
