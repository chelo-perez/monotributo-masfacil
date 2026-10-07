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
    hoy = hoy_ar()
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

class LoteNoDisponible(Exception):
    """El lote ya fue tomado por otra emisión (doble clic, dos pestañas) o ya se emitió."""


# Avance de los lotes en emisión, en memoria (el servicio corre con un solo
# proceso). Lo lee GET /lotes/{id}/progreso para la barra de avance.
PROGRESO: dict[int, dict] = {}


def _avanzar(lote_id: int, **cambios) -> None:
    p = PROGRESO.get(lote_id)
    if p is None:
        return
    for k, v in cambios.items():
        if k in ("procesadas", "aprobadas", "rechazadas"):
            p[k] += v
        else:
            p[k] = v


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
        token, sign = await wsfe_module.get_token_sign(
            cert_pem, key_pem,
            environment=monotributista.afip_environment or "production",
        )
    except Exception as e:
        resultado.error_general = f"Error de autenticación ARCA: {e}"
        return resultado

    # Cache de condición IVA por CUIT receptor (una consulta al padrón por lote)
    _cond_iva_cache: dict[str, Optional[int]] = {}

    # Control de exclusión: acumulado de 365 días al empezar + lo que se va
    # emitiendo en este lote. Si el control no está disponible se emite igual
    # (mismo criterio que Facturo Más Fácil) y queda registrado en el log.
    _acu_base = None
    _topes_ctrl = None
    _emitido_lote = Decimal("0")
    try:
        from datetime import timedelta as _td
        from app.monotributo.service import acumulado_periodo, get_topes_db
        from app.monotributo import reglas as _reglas
        _hoy = hoy_ar()
        _topes_ctrl = await get_topes_db(db, _hoy)
        _acu_base = await acumulado_periodo(monotributista.id, db, _hoy - _td(days=365), _hoy)
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(f"[emision] control de tope no disponible: {_e}")
        _acu_base = None

    # Emitir secuencialmente
    for fila in filas:
        _avanzar(fila.lote_id, actual=fila.cliente_raw)

        if _acu_base is not None and _topes_ctrl:
            _ctrl = _reglas.verificar_limite_exclusion(
                _acu_base + _emitido_lote, fila.importe_resuelto or 0, _topes_ctrl)
            if _ctrl.bloquear:
                _res_b = ResultadoFactura(
                    fila_id=fila.id, cliente_nombre=fila.cliente_raw,
                    importe=fila.importe_resuelto or Decimal("0"),
                    error=("Bloqueada por el control de exclusión. " + _ctrl.mensaje +
                           " Si igual corresponde emitirla, hacelo desde Factura manual."),
                )
                resultado.rechazadas += 1
                resultado.facturas.append(_res_b)
                fila.valida = False
                fila.error = _res_b.error
                await db.commit()
                _avanzar(fila.lote_id, procesadas=1, rechazadas=1)
                continue  # no consume numeración: nunca se llamó a ARCA
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
                environment=monotributista.afip_environment or "production",
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
            import re as _re
            _dni_raw = _re.sub(r"\D", "", fila.dni_cliente_raw or "")
            _sin_identificar = not (len(_dni_raw) in (7, 8, 11) and int(_dni_raw) > 0)
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
                _avanzar(fila.lote_id, procesadas=1, rechazadas=1)
                continue  # no consume numeración: nunca se llamó a ARCA

            # ── Cond. IVA del receptor con CUIT (padrón ARCA, RG 5616) ──
            _cond_iva = None
            if len(_dni_raw) == 11 and _dni_raw.isdigit():
                if _dni_raw in _cond_iva_cache:
                    _cond_iva = _cond_iva_cache[_dni_raw]
                else:
                    try:
                        from ..afip.padron import consultar_padron_plataforma
                        _cons = await asyncio.wait_for(
                            consultar_padron_plataforma(_dni_raw, db), timeout=10)
                        if _cons is not None and not _cons.error:
                            # Monotributista → 6. Sociedad no monotributista → 1 (RI).
                            # Persona física no monotributista: el padrón no alcanza para
                            # distinguir RI de no inscripto → consumidor final (5).
                            if _cons.es_monotributo:
                                _cond_iva = 6
                            elif (_cons.tipo_persona or "").upper().startswith("JUR"):
                                _cond_iva = 1
                            else:
                                _cond_iva = None
                            _cond_iva_cache[_dni_raw] = _cond_iva
                        else:
                            _cond_iva_cache[_dni_raw] = None
                    except Exception:
                        _cond_iva = None  # fallback: 5 (CF) en el WSFE
                        _cond_iva_cache[_dni_raw] = None

            # Documento del receptor: CUIT (80) si tiene 11 dígitos, DNI (96)
            # si tiene 7-8, sin identificar (99) en cualquier otro caso.
            if _dni_raw.isdigit() and len(_dni_raw) == 11:
                _doc_tipo, _doc_nro = 80, _dni_raw
            elif _dni_raw.isdigit() and len(_dni_raw) in (7, 8):
                _doc_tipo, _doc_nro = 96, _dni_raw
            else:
                _doc_tipo, _doc_nro = 99, "0"

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
                concepto=2,  # Servicios (igual que factura manual): admite fecha hasta 10 días atrás
                fch_serv_desde=fecha_cbte.replace(day=1),
                fch_serv_hasta=_fch_hasta,
                doc_tipo=_doc_tipo,
                doc_nro=_doc_nro,
                environment=monotributista.afip_environment or "production",
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
                    fch_serv_hasta=_fch_hasta,
                    imp_total=fila.importe_resuelto,
                    concepto=fila.concepto_raw,
                    cae=cae,
                    cae_vto=cae_vto,
                    afip_result=EstadoFactura.aprobada,
                )
                db.add(factura)
                # Se guarda factura por factura: si la conexión se corta a mitad
                # del lote, lo que ARCA ya autorizó queda registrado.
                await db.commit()

                res_factura.cae = cae
                res_factura.aprobada = True
                resultado.aprobadas += 1
                _emitido_lote += Decimal(str(fila.importe_resuelto or 0))
                _avanzar(fila.lote_id, procesadas=1, aprobadas=1)

            else:
                # ARCA rechazó — detener este CUIT
                res_factura.error = obs or "ARCA rechazó la factura sin observaciones"
                resultado.rechazadas += 1
                resultado.facturas.append(res_factura)

                # Actualizar FilaExcel
                fila.valida = False
                fila.error = res_factura.error
                await db.commit()
                _avanzar(fila.lote_id, procesadas=1, rechazadas=1)
                break  # <-- preserva correlativo, igual que en Facturo Más Fácil

        except Exception as e:
            res_factura.error = f"Error técnico: {e}"
            resultado.rechazadas += 1
            resultado.facturas.append(res_factura)
            fila.valida = False
            fila.error = res_factura.error[:500]
            try:
                await db.commit()
            except Exception:
                await db.rollback()
            _avanzar(fila.lote_id, procesadas=1, rechazadas=1)
            import logging as _log
            _log.getLogger(__name__).error(
                f"[emision] Fila {fila.id} ({fila.cliente_raw}): {e}", exc_info=True)
            break  # también detenemos en errores técnicos

        resultado.facturas.append(res_factura)

    await db.commit()
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

    # Tomar el lote de forma atómica: solo una emisión puede pasarlo de
    # "borrador" a "emitiendo". Un doble clic o una segunda pestaña no entra.
    tomado = await db.execute(
        update(LoteEmision)
        .where(
            LoteEmision.id == lote_id,
            LoteEmision.tenant_id == tenant_id,
            LoteEmision.estado == EstadoLote.borrador,
        )
        .values(estado=EstadoLote.emitiendo, emitido_at=datetime.utcnow())
    )
    if not tomado.rowcount:
        await db.rollback()
        raise LoteNoDisponible(
            "Este lote ya se está emitiendo o ya fue emitido. "
            "Revisá Facturas emitidas antes de volver a intentar.")
    await db.commit()

    # Obtener filas válidas del lote, agrupadas por monotributista
    result = await db.execute(
        select(FilaExcel).where(
            FilaExcel.lote_id == lote_id,
            FilaExcel.valida == True,
            FilaExcel.monotributista_id.is_not(None),
            # Nunca reemitir una fila que ya tiene factura aprobada
            ~FilaExcel.id.in_(
                select(Factura.fila_excel_id).where(
                    Factura.fila_excel_id.is_not(None),
                    Factura.afip_result == EstadoFactura.aprobada,
                )
            ),
            # Orden cronológico: ARCA rechaza una fecha anterior a la del último comprobante
        ).order_by(FilaExcel.monotributista_id, FilaExcel.fecha_resuelta, FilaExcel.fila_numero)
    )
    filas = result.scalars().all()

    PROGRESO[lote_id] = {"tenant_id": tenant_id, "total": len(filas), "procesadas": 0,
                         "aprobadas": 0, "rechazadas": 0, "actual": "", "terminado": False}
    # Limpieza: conservar solo los últimos lotes
    for _viejo in list(PROGRESO.keys())[:-20]:
        PROGRESO.pop(_viejo, None)

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

    _avanzar(lote_id, terminado=True, actual="")
    duracion = (datetime.now() - inicio).total_seconds()

    return ResultadoLote(
        lote_id=lote_id,
        total_aprobadas=total_aprobadas,
        total_rechazadas=total_rechazadas,
        por_monotributista=resultados,
        duracion_segundos=duracion,
    )
