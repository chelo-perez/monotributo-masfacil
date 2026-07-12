"""
Modelos de facturas y lotes de emisión para Monotributo Más Fácil.

La gran diferencia con Facturo Más Fácil: un LoteEmision puede contener
facturas de MÚLTIPLES monotributistas, emitidas en paralelo.
"""

from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date,
    ForeignKey, Numeric, Text, Enum as SAEnum
)
from sqlalchemy.orm import relationship
import enum

from app.auth.models import Base


class EstadoFactura(str, enum.Enum):
    pendiente = "pendiente"
    aprobada = "aprobada"
    rechazada = "rechazada"
    anulada = "anulada"


class EstadoLote(str, enum.Enum):
    borrador = "borrador"       # Excel subido, pendiente confirmación
    emitiendo = "emitiendo"     # en proceso
    completado = "completado"   # terminó (con o sin errores)


class LoteEmision(Base):
    """
    Un lote agrupa N facturas de M monotributistas distintos.
    El contador sube un Excel → se crea un lote → preview → confirma → se emite.
    """
    __tablename__ = "lotes_emision"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    creado_por = Column(Integer, ForeignKey("users.id"), nullable=False)

    nombre = Column(String(200))                          # "Julio 2026 — todos"
    estado = Column(SAEnum(EstadoLote), default=EstadoLote.borrador)

    total_facturas = Column(Integer, default=0)
    aprobadas = Column(Integer, default=0)
    rechazadas = Column(Integer, default=0)

    excel_filename = Column(String(300), nullable=True)   # nombre original del archivo
    created_at = Column(DateTime, default=datetime.utcnow)
    emitido_at = Column(DateTime, nullable=True)

    facturas = relationship("Factura", back_populates="lote")


class FilaExcel(Base):
    """
    Una fila del Excel importado, antes de convertirse en factura.
    Permite mostrar la preview y guardar errores de validación.
    """
    __tablename__ = "filas_excel"

    id = Column(Integer, primary_key=True)
    lote_id = Column(Integer, ForeignKey("lotes_emision.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)

    # Datos del Excel (tal como venían)
    fila_numero = Column(Integer)                         # nro de fila original
    fecha_raw = Column(String(50))
    importe_raw = Column(String(50))
    cliente_raw = Column(String(200))
    dni_cliente_raw = Column(String(50), nullable=True)
    email_cliente_raw = Column(String(200), nullable=True)
    concepto_raw = Column(String(500))
    monotributista_raw = Column(String(200))              # nombre o CUIT

    # Resolución
    monotributista_id = Column(Integer, ForeignKey("monotributistas.id"), nullable=True)
    cliente_id = Column(Integer, ForeignKey("clientes_finales.id"), nullable=True)
    fecha_resuelta = Column(Date, nullable=True)
    importe_resuelto = Column(Numeric(12, 2), nullable=True)

    # Validación
    valida = Column(Boolean, default=True)
    error = Column(Text, nullable=True)                   # descripción del error si hay


class Factura(Base):
    """
    Factura emitida ante ARCA. Siempre Tipo C (11) para monotributistas.
    """
    __tablename__ = "facturas"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    lote_id = Column(Integer, ForeignKey("lotes_emision.id"), nullable=True)
    monotributista_id = Column(Integer, ForeignKey("monotributistas.id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("clientes_finales.id"), nullable=True)
    fila_excel_id = Column(Integer, ForeignKey("filas_excel.id"), nullable=True)

    # ARCA
    cbte_tipo = Column(Integer, default=11)               # 11 = Factura C
    cbte_nro = Column(Integer, nullable=True)
    punto_venta = Column(Integer, nullable=True)
    cbte_fecha = Column(Date, nullable=True)
    fch_serv_desde = Column(Date, nullable=True)
    fch_serv_hasta = Column(Date, nullable=True)

    # Importes
    imp_total = Column(Numeric(12, 2), nullable=False)
    concepto = Column(String(500), nullable=True)

    # Resultado ARCA
    cae = Column(String(20), nullable=True)
    cae_vto = Column(Date, nullable=True)
    afip_result = Column(SAEnum(EstadoFactura), default=EstadoFactura.pendiente)
    afip_obs = Column(Text, nullable=True)                # observaciones/errores ARCA

    # Estado
    anulada = Column(Boolean, default=False)
    pdf_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    lote = relationship("LoteEmision", back_populates="facturas")
    monotributista = relationship("Monotributista", back_populates="facturas")
    cliente = relationship("ClienteFinal", back_populates="facturas")
