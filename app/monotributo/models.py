"""
Tablas históricas de categorías de monotributo.
Cada registro representa una tabla vigente en un período.
"""

from datetime import date, datetime, timezone
from sqlalchemy import Date, DateTime, Integer, String, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.auth.models import Base


class TablaCategorias(Base):
    """
    Tabla de categorías de monotributo vigente en un período.
    Los topes se guardan como JSON: {"A": 10277988.13, "B": ..., "K": ...}
    """
    __tablename__ = "tablas_categorias"

    id:           Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    vigente_desde: Mapped[date]      = mapped_column(Date, nullable=False, unique=True)
    vigente_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)  # NULL = vigente actual
    label:        Mapped[str]        = mapped_column(String(50), nullable=False)  # "Feb 2026 – Jul 2026"
    fuente:       Mapped[str | None] = mapped_column(String(200), nullable=True)  # URL ARCA
    activa:       Mapped[bool]       = mapped_column(Boolean, default=True)

    # JSON con topes de ingresos brutos por categoría
    # {"A": 10277988.13, "B": 15058447.71, ..., "K": 108357084.05}
    topes:        Mapped[dict]       = mapped_column(JSON, nullable=False)

    # JSON con cuotas mensuales (servicios)
    # {"A": 42386.74, "B": 48250.78, ..., "K": 1381687.90}
    cuotas_servicios: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # JSON con cuotas mensuales (venta de bienes)
    cuotas_bienes: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at:   Mapped[datetime]   = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
