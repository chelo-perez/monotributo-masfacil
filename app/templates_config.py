"""Configuración centralizada de Jinja2 + filtros custom."""

from jinja2 import Environment
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


def format_money(value) -> str:
    """$ 1.234.567,89 — formato argentino."""
    if value is None:
        return "—"
    try:
        f = float(value)
        # Separar parte entera y decimal
        partes = f"{f:,.2f}".split(".")
        entero = partes[0].replace(",", ".")
        decimal = partes[1]
        return f"{entero},{decimal}"
    except (TypeError, ValueError):
        return str(value)


def format_cuit(value: str) -> str:
    """20123456789 → 20-12345678-9"""
    if not value:
        return ""
    limpio = value.replace("-", "").replace(" ", "")
    if len(limpio) == 11:
        return f"{limpio[:2]}-{limpio[2:10]}-{limpio[10]}"
    return value


templates.env.filters["format_money"] = format_money
templates.env.filters["format_cuit"] = format_cuit
