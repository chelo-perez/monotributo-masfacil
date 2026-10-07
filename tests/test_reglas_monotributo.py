"""
Pruebas de las reglas de monotributo (módulo puro, sin base de datos).
Fijan el comportamiento que tiene que coincidir con Facturo Más Fácil.

Correr:  python -m pytest tests/ -q
"""
from datetime import date
from decimal import Decimal as D

import pytest

from app.monotributo import reglas as r

# Escala ARCA vigente desde 01/08/2026
T = {k: D(str(v)) for k, v in {
    "A": 12009410.45, "B": 17595182.74, "C": 24670494.31, "D": 30628651.43,
    "E": 36028231.33, "F": 45151659.41, "G": 53995798.87, "H": 81924660.37,
    "I": 91699761.90, "J": 105012519.20, "K": 126610838.75}.items()}
K = T["K"]


# ── Categoría que corresponde ──
def test_categoria_corresponde():
    assert r.categoria_corresponde(D("0"), T) == "A"
    assert r.categoria_corresponde(T["A"], T) == "A"                 # el tope incluye
    assert r.categoria_corresponde(T["A"] + D("0.01"), T) == "B"
    assert r.categoria_corresponde(T["J"] + 1, T) == "K"
    assert r.categoria_corresponde(K + 1, T) is None                 # exclusión, no "K"


# ── Semáforo de cuatro estados ──
@pytest.mark.parametrize("pct,estado", [(50, "verde"), (79.9, "verde"), (80, "amarillo"),
                                        (100, "amarillo"), (100.1, "naranja")])
def test_semaforo_por_porcentaje_de_la_categoria(pct, estado):
    s = r.semaforo(T["B"] * D(str(pct)) / 100, "B", T)
    assert s.estado == estado


def test_semaforo_rojo_al_80_de_k_en_cualquier_categoria():
    assert r.semaforo(K * D("0.80"), "H", T).estado == "rojo"
    assert r.semaforo(K * D("0.79"), "J", T).estado != "rojo"


def test_semaforo_categoria_k():
    assert r.semaforo(K * D("0.80"), "K", T).estado == "rojo"
    assert r.semaforo(K * D("0.50"), "K", T).baja is True            # correspondería una menor


def test_semaforo_no_topea_el_porcentaje():
    assert r.semaforo(T["A"] * 2, "A", T).porcentaje == 200.0


def test_semaforo_baja_de_categoria():
    s = r.semaforo(T["A"] / 2, "D", T)
    assert s.estado == "verde" and s.baja and s.categoria_corresponde == "A"


# ── Exclusión: solo % de K ──
@pytest.mark.parametrize("pct,estado", [(79.9, "verde"), (80, "amarillo"), (85, "naranja"),
                                        (90, "rojo"), (120, "rojo")])
def test_estado_exclusion(pct, estado):
    assert r.estado_exclusion(pct) == estado


# ── Bloqueo de emisión al 90 % de K ──
def test_bloqueo_emision():
    assert r.verificar_limite_exclusion(K * D("0.70"), D("1000"), T).bloquear is False
    aviso = r.verificar_limite_exclusion(K * D("0.80"), D("1"), T)
    assert aviso.bloquear is False and aviso.aviso is True
    justo = r.verificar_limite_exclusion(K * D("0.90") - 100, D("100"), T)
    assert justo.bloquear is True                                    # llega exacto al 90 %
    assert r.verificar_limite_exclusion(K * D("0.90") - 100, D("99"), T).bloquear is False
    assert r.verificar_limite_exclusion(K, D("1"), T).bloquear is True


def test_bloqueo_sin_tabla_no_bloquea():
    assert r.verificar_limite_exclusion(D("999999999"), D("1"), {}).bloquear is False


# ── Períodos según ventanas de ARCA ──
@pytest.mark.parametrize("ref,desde,hasta", [
    (date(2026, 1, 1),  date(2025, 1, 1), date(2025, 12, 31)),   # ventana de febrero abierta
    (date(2026, 2, 5),  date(2025, 1, 1), date(2025, 12, 31)),   # último día de la ventana
    (date(2026, 2, 6),  date(2025, 7, 1), date(2026, 6, 30)),    # pasa a la de agosto
    (date(2026, 6, 30), date(2025, 7, 1), date(2026, 6, 30)),
    (date(2026, 7, 15), date(2025, 7, 1), date(2026, 6, 30)),
    (date(2026, 8, 5),  date(2025, 7, 1), date(2026, 6, 30)),    # del 1 al 5 de agosto sigue abierta
    (date(2026, 8, 6),  date(2026, 1, 1), date(2026, 12, 31)),   # período en curso
    (date(2026, 10, 7), date(2026, 1, 1), date(2026, 12, 31)),
    (date(2026, 12, 31), date(2026, 1, 1), date(2026, 12, 31)),
])
def test_periodo_recategorizacion(ref, desde, hasta):
    d, h, _ = r.periodo_recategorizacion(ref)
    assert (d, h) == (desde, hasta)


def test_ventana_coincide_con_el_periodo():
    v = r.ventana_recategorizacion(date(2026, 10, 7))
    assert (v.nombre, v.cierre, v.vigencia) == ("febrero", date(2027, 2, 5), date(2027, 2, 1))
    assert v.en_ventana is False
    v = r.ventana_recategorizacion(date(2026, 7, 20))
    assert (v.nombre, v.cierre) == ("agosto", date(2026, 8, 5)) and v.en_ventana is True
    v = r.ventana_recategorizacion(date(2026, 8, 3))                # ventana aún abierta
    assert v.nombre == "agosto" and v.dias_para_cierre == 2
    v = r.ventana_recategorizacion(date(2027, 1, 20))
    assert (v.nombre, v.cierre) == ("febrero", date(2027, 2, 5)) and v.en_ventana is True


# ── Ventana del ritmo: 3 meses completos, sin el mes en curso ──
def test_ventana_ritmo():
    assert r.ventana_ritmo(date(2026, 10, 7), date(2026, 1, 1)) == (date(2026, 7, 1), date(2026, 9, 30), D("3"))
    # acotada al inicio del período
    assert r.ventana_ritmo(date(2026, 2, 20), date(2026, 1, 1)) == (date(2026, 1, 1), date(2026, 1, 31), D("1"))
    # sin ningún mes completo dentro del período
    assert r.ventana_ritmo(date(2026, 1, 20), date(2026, 1, 1)) is None


# ── Proyección ──
def _proy(acu, ritmo, cat="B", ref=date(2026, 10, 1), hasta=date(2026, 12, 31)):
    return r.calcular_proyeccion(acumulado=D(str(acu)), ritmo_mensual=D(str(ritmo)),
                                 ref=ref, f_hasta=hasta, categoria=cat, categorias=T,
                                 sujeto="el cliente")


def test_proyeccion_dentro_de_categoria():
    p = _proy(10_000_000, 1_000_000)
    assert p["categoria_proyectada"] == "B" and not p["supera_categoria"]
    assert p["mes_cruce"] is None and p["exceso_a_postergar"] is None
    assert p["margen_total"] == pytest.approx(float(T["B"]) - 10_000_000)


def test_proyeccion_sube_de_categoria_con_mes_de_cruce():
    p = _proy(16_000_000, 1_500_000)                 # cruza B (17,6 M) en noviembre
    assert p["supera_categoria"] and p["categoria_proyectada"] == "C"
    assert p["mes_cruce"] == "noviembre 2026"
    assert p["exceso_a_postergar"] == pytest.approx(p["proyeccion"] - float(T["B"]))
    assert p["categoria_objetivo"] == "B"


def test_proyeccion_ya_supero_la_categoria():
    p = _proy(20_000_000, 1_000_000)                 # ya pasó B: lo mínimo posible es C
    assert p["ya_supero_categoria"] and p["categoria_minima"] == "C"
    assert p["categoria_objetivo"] == "C" and p["margen_total"] == 0


def test_proyeccion_piso_en_lo_ya_facturado():
    p = _proy(15_000_000, -2_000_000)                # ritmo negativo por notas de crédito
    assert p["proyeccion"] == 15_000_000


def test_proyeccion_exclusion():
    p = _proy(120_000_000, 5_000_000, cat="K")
    assert p["supera_k"] and p["categoria_proyectada"] is None
    assert p["exceso_a_postergar"] is None           # no hay nada que postergar: excede K
    assert r.debe_alertar_proyeccion(p) == (True, "exclusion")


def test_proyeccion_periodo_cerrado():
    p = _proy(10_000_000, 1_000_000, ref=date(2027, 1, 10))
    assert p["periodo_cerrado"] and p["proyeccion"] == 10_000_000


def test_alerta_por_suba_de_categoria():
    assert r.debe_alertar_proyeccion(_proy(16_000_000, 1_500_000)) == (True, "suba_categoria")
    assert r.debe_alertar_proyeccion(_proy(5_000_000, 100_000)) == (False, "")
