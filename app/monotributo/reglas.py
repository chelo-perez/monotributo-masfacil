"""
Reglas del régimen de monotributo — módulo PURO.

No toca la base de datos, no importa modelos ni nada del resto de la app:
recibe números y tablas, devuelve resultados. Está pensado para poder ser el
mismo archivo en Facturo Más Fácil y en Monotributo Más Fácil, así una
corrección de reglas impacta en las dos.

Las reglas replican las de Facturo Más Fácil (app/monotributo/categorias.py y
la parte de cálculo de app/monotributo/service.py):

  - Semáforo de cuatro estados (verde / amarillo / naranja / rojo).
  - Exclusión: solo depende del % del tope K (80 / 85 / 90).
  - Recategorización: períodos según las ventanas reales de ARCA
    (cierre el 5 de febrero y el 5 de agosto).
  - Bloqueo de emisión al 90 % del tope K.
  - Proyección de cierre con ritmo de los últimos 3 meses completos.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

ORDEN_CATEGORIAS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]

DIAS_POR_MES = Decimal("30.4375")

MESES_ES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

# Umbrales
UMBRAL_AMARILLO_CATEGORIA = 80      # % del tope de la categoría
UMBRAL_ROJO_K = 80                  # % del tope K en el semáforo general
UMBRAL_EXCLUSION = (80, 85, 90)     # amarillo, naranja, rojo — solo % de K
UMBRAL_BLOQUEO_K = Decimal("0.90")  # no se emite si acumulado + importe llega acá
UMBRAL_AVISO_K = Decimal("0.80")    # aviso previo en la vista de emisión


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v or 0))


# ─────────────────────────────────────────────────────────────
# Categorías
# ─────────────────────────────────────────────────────────────

def limite_categoria(cat: str | None, categorias: dict) -> Decimal:
    return _d(categorias.get((cat or "").upper(), 0))


def tope_k(categorias: dict) -> Decimal:
    return max((_d(v) for v in categorias.values()), default=Decimal("0"))


def categoria_corresponde(acumulado, categorias: dict) -> str | None:
    """Primera categoría cuyo tope alcanza al acumulado. None = supera K (exclusión)."""
    acumulado = _d(acumulado)
    for c in ORDEN_CATEGORIAS:
        if c in categorias and limite_categoria(c, categorias) >= acumulado:
            return c
    return None


def indice(cat: str | None) -> int:
    c = (cat or "").upper()
    return ORDEN_CATEGORIAS.index(c) if c in ORDEN_CATEGORIAS else -1


# ─────────────────────────────────────────────────────────────
# Semáforo
# ─────────────────────────────────────────────────────────────

@dataclass
class EstadoSemaforo:
    estado: str                 # verde | amarillo | naranja | rojo | desconocido
    porcentaje: float           # % del tope de la categoría (sin tope en 100)
    porcentaje_k: float         # % del tope K
    mensaje: str
    acumulado: Decimal
    tope_categoria: Decimal
    tope_k: Decimal
    categoria: str
    categoria_corresponde: str | None   # None = supera K
    baja: bool = False          # corresponde una categoría menor


def semaforo(acumulado, categoria: str, categorias: dict) -> EstadoSemaforo:
    """
    Orden de evaluación (igual que Facturo Más Fácil):
      1. rojo     — categoría K al 80 % de su tope, o cualquier otra al 80 % de K
      2. naranja  — superó el tope de su categoría
      3. amarillo — entre 80 % y 100 % del tope de su categoría
      4. verde    — si corresponde una categoría menor, el % se mide contra esa
    """
    acumulado = _d(acumulado)
    cat = (categoria or "A").upper()
    tope = limite_categoria(cat, categorias)
    tk = tope_k(categorias)
    corresponde = categoria_corresponde(acumulado, categorias)

    if tope == 0:
        return EstadoSemaforo("desconocido", 0, 0, "Categoría no configurada",
                              acumulado, Decimal("0"), tk, cat, corresponde)

    pct_cat = float(acumulado / tope * 100)
    pct_k = float(acumulado / tk * 100) if tk else 0.0

    def r(estado, mensaje, pct=pct_cat, tope_ref=tope, baja=False):
        return EstadoSemaforo(estado, round(pct, 1), round(pct_k, 1), mensaje,
                              acumulado, tope_ref, tk, cat, corresponde, baja)

    if cat == "K" and pct_cat >= UMBRAL_ROJO_K:
        return r("rojo", f"Cerca del límite máximo del monotributo ({pct_cat:.1f}%). "
                         f"Si supera {_fmt(tk)} queda excluido del régimen.")
    if pct_k >= UMBRAL_ROJO_K and cat != "K":
        return r("rojo", f"Cerca del límite máximo del monotributo ({pct_k:.1f}% del tope K). "
                         f"Riesgo de exclusión del régimen simplificado.")
    if acumulado > tope:
        sig = f" Corresponde recategorizar a {corresponde}." if corresponde else \
              " Supera el tope máximo del régimen."
        return r("naranja", f"Superó el tope de la categoría {cat} ({_fmt(tope)}).{sig}")
    if pct_cat >= UMBRAL_AMARILLO_CATEGORIA:
        return r("amarillo", f"Cerca del límite de la categoría {cat} ({pct_cat:.1f}% utilizado). "
                             f"Quedan {_fmt(tope - acumulado)} disponibles.")

    if corresponde and 0 <= indice(corresponde) < indice(cat):
        tope_c = limite_categoria(corresponde, categorias)
        pct_c = float(acumulado / tope_c * 100) if tope_c else 0.0
        return r("verde", f"Correspondería una categoría menor: {corresponde} "
                          f"(facturado {pct_c:.1f}% de su tope).",
                 pct=pct_c, tope_ref=tope_c, baja=True)

    return r("verde", f"Dentro del límite de la categoría {cat} ({pct_cat:.1f}% utilizado). "
                      f"Quedan {_fmt(tope - acumulado)} disponibles.")


def estado_exclusion(pct_k: float) -> str:
    """Estado del bloque de exclusión: depende solo del % del tope K."""
    a, n, rj = UMBRAL_EXCLUSION
    if pct_k >= rj:
        return "rojo"
    if pct_k >= n:
        return "naranja"
    if pct_k >= a:
        return "amarillo"
    return "verde"


def peor_estado(a: str, b: str) -> str:
    orden = {"verde": 0, "amarillo": 1, "naranja": 2, "rojo": 3}
    return a if orden.get(a, 0) >= orden.get(b, 0) else b


# ─────────────────────────────────────────────────────────────
# Bloqueo de emisión
# ─────────────────────────────────────────────────────────────

@dataclass
class ControlExclusion:
    bloquear: bool
    aviso: bool                 # ≥ 80 % de K: se avisa pero se puede emitir
    porcentaje_k: float
    mensaje: str


def verificar_limite_exclusion(acumulado_actual, importe_nuevo, categorias: dict) -> ControlExclusion:
    """Bloquea si acumulado de 365 días + importe llega al 90 % del tope K."""
    tk = tope_k(categorias)
    if tk <= 0:
        return ControlExclusion(False, False, 0.0, "")
    proyectado = _d(acumulado_actual) + _d(importe_nuevo)
    pct = float(proyectado / tk * 100)

    if proyectado >= tk:
        return ControlExclusion(True, True, round(pct, 1), (
            f"El acumulado de 12 meses con esta factura ({_fmt(proyectado)}) supera el límite "
            f"máximo del monotributo ({_fmt(tk)}). Emitirla implicaría la exclusión del régimen."))
    if proyectado >= tk * UMBRAL_BLOQUEO_K:
        return ControlExclusion(True, True, round(pct, 1), (
            f"El acumulado de 12 meses con esta factura ({_fmt(proyectado)}) supera el 90% del "
            f"límite máximo del monotributo ({_fmt(tk)}). Solo quedarían {_fmt(tk - proyectado)} "
            f"disponibles."))
    if proyectado >= tk * UMBRAL_AVISO_K:
        return ControlExclusion(False, True, round(pct, 1), (
            f"Con esta emisión el acumulado de 12 meses llega al {pct:.1f}% del tope máximo "
            f"del monotributo."))
    return ControlExclusion(False, False, round(pct, 1), "")


# ─────────────────────────────────────────────────────────────
# Períodos y ventanas de recategorización
# ─────────────────────────────────────────────────────────────

def periodo_recategorizacion(ref: date) -> tuple[date, date, str]:
    """
    Período de 12 meses que ARCA evalúa en la recategorización vigente o próxima.

      1 ene – 5 feb   → ventana de FEBRERO abierta → Ene–Dic del año anterior
      6 feb – 5 ago   → ventana de AGOSTO          → Jul (año ant.) – Jun (año act.)
      6 ago – 31 dic  → próxima es FEBRERO         → Ene–Dic del año en curso

    La ventana cierra el día 5, no el último día del mes anterior: del 1 al 5 de
    agosto la recategorización de agosto sigue abierta y el período sigue siendo
    Jul–Jun.
    """
    anio, m, d = ref.year, ref.month, ref.day
    if (m == 2 and d >= 6) or (3 <= m <= 7) or (m == 8 and d <= 5):
        return date(anio - 1, 7, 1), date(anio, 6, 30), f"Jul {anio - 1} – Jun {anio}"
    if m == 1 or (m == 2 and d <= 5):
        return date(anio - 1, 1, 1), date(anio - 1, 12, 31), f"Ene – Dic {anio - 1}"
    return date(anio, 1, 1), date(anio, 12, 31), f"Ene – Dic {anio}"


@dataclass
class Ventana:
    nombre: str            # "agosto" | "febrero"
    cierre: date           # último día para recategorizar (día 5)
    vigencia: date         # desde cuándo rige la nueva categoría (día 1)
    dias_para_cierre: int
    en_ventana: bool       # faltan 40 días o menos
    etiqueta: str          # "Agosto 2026"


def ventana_recategorizacion(ref: date) -> Ventana:
    """Ventana que corresponde al período devuelto por periodo_recategorizacion."""
    desde, hasta, _ = periodo_recategorizacion(ref)
    if desde.month == 7:                       # Jul–Jun → agosto del año de cierre
        cierre, vigencia, nombre = date(hasta.year, 8, 5), date(hasta.year, 8, 1), "agosto"
    else:                                      # Ene–Dic → febrero del año siguiente
        cierre, vigencia, nombre = date(hasta.year + 1, 2, 5), date(hasta.year + 1, 2, 1), "febrero"
    dias = (cierre - ref).days
    return Ventana(nombre, cierre, vigencia, dias, 0 <= dias <= 40,
                   f"{nombre.capitalize()} {cierre.year}")


def meses_transcurridos(f_desde: date, ref: date) -> Decimal:
    return Decimal(str((ref - f_desde).days + 1)) / DIAS_POR_MES


def meses_restantes(ref: date, f_hasta: date) -> Decimal:
    return Decimal(str(max((f_hasta - ref).days, 0))) / DIAS_POR_MES


def ventana_ritmo(ref: date, f_desde_periodo: date) -> tuple[date, date, Decimal] | None:
    """
    Ventana para calcular el ritmo: los últimos 3 meses calendario COMPLETOS,
    acotados al inicio del período. El mes en curso se excluye por incompleto.
    Devuelve (desde, hasta, cantidad_de_meses) o None si todavía no hay ningún
    mes completo dentro del período.
    """
    inicio_mes_actual = ref.replace(day=1)
    anio, mes = inicio_mes_actual.year, inicio_mes_actual.month
    for _ in range(3):
        mes -= 1
        if mes == 0:
            mes, anio = 12, anio - 1
    desde = max(f_desde_periodo, date(anio, mes, 1))
    hasta = inicio_mes_actual - timedelta(days=1)
    if hasta < desde:
        return None
    meses = Decimal(str((hasta.year - desde.year) * 12 + (hasta.month - desde.month) + 1))
    return desde, hasta, max(meses, Decimal("1"))


# ─────────────────────────────────────────────────────────────
# Proyección de cierre
# ─────────────────────────────────────────────────────────────

def _fmt(v) -> str:
    return ("$" + format(_d(v), ",.0f")).replace(",", ".")


def calcular_proyeccion(
    *, acumulado, ritmo_mensual, ref: date, f_hasta: date,
    categoria: str, categorias: dict, sujeto: str = "tu",
) -> dict:
    """
    Proyección de cierre del período de recategorización.

    sujeto: "tu" (habla el dueño, Facturo Más Fácil) o un nombre/“el cliente”
    (habla el contador, Monotributo Más Fácil). Solo cambia la redacción.
    """
    acu = _d(acumulado)
    ritmo = _d(ritmo_mensual)
    cat = (categoria or "A").upper()
    rest = meses_restantes(ref, f_hasta)

    proyeccion = acu + ritmo * rest
    # Lo ya facturado es el piso: con muchas notas de crédito recientes el ritmo
    # puede dar negativo y proyectar por debajo de lo que ya se facturó.
    if proyeccion < acu:
        proyeccion = acu

    tope_cat = limite_categoria(cat, categorias)
    tk = tope_k(categorias)
    cat_proy = categoria_corresponde(proyeccion, categorias)
    sem = semaforo(proyeccion, cat, categorias)

    margen_total = max(tope_cat - acu, Decimal("0"))
    margen_mensual = margen_total / rest if rest > 0 else margen_total

    mes_cruce = None
    if ritmo > 0 and acu < tope_cat < proyeccion:
        dias = int((tope_cat - acu) / ritmo * DIAS_POR_MES)
        fecha_cruce = ref + timedelta(days=dias)
        if fecha_cruce <= f_hasta:
            mes_cruce = f"{MESES_ES[fecha_cruce.month]} {fecha_cruce.year}"

    supera_actual = proyeccion > tope_cat
    supera_k = proyeccion > tk

    # Categoría más baja todavía alcanzable: lo ya facturado no se puede postergar.
    cat_minima = categoria_corresponde(acu, categorias) or cat
    ya_supero = acu > tope_cat
    cat_objetivo = cat_minima if ya_supero else cat
    tope_objetivo = limite_categoria(cat_objetivo, categorias)

    exceso = proyeccion - tope_objetivo if (not supera_k and proyeccion > tope_objetivo) else None

    return {
        "categoria": cat,
        "acu_sem": float(acu), "acu_sem_fmt": _fmt(acu),
        "ritmo_mensual": float(ritmo), "ritmo_mensual_fmt": _fmt(ritmo),
        "meses_restantes": round(float(rest), 1),
        "periodo_cerrado": rest <= 0,
        "proyeccion": float(proyeccion), "proyeccion_fmt": _fmt(proyeccion),
        "categoria_proyectada": cat_proy,
        "estado_proyectado": sem.estado,
        "supera_categoria": supera_actual,
        "baja_categoria": bool(cat_proy and 0 <= indice(cat_proy) < indice(cat)),
        "supera_k": supera_k,
        "margen_total": float(margen_total), "margen_total_fmt": _fmt(margen_total),
        "margen_mensual": float(margen_mensual), "margen_mensual_fmt": _fmt(margen_mensual),
        "mes_cruce": mes_cruce,
        "exceso_a_postergar": float(exceso) if exceso is not None else None,
        "exceso_a_postergar_fmt": _fmt(exceso) if exceso is not None else None,
        "categoria_objetivo": cat_objetivo,
        "categoria_minima": cat_minima,
        "ya_supero_categoria": ya_supero,
        "tope_objetivo_fmt": _fmt(tope_objetivo),
        "tope_categoria_fmt": _fmt(tope_cat),
        "tope_k": float(tk), "tope_k_fmt": _fmt(tk),
        "mensaje": mensaje_proyeccion(cat, cat_proy, proyeccion, tope_cat, tk,
                                      mes_cruce, supera_k, sujeto),
    }


def mensaje_proyeccion(cat_actual, cat_proy, proyeccion, tope_cat, tk,
                       mes_cruce, supera_k, sujeto: str = "tu") -> str:
    propio = sujeto == "tu"
    quien = "A tu ritmo actual proyectás" if propio else f"Al ritmo actual, {sujeto} proyecta"
    if supera_k:
        return (f"{quien} {_fmt(proyeccion)} al cierre del período, por encima del tope "
                f"máximo del monotributo ({_fmt(tk)}). Implicaría la exclusión del régimen.")
    if cat_proy and cat_proy != cat_actual and proyeccion > tope_cat:
        cruce = f" El tope se cruzaría cerca de {mes_cruce}." if mes_cruce else ""
        return (f"{quien} {_fmt(proyeccion)} al cierre del período. "
                f"Correspondería recategorizar de {cat_actual} a {cat_proy}.{cruce}")
    if cat_proy and 0 <= indice(cat_proy) < indice(cat_actual):
        return (f"{quien} {_fmt(proyeccion)} al cierre del período, por debajo de la "
                f"categoría {cat_actual}: podría recategorizar a {cat_proy} y pagar menos.")
    return (f"{quien} {_fmt(proyeccion)} al cierre del período, "
            f"dentro de la categoría {cat_actual}.")


def debe_alertar_proyeccion(proy: dict | None) -> tuple[bool, str]:
    """(alertar, motivo) — motivo: "exclusion" | "suba_categoria" | ""."""
    if not proy:
        return False, ""
    tk = _d(proy.get("tope_k"))
    if tk > 0 and _d(proy.get("proyeccion")) / tk >= UMBRAL_BLOQUEO_K:
        return True, "exclusion"
    if proy.get("supera_categoria") and proy.get("categoria_proyectada") not in (None, proy.get("categoria")):
        return True, "suba_categoria"
    return False, ""
