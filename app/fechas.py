"""
Fecha y hora en zona horaria argentina.

Railway (y casi cualquier hosting) corre en UTC. Entre las 21:00 y las 00:00
hora argentina, `date.today()` en UTC ya devuelve el día SIGUIENTE. Para todo
lo fiscal (fecha de comprobante, ventanas de monotributo, cierres de mes) hay
que usar SIEMPRE la fecha argentina, si no una factura emitida el 31/07 a las
22:00 sale fechada 01/08 y cae en el mes equivocado.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")


def hoy_ar() -> date:
    """Fecha actual en Argentina (no en UTC)."""
    return datetime.now(TZ_AR).date()


def ahora_ar() -> datetime:
    """Datetime actual en Argentina."""
    return datetime.now(TZ_AR)
