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

from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.afip.history_models import AfipInvoiceHistory
from app.facturas.models import Factura, EstadoFactura

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

# Vigentes desde 01/02/2026 — tabla actual (RG ARCA 5/2026)
TOPES_FEB_2026 = {
    "A":  Decimal("10277988"),
    "B":  Decimal("15058448"),
    "C":  Decimal("20078547"),
    "D":  Decimal("25098147"),
    "E":  Decimal("30117746"),
    "F":  Decimal("36641455"),
    "G":  Decimal("43974109"),
    "H":  Decimal("52366038"),
    "I":  Decimal("61577243"),
    "J":  Decimal("71898540"),
    "K":  Decimal("108357084"),
}

# Vigentes desde 01/08/2026 (estimados +16.8% — confirmar cuando ARCA publique)
# Fuente: La Nacion 14/07/2026, Infobae 14/07/2026
TOPES_AGO_2026 = {
    "A":  Decimal("12004690"),
    "B":  Decimal("17588267"),
    "C":  Decimal("23451783"),
    "D":  Decimal("29314699"),
    "E":  Decimal("35177616"),
    "F":  Decimal("42797400"),
    "G":  Decimal("51381771"),
    "H":  Decimal("61203692"),
    "I":  Decimal("71942530"),
    "J":  Decimal("83978295"),
    "K":  Decimal("126561074"),
}

def _get_topes(fecha_ref=None) -> dict:
    """Retorna la tabla de topes vigente para la fecha dada (hardcoded fallback)."""
    from datetime import date as _date
    ref = fecha_ref or _date.today()
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
    ref = fecha_ref or _date.today()
    try:
        from sqlalchemy import and_, or_
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
    except Exception:
        pass
    return {k: Decimal(str(v)) for k, v in _get_topes(ref).items()}


# Alias para compatibilidad — usa la tabla vigente hoy
TOPES = _get_topes()

LETRAS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]


def _tope(cat: str, topes: dict | None = None) -> Decimal:
    t = topes or TOPES
    return t.get(cat, t["A"])


def _categoria_para_monto(monto: Decimal, topes: dict | None = None) -> str:
    t = topes or TOPES
    for letra in LETRAS:
        if monto <= t[letra]:
            return letra
    return "K"


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
    Retorna (desde, hasta, label_periodo, label_prox_recat).

    ARCA recategoriza en enero y julio evaluando el período que YA CERRÓ:

      Recategorización Enero → evalúa Ene–Dic del año anterior
      Recategorización Julio → evalúa Jul(año ant)–Jun(año act)

    La fecha_ref es la fecha a la que queremos calcular. Lo que importa
    es cuál fue el ÚLTIMO período cerrado antes de esa fecha:

      Si ref <= 30/06 → el último período cerrado es Ene–Dic del año anterior
                        (recategorizado en enero de este año)
      Si ref >= 01/07 → el último período cerrado es Jul(año ant)–Jun(año act)
                        (se recategoriza en julio de este año)
    """
    anio = ref.year

    # ARCA tiene dos ventanas de recategorización al año:
    #
    #   Julio (evalúa Jul año-ant – Jun año-act):
    #     Si ref >= 01/07 → ya cerró Jun, se puede evaluar ese período
    #     Si ref == 30/06 → es el cierre exacto del período Jul-Jun
    #
    #   Enero (evalúa Ene–Dic año-ant):
    #     Si ref >= 01/01 y < 01/07 → ya cerró Dic del año anterior
    #
    # Usamos >= 01/07 como corte para el período Jul-Jun del año.

    if ref.month >= 7:
        # Período Jul(año ant) – Jun(año act) → recategoriza enero siguiente
        desde = date(anio - 1, 7, 1)
        hasta = date(anio, 6, 30)
        label = f"Jul {anio - 1} – Jun {anio}"
        prox  = f"Enero {anio + 1}"
    else:
        # Período Ene–Dic(año ant) → recategoriza julio de este año
        desde = date(anio - 1, 1, 1)
        hasta = date(anio - 1, 12, 31)
        label = f"Ene – Dic {anio - 1}"
        prox  = f"Julio {anio}"
    return desde, hasta, label, prox


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
    from sqlalchemy import exists as sa_exists
    r = await db.execute(
        select(func.coalesce(func.sum(Factura.imp_total), 0))
        .where(
            Factura.monotributista_id == mono_id,
            Factura.afip_result == EstadoFactura.aprobada,
            Factura.anulada == False,
            Factura.cbte_tipo.in_([11, 1, 6]),
            func.coalesce(Factura.fch_serv_desde, Factura.cbte_fecha) >= desde,
            func.coalesce(Factura.fch_serv_desde, Factura.cbte_fecha) <= hasta,
            ~sa_exists().where(
                AfipInvoiceHistory.mono_id    == mono_id,
                AfipInvoiceHistory.cbte_nro   == Factura.cbte_nro,
                AfipInvoiceHistory.cbte_tipo  == Factura.cbte_tipo,
                AfipInvoiceHistory.punto_venta == Factura.punto_venta,
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
    Calcula el estado completo del semáforo para un monotributista.
    Retorna un dict listo para pasar al template.
    """
    ref = fecha_ref or date.today()
    topes = await get_topes_db(db, ref)

    # ── Control 1: Exclusión — 365 días corridos ──
    desde_365 = ref - timedelta(days=365)
    acu_365   = await acumulado_periodo(mono_id, db, desde_365, ref)
    tope_k    = topes["K"]
    tope_cat  = _tope(categoria_actual, topes)
    pct_365   = _pct(acu_365, tope_k)          # respecto a K (exclusión)
    pct_cat   = _pct(acu_365, tope_cat)         # respecto a su categoría
    estado_365 = _estado(pct_365)

    # ── Control 2: Recategorización — período semestral ──
    f_desde, f_hasta, periodo_label, prox_recat = _periodo_recategorizacion(ref)
    acu_sem   = await acumulado_periodo(mono_id, db, f_desde, f_hasta)

    # Tope de referencia semestral: si ya superó la categoría actual, usar la siguiente
    tope_sem = tope_cat
    cat_siguiente = None
    if acu_sem > tope_cat:
        cat_siguiente = _categoria_para_monto(acu_sem, topes)
        tope_sem = _tope(cat_siguiente, topes)

    pct_sem    = _pct(acu_sem, tope_sem)
    estado_sem = _estado(pct_sem)

    # Marcadores de categorías para la barra visual
    cat_markers = [
        {
            "letra": letra,
            "pct": round(float(topes[letra] / tope_k * 100), 1),
            "activa": letra == categoria_actual,
            "tope_fmt": _fmt(topes[letra]),
        }
        for letra in LETRAS[:-1]  # excluir K (es el 100%)
    ]

    return {
        # Control 365 — exclusión
        "acu_365":          float(acu_365),
        "acu_365_fmt":      _fmt(acu_365),
        "tope_k":           float(tope_k),
        "tope_k_fmt":       _fmt(tope_k),
        "tope_cat":         float(tope_cat),
        "tope_cat_fmt":     _fmt(tope_cat),
        "pct_365":          pct_365,
        "pct_cat":          pct_cat,
        "estado_365":       estado_365,
        "disponible_k":     _fmt(max(Decimal("0"), tope_k - acu_365)),
        "desde_365":        desde_365.strftime("%-d/%-m/%Y"),
        "hasta_365":        ref.strftime("%-d/%-m/%Y"),

        # Control semestral — recategorización
        "acu_sem":          float(acu_sem),
        "acu_sem_fmt":      _fmt(acu_sem),
        "tope_sem":         float(tope_sem),
        "tope_sem_fmt":     _fmt(tope_sem),
        "pct_sem":          pct_sem,
        "estado_sem":       estado_sem,
        "periodo_label":    periodo_label,
        "prox_recat":       prox_recat,
        "cat_siguiente":    cat_siguiente,
        "disponible_sem":   _fmt(max(Decimal("0"), tope_sem - acu_sem)),

        # Datos generales
        "categoria":        categoria_actual,
        "cat_markers":      cat_markers,
    }
