"""
Historial de comprobantes traídos de ARCA para Monotributo Más Fácil.
Equivalente a AfipInvoiceHistory en Facturo Más Fácil,
adaptado para usar Integer PKs en vez de UUID.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from sqlalchemy import (
    Date, DateTime, Integer, Numeric, String,
    UniqueConstraint, ForeignKey,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.auth.models import Base


class AfipInvoiceHistory(Base):
    """
    Comprobante traído de ARCA vía FECompConsultar o importado desde CSV.
    Fuente de verdad para el cálculo de monotributo (semáforo).
    """
    __tablename__ = "afip_invoice_history"
    __table_args__ = (
        UniqueConstraint(
            "mono_id", "cbte_tipo", "cbte_nro", "punto_venta",
            name="uq_afip_history_cbte"
        ),
    )

    id:             Mapped[int]          = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id:      Mapped[int]          = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    mono_id:        Mapped[int]          = mapped_column(Integer, ForeignKey("monotributistas.id", ondelete="CASCADE"), nullable=False, index=True)

    cbte_tipo:      Mapped[int]          = mapped_column(Integer, nullable=False)
    punto_venta:    Mapped[int]          = mapped_column(Integer, nullable=False)
    cbte_nro:       Mapped[int]          = mapped_column(Integer, nullable=False)
    cbte_fecha:     Mapped[date]         = mapped_column(Date, nullable=False)
    concepto:       Mapped[int]          = mapped_column(Integer, default=2)   # 2=Servicios

    fch_serv_desde: Mapped[date | None]  = mapped_column(Date, nullable=True)
    fch_serv_hasta: Mapped[date | None]  = mapped_column(Date, nullable=True)

    imp_total:      Mapped[Decimal]      = mapped_column(Numeric(14, 2), nullable=False)
    cae:            Mapped[str | None]   = mapped_column(String(20), nullable=True)

    # 'wsfe' = sincronizado via API | 'mis_comprobantes' = importado desde CSV
    source:         Mapped[str | None]   = mapped_column(String(20), default="wsfe")
    synced_at:      Mapped[datetime]     = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )
