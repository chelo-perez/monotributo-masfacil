"""
Modelos de autenticación y tenants para Monotributo Más Fácil.

Mapeo conceptual:
  Tenant     = Estudio Contable
  User       = Usuarios del estudio (contador, admin)
  Monotributista = Cliente del estudio (antes: Branch/Sede)
  ClienteFinal   = Cliente del monotributista (antes: Customer/Alumno)
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey,
    Numeric, Text, Enum as SAEnum
)
from sqlalchemy.orm import relationship, DeclarativeBase
import enum


class Base(DeclarativeBase):
    pass


class PlanEstudio(str, enum.Enum):
    basico = "basico"        # hasta 10 monotributistas — USD 30/mes
    estudio = "estudio"      # hasta 30 monotributistas — USD 60/mes
    pro = "pro"              # ilimitados — USD 100/mes


class Tenant(Base):
    """Estudio Contable."""
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    nombre = Column(String(200), nullable=False)          # "Gualtieri & Asociados"
    email_admin = Column(String(200), unique=True, nullable=False)
    plan = Column(SAEnum(PlanEstudio), default=PlanEstudio.basico)
    activo = Column(Boolean, default=False)               # activa el admin vía email
    trial_hasta = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="tenant")
    monotributistas = relationship("Monotributista", back_populates="tenant")


class User(Base):
    """Usuario del estudio contable (contador, empleado)."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    email = Column(String(200), unique=True, nullable=False)
    hashed_password = Column(String(200), nullable=False)
    nombre = Column(String(200))
    rol = Column(String(50), default="admin")             # admin | readonly
    activo = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="users")


class CategoriaMonotributo(str, enum.Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"
    G = "G"
    H = "H"
    I = "I"
    J = "J"
    K = "K"


class Monotributista(Base):
    """
    Cliente del estudio contable que es monotributista.
    Equivale a Branch/Sede en Facturo Más Fácil.
    El contador opera en su nombre — el monotributista NO tiene usuario.
    """
    __tablename__ = "monotributistas"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)

    # Datos fiscales
    cuit = Column(String(13), nullable=False)             # "27-12345678-9"
    razon_social = Column(String(200), nullable=False)
    nombre_fantasia = Column(String(200), nullable=True)
    logo_base64 = Column(Text, nullable=True)              # PNG/JPG en base64
    domicilio = Column(String(300), nullable=True)
    email = Column(String(200), nullable=True)            # para notificaciones al mono
    telefono = Column(String(50), nullable=True)

    # ARCA
    afip_punto_venta = Column(Integer, nullable=True)
    afip_environment = Column(String(20), default="production")
    cert_encrypted = Column(Text, nullable=True)          # Fernet
    key_encrypted = Column(Text, nullable=True)           # Fernet

    # Monotributo
    categoria_actual = Column(SAEnum(CategoriaMonotributo), nullable=True)
    actividad = Column(String(200), nullable=True)        # "Servicios de consultoría"

    # Estado
    activo = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="monotributistas")
    clientes = relationship("ClienteFinal", back_populates="monotributista")
    facturas = relationship("Factura", back_populates="monotributista")
    certificado = relationship("Certificado", back_populates="monotributista", uselist=False)


class Certificado(Base):
    """Certificado ARCA encriptado con Fernet, uno por monotributista."""
    __tablename__ = "certificados"

    id = Column(Integer, primary_key=True)
    monotributista_id = Column(Integer, ForeignKey("monotributistas.id"), unique=True)
    cert_encrypted = Column(Text, nullable=False)
    key_encrypted = Column(Text, nullable=False)
    vence_el = Column(DateTime, nullable=True)
    subido_at = Column(DateTime, default=datetime.utcnow)

    monotributista = relationship("Monotributista", back_populates="certificado")


class ClienteFinal(Base):
    """
    Cliente del monotributista (alumno, paciente, cliente de consultoría, etc.).
    Equivale a Customer en Facturo Más Fácil.
    """
    __tablename__ = "clientes_finales"

    id = Column(Integer, primary_key=True)
    monotributista_id = Column(Integer, ForeignKey("monotributistas.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)

    nombre = Column(String(200), nullable=False)
    dni = Column(String(20), nullable=True)
    cuit = Column(String(13), nullable=True)
    email = Column(String(200), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    monotributista = relationship("Monotributista", back_populates="clientes")
    facturas = relationship("Factura", back_populates="cliente")
