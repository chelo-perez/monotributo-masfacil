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
# Tablas vigentes de categorías (julio 2025)
# ─────────────────────────────────────────────
TOPES = {
    "A":  Decimal("3700000"),
    "B":  Decimal("5550000"),
    "C":  Decimal("7400000"),
    "D":  Decimal("9250000"),
    "E":  Decimal("11100000"),
    "F":  Decimal("13500000"),
    "G":  Decimal("16200000"),
    "H":  Decimal("19300000"),
    "I":  Decimal("22700000"),
    "J":  Decimal("26500000"),
    "K":  Decimal("30000000"),
}

LETRAS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]


def _tope(cat: str) -> Decimal:
    return TOPES.get(cat, TOPES["A"])


def _categoria_para_monto(monto: Decimal) -> str:
    for letra in LETRAS:
        if monto <= TOPES[letra]:
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

    Si estamos en Ene–Jun → recategorización en Julio
        período: Jul(año ant) – Jun(año act)
    Si estamos en Jul–Dic → recategorización en Enero
        período: Ene(año act) – Dic(año act)
    """
    anio = ref.year
    if ref.month <= 6:
        desde = date(anio - 1, 7, 1)
        hasta = date(anio, 6, 30)
        label = f"Jul {anio - 1} – Jun {anio}"
        prox  = f"Julio {anio}"
    else:
        desde = date(anio, 1, 1)
        hasta = date(anio, 12, 31)
        label = f"Ene – Dic {anio}"
        prox  = f"Enero {anio + 1}"
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
    """Facturas emitidas por el sistema no presentes en el historial (evita duplicados)."""
    nros_hist = select(AfipInvoiceHistory.cbte_nro).where(
        AfipInvoiceHistory.mono_id == mono_id
    )
    r = await db.execute(
        select(func.coalesce(func.sum(Factura.imp_total), 0))
        .where(
            Factura.monotributista_id == mono_id,
            Factura.afip_result == EstadoFactura.aprobada,
            Factura.anulada == False,
            Factura.cbte_tipo.in_([11, 1, 6]),
            func.coalesce(Factura.fch_serv_desde, Factura.cbte_fecha) >= desde,
            func.coalesce(Factura.fch_serv_desde, Factura.cbte_fecha) <= hasta,
            ~Factura.cbte_nro.in_(nros_hist),
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

    # ── Control 1: Exclusión — 365 días corridos ──
    desde_365 = ref - timedelta(days=365)
    acu_365   = await acumulado_periodo(mono_id, db, desde_365, ref)
    tope_k    = TOPES["K"]
    tope_cat  = _tope(categoria_actual)
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
        cat_siguiente = _categoria_para_monto(acu_sem)
        tope_sem = _tope(cat_siguiente)

    pct_sem    = _pct(acu_sem, tope_sem)
    estado_sem = _estado(pct_sem)

    # Marcadores de categorías para la barra visual
    cat_markers = [
        {
            "letra": letra,
            "pct": round(float(TOPES[letra] / tope_k * 100), 1),
            "activa": letra == categoria_actual,
            "tope_fmt": _fmt(TOPES[letra]),
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
