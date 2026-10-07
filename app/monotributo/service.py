"""
Lógica de semáforo de monotributo para Monotributo Más Fácil.

DOS controles distintos:

  1. EXCLUSIÓN (365 días corridos):
     ARCA controla que el acumulado de los últimos 365 días no supere el
     tope de categoría K. Si lo supera, excluye del régimen simplificado.

  2. RECATEGORIZACIÓN (semestral, Enero y Julio):
     Julio  → período 1/7 (año ant) – 30/6 (año act)
     Enero  → período 1/1 (año ant) – 31/12 (año ant)
     Muestra el acumulado del período vigente vs tope de categoría actual.
     → Solo informativo, no bloquea emisión.

La fecha que importa es fch_serv_desde (devengamiento), con fallback a cbte_fecha.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import select, func, exists
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

from app.afip.history_models import AfipInvoiceHistory
from app.facturas.models import Factura, EstadoFactura
from app.monotributo import reglas

# ─────────────────────────────────────────────
# Tablas de topes por período
# Fuente: ARCA — se actualiza semestralmente por IPC
# ─────────────────────────────────────────────

# Vigentes desde 01/08/2025 al 31/01/2026
TOPES_AGO_2025 = {
    "A":  Decimal("8992597"),
    "B":  Decimal("13175201"),
    "C":  Decimal("17566935"),
    "D":  Decimal("21824384"),
    "E":  Decimal("25683982"),
    "F":  Decimal("32176855"),
    "G":  Decimal("38508474"),
    "H":  Decimal("58453432"),
    "I":  Decimal("65462413"),
    "J":  Decimal("74925499"),
    "K":  Decimal("90264073"),
}

# Vigentes desde 01/02/2026 al 31/07/2026 (misma escala que el seed de la BD)
TOPES_FEB_2026 = {
    "A":  Decimal("10277988.13"),
    "B":  Decimal("15058447.71"),
    "C":  Decimal("21113696.52"),
    "D":  Decimal("26212853.42"),
    "E":  Decimal("30833964.37"),
    "F":  Decimal("38642048.36"),
    "G":  Decimal("46211109.37"),
    "H":  Decimal("70113407.33"),
    "I":  Decimal("78479211.62"),
    "J":  Decimal("89872640.30"),
    "K":  Decimal("108357084.05"),
}

# Vigentes desde 01/08/2026 — escala ARCA, igual a la cargada en Facturo Más Fácil
TOPES_AGO_2026 = {
    "A":  Decimal("12009410.45"),
    "B":  Decimal("17595182.74"),
    "C":  Decimal("24670494.31"),
    "D":  Decimal("30628651.43"),
    "E":  Decimal("36028231.33"),
    "F":  Decimal("45151659.41"),
    "G":  Decimal("53995798.87"),
    "H":  Decimal("81924660.37"),
    "I":  Decimal("91699761.90"),
    "J":  Decimal("105012519.20"),
    "K":  Decimal("126610838.75"),
}

def _get_topes(fecha_ref=None) -> dict:
    """Retorna la tabla de topes vigente para la fecha dada (hardcoded fallback)."""
    from datetime import date as _date
    from app.fechas import hoy_ar as _hoy_ar
    ref = fecha_ref or _hoy_ar()
    if ref >= _date(2026, 8, 1):
        return TOPES_AGO_2026
    if ref >= _date(2026, 2, 1):
        return TOPES_FEB_2026
    return TOPES_AGO_2025


async def get_topes_db(db, fecha_ref=None) -> dict:
    """
    Lee los topes vigentes desde la BD (TablaCategorias).
    Fallback a hardcoded si no hay datos en BD.
    """
    from datetime import date as _date
    from app.monotributo.models import TablaCategorias
    from app.fechas import hoy_ar as _hoy_ar
    ref = fecha_ref or _hoy_ar()
    try:
        from sqlalchemy import or_
        # Savepoint: si la consulta falla no deja abortada la transacción de
        # quien llama (por ejemplo, un lote en plena emisión).
        async with db.begin_nested():
            result = await db.execute(
                select(TablaCategorias).where(
                    TablaCategorias.activa == True,
                    TablaCategorias.vigente_desde <= ref,
                    or_(
                        TablaCategorias.vigente_hasta == None,
                        TablaCategorias.vigente_hasta >= ref,
                    )
                ).order_by(TablaCategorias.vigente_desde.desc()).limit(1)
            )
            tabla = result.scalar_one_or_none()
        if tabla and tabla.topes:
            return {k: Decimal(str(v)) for k, v in tabla.topes.items()}
    except Exception as e:
        log.warning(f"get_topes_db falló, usando fallback hardcoded: {e}")
    return {k: Decimal(str(v)) for k, v in _get_topes(ref).items()}


# Alias para compatibilidad — usa la tabla vigente hoy
TOPES = _get_topes()

LETRAS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]


def _tope(cat: str, topes: dict | None = None) -> Decimal:
    t = topes or TOPES
    return t.get(cat, t["A"])


def _categoria_para_monto(monto: Decimal, topes: dict | None = None) -> str | None:
    """Categoría que corresponde al monto. None = supera el tope K (exclusión)."""
    return reglas.categoria_corresponde(monto, topes or TOPES)


def _pct(monto: Decimal, tope: Decimal) -> float:
    if not tope:
        return 0.0
    return round(min(float(monto / tope * 100), 100), 1)


def _estado(pct: float) -> str:
    if pct >= 100:
        return "rojo"
    if pct >= 80:
        return "amarillo"
    return "verde"


def _periodo_recategorizacion(ref: date) -> tuple[date, date, str, str]:
    """
    (desde, hasta, etiqueta del período, etiqueta de la próxima recategorización).
    La regla vive en reglas.periodo_recategorizacion (ventanas reales de ARCA).
    """
    desde, hasta, label = reglas.periodo_recategorizacion(ref)
    return desde, hasta, label, reglas.ventana_recategorizacion(ref).etiqueta


def _fmt(v: Decimal) -> str:
    return f"$ {v:,.0f}".replace(",", ".")


# ─────────────────────────────────────────────
# Acumulado dual-source
# ─────────────────────────────────────────────

async def _suma_historia(mono_id: int, db: AsyncSession, desde: date, hasta: date) -> Decimal:
    """Suma de facturas en AfipInvoiceHistory para un período."""
    # Facturas
    r = await db.execute(
        select(func.coalesce(func.sum(AfipInvoiceHistory.imp_total), 0))
        .where(
            AfipInvoiceHistory.mono_id == mono_id,
            AfipInvoiceHistory.cbte_tipo.in_([11, 1, 6]),  # Facturas C/B/A
            func.coalesce(
                AfipInvoiceHistory.fch_serv_desde,
                AfipInvoiceHistory.cbte_fecha
            ) >= desde,
            func.coalesce(
                AfipInvoiceHistory.fch_serv_desde,
                AfipInvoiceHistory.cbte_fecha
            ) <= hasta,
        )
    )
    total = Decimal(str(r.scalar() or 0))

    # Restar notas de crédito
    nc = await db.execute(
        select(func.coalesce(func.sum(AfipInvoiceHistory.imp_total), 0))
        .where(
            AfipInvoiceHistory.mono_id == mono_id,
            AfipInvoiceHistory.cbte_tipo.in_([13, 8]),  # NC C/B
            func.coalesce(
                AfipInvoiceHistory.fch_serv_desde,
                AfipInvoiceHistory.cbte_fecha
            ) >= desde,
            func.coalesce(
                AfipInvoiceHistory.fch_serv_desde,
                AfipInvoiceHistory.cbte_fecha
            ) <= hasta,
        )
    )
    total -= Decimal(str(nc.scalar() or 0))
    return total


async def _suma_sistema(mono_id: int, db: AsyncSession, desde: date, hasta: date) -> Decimal:
    """
    Facturas emitidas por el sistema no presentes en el historial (evita duplicados).
    Deduplicación por (cbte_nro, punto_venta, cbte_tipo) para cubrir múltiples PVs.
    Usa fch_serv_desde con fallback a cbte_fecha (devengamiento).
    """
    r = await db.execute(
        select(func.coalesce(func.sum(Factura.imp_total), 0))
        .where(
            Factura.monotributista_id == mono_id,
            Factura.afip_result == EstadoFactura.aprobada,
            Factura.anulada == False,
            Factura.cbte_tipo.in_([11, 1, 6]),
            func.coalesce(Factura.fch_serv_desde, Factura.cbte_fecha) >= desde,
            func.coalesce(Factura.fch_serv_desde, Factura.cbte_fecha) <= hasta,
            ~exists(
                select(AfipInvoiceHistory.id).where(
                    AfipInvoiceHistory.mono_id    == mono_id,
                    AfipInvoiceHistory.cbte_nro   == Factura.cbte_nro,
                    AfipInvoiceHistory.cbte_tipo  == Factura.cbte_tipo,
                    AfipInvoiceHistory.punto_venta == Factura.punto_venta,
                ).correlate(Factura)
            ),
        )
    )
    return Decimal(str(r.scalar() or 0))


async def acumulado_periodo(mono_id: int, db: AsyncSession, desde: date, hasta: date) -> Decimal:
    hist = await _suma_historia(mono_id, db, desde, hasta)
    sys  = await _suma_sistema(mono_id, db, desde, hasta)
    return hist + sys


# ─────────────────────────────────────────────
# Semáforo principal
# ─────────────────────────────────────────────

async def get_semaforo_mono(
    mono_id: int,
    categoria_actual: str,
    db: AsyncSession,
    fecha_ref: date | None = None,
) -> dict:
    """
    Semáforo completo de un monotributista, con las reglas de Facturo Más Fácil:
      - Exclusión: 365 días corridos contra el tope K de la tabla vigente a la fecha.
      - Recategorización: período según la ventana de ARCA, contra la tabla que
        rige al cierre de ese período.
    """
    from app.fechas import hoy_ar as _hoy_ar
    ref = fecha_ref or _hoy_ar()
    cat = (categoria_actual or "A").upper()

    # ── Control 1: exclusión — 365 días corridos ──
    topes = await get_topes_db(db, ref)
    desde_365 = ref - timedelta(days=365)
    acu_365 = await acumulado_periodo(mono_id, db, desde_365, ref)
    sem_365 = reglas.semaforo(acu_365, cat, topes)
    tope_k = sem_365.tope_k
    estado_365 = reglas.estado_exclusion(sem_365.porcentaje_k)
    disponible_k = max(Decimal("0"), tope_k - acu_365)

    # ── Control 2: recategorización — período de la ventana de ARCA ──
    f_desde, f_hasta, periodo_label, prox_recat = _periodo_recategorizacion(ref)
    topes_per = await get_topes_db(db, f_hasta)
    acu_sem = await acumulado_periodo(mono_id, db, f_desde, f_hasta)
    sem_sem = reglas.semaforo(acu_sem, cat, topes_per)
    tope_cat_per = reglas.limite_categoria(cat, topes_per)
    corresponde = sem_sem.categoria_corresponde          # None = supera K
    sube = corresponde is None or reglas.indice(corresponde) > reglas.indice(cat)
    baja = corresponde is not None and reglas.indice(corresponde) < reglas.indice(cat)

    # Tope de referencia: el de la categoría que corresponde si ya superó la suya
    tope_ref_sem = tope_cat_per
    if sube and corresponde:
        tope_ref_sem = reglas.limite_categoria(corresponde, topes_per)

    meses_rest = reglas.meses_restantes(ref, f_hasta)
    meses_div = meses_rest if meses_rest >= 1 else Decimal("1")

    cat_markers = [
        {
            "letra": letra,
            "pct": min(100, round(float(topes[letra] / tope_k * 100), 1)) if tope_k else 0,
            "activa": letra == cat,
            "tope_fmt": _fmt(topes[letra]),
        }
        for letra in LETRAS[:-1] if letra in topes
    ]

    return {
        # Control 365 — exclusión (estado según % de K: 80 / 85 / 90)
        "acu_365":          float(acu_365),
        "acu_365_fmt":      _fmt(acu_365),
        "tope_k":           float(tope_k),
        "tope_k_fmt":       _fmt(tope_k),
        "tope_cat":         float(sem_365.tope_categoria),
        "tope_cat_fmt":     _fmt(sem_365.tope_categoria),
        "pct_365":          sem_365.porcentaje_k,
        "pct_365_barra":    min(100.0, sem_365.porcentaje_k),
        "pct_cat":          sem_365.porcentaje,
        "estado_365":       estado_365,
        "excluido":         acu_365 > tope_k,
        "disponible_k":     _fmt(disponible_k),
        "disponible_k_mensual": _fmt(disponible_k / meses_div),
        "desde_365":        desde_365.strftime("%-d/%-m/%Y"),
        "hasta_365":        ref.strftime("%-d/%-m/%Y"),
        "hasta_365_iso":    ref.isoformat(),

        # Control de recategorización
        "acu_sem":          float(acu_sem),
        "acu_sem_fmt":      _fmt(acu_sem),
        "tope_sem":         float(tope_ref_sem),
        "tope_sem_fmt":     _fmt(tope_ref_sem),
        "tope_cat_sem_fmt": _fmt(tope_cat_per),
        "pct_sem":          round(float(acu_sem / tope_cat_per * 100), 1) if tope_cat_per else 0.0,
        "pct_sem_barra":    min(100.0, round(float(acu_sem / tope_ref_sem * 100), 1)) if tope_ref_sem else 0.0,
        "estado_sem":       sem_sem.estado,
        "mensaje_sem":      sem_sem.mensaje,
        "periodo_label":    periodo_label,
        "periodo_cerrado":  meses_rest <= 0,
        "prox_recat":       prox_recat,
        "cat_corresponde":  corresponde,
        "cat_siguiente":    corresponde if sube else None,   # compatibilidad
        "sube_categoria":   sube and corresponde is not None,
        "baja_categoria":   baja,
        "excede_k_sem":     corresponde is None,
        "disponible_sem":   _fmt(max(Decimal("0"), tope_ref_sem - acu_sem)),

        # Datos generales
        "categoria":        cat,
        "cat_markers":      cat_markers,
        "estado":           reglas.peor_estado(estado_365, sem_sem.estado),
    }


async def control_emision_mono(
    mono_id: int,
    importe_nuevo,
    db: AsyncSession,
    acumulado_extra: Decimal | None = None,
) -> reglas.ControlExclusion:
    """
    Control previo a emitir: ¿el acumulado de 365 días + este importe llega al
    90 % del tope K? `acumulado_extra` suma lo ya emitido en el lote en curso.
    """
    from app.fechas import hoy_ar as _hoy_ar
    hoy = _hoy_ar()
    topes = await get_topes_db(db, hoy)
    acu = await acumulado_periodo(mono_id, db, hoy - timedelta(days=365), hoy)
    return reglas.verificar_limite_exclusion(
        acu + (acumulado_extra or Decimal("0")), importe_nuevo, topes)


# ============================================================
# Proyección de cierre del período de recategorización
# Copiado de Facturo Más Fácil — adaptado para Monotributo MF
# ============================================================

def _meses_transcurridos(f_desde: date, ref: date) -> Decimal:
    dias = (ref - f_desde).days + 1
    return Decimal(str(dias)) / Decimal("30.4375")


def _meses_restantes_recat(ref: date, f_hasta: date) -> Decimal:
    dias = max((f_hasta - ref).days, 0)
    return Decimal(str(dias)) / Decimal("30.4375")


async def _facturado_ultimos_3_meses(
    mono_id: int, db: AsyncSession, ref: date, f_desde_periodo: date,
) -> tuple[Decimal, Decimal]:
    """
    Suma los últimos 3 meses calendario completos dentro del período,
    para calcular el ritmo mensual. Excluye el mes en curso (incompleto).
    """
    from datetime import timedelta as _td

    inicio_mes_actual = ref.replace(day=1)
    anio, mes = inicio_mes_actual.year, inicio_mes_actual.month
    for _ in range(3):
        mes -= 1
        if mes == 0:
            mes, anio = 12, anio - 1
    ventana_desde = max(f_desde_periodo, date(anio, mes, 1))
    ventana_hasta = inicio_mes_actual - _td(days=1)

    if ventana_hasta < ventana_desde:
        # Período reciente sin meses completos — usar lo acumulado hasta hoy
        total = await acumulado_periodo(mono_id, db, f_desde_periodo, ref)
        dias = Decimal(str((ref - f_desde_periodo).days + 1))
        return total, max(dias / Decimal("30.4375"), Decimal("0.5"))

    total = await acumulado_periodo(mono_id, db, ventana_desde, ventana_hasta)
    meses = Decimal(str(
        (ventana_hasta.year - ventana_desde.year) * 12
        + (ventana_hasta.month - ventana_desde.month) + 1
    ))
    return total, max(meses, Decimal("1"))


async def proyeccion_mono(
    mono_id: int, db: AsyncSession, fecha_ref: date | None = None,
    categoria_actual: str | None = None, sujeto: str = "el cliente",
) -> dict | None:
    """
    Proyección de cierre del período de recategorización vigente.
    Ritmo = facturación de los últimos 3 meses completos. El cálculo es el de
    reglas.calcular_proyeccion (el mismo de Facturo Más Fácil).
    """
    from app.fechas import hoy_ar as _hoy_ar
    ref = fecha_ref or _hoy_ar()
    f_desde, f_hasta, periodo_label, _ = _periodo_recategorizacion(ref)

    # Tabla que va a regir al cierre del período (no la de hoy)
    topes = await get_topes_db(db, f_hasta)
    if not topes:
        return None

    acu_sem = await acumulado_periodo(mono_id, db, f_desde, f_hasta)
    total_3m, meses_3m = await _facturado_ultimos_3_meses(mono_id, db, ref, f_desde)
    ritmo = total_3m / meses_3m if meses_3m > 0 else Decimal("0")

    proy = reglas.calcular_proyeccion(
        acumulado=acu_sem, ritmo_mensual=ritmo, ref=ref, f_hasta=f_hasta,
        categoria=categoria_actual or "A", categorias=topes, sujeto=sujeto,
    )
    proy["periodo_label"] = periodo_label
    return proy
