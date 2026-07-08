"""
Servicio de importación del Excel para Monotributo Más Fácil.

Responsabilidades:
1. Tomar el resultado del parser (FilaParsed[])
2. Resolver cada monotributista_raw → Monotributista en BD (por nombre o CUIT)
3. Resolver/crear cada cliente_raw → ClienteFinal en BD
4. Crear las FilaExcel y el LoteEmision en BD
5. Devolver resumen para mostrar el preview al contador
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Monotributista, ClienteFinal, Tenant
from app.facturas.models import LoteEmision, FilaExcel, EstadoLote
from app.excel.parser import FilaParsed, parsear_excel


def _normalizar_texto(s: str) -> str:
    """Lowercase, sin acentos básicos, sin espacios dobles."""
    s = s.lower().strip()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s)


def _es_cuit(s: str) -> bool:
    """True si parece un CUIT (con o sin guiones)."""
    limpio = s.replace("-", "").replace(" ", "")
    return limpio.isdigit() and len(limpio) == 11


async def _resolver_monotributista(
    raw: str,
    tenant_id: int,
    db: AsyncSession,
) -> tuple[Optional[Monotributista], Optional[str]]:
    """
    Intenta encontrar el monotributista en BD por CUIT exacto o nombre aproximado.
    Devuelve (monotributista, error).
    """
    raw = raw.strip()
    if not raw:
        return None, "Monotributista vacío"

    if _es_cuit(raw):
        cuit_limpio = raw.replace("-", "").replace(" ", "")
        cuit_fmt = f"{cuit_limpio[:2]}-{cuit_limpio[2:10]}-{cuit_limpio[10]}"
        result = await db.execute(
            select(Monotributista).where(
                Monotributista.tenant_id == tenant_id,
                Monotributista.cuit == cuit_fmt,
                Monotributista.activo == True,
            )
        )
        mono = result.scalar_one_or_none()
        if mono:
            return mono, None
        return None, f"No se encontró el CUIT {raw} en la cuenta"

    # Búsqueda por nombre normalizado
    raw_norm = _normalizar_texto(raw)
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.tenant_id == tenant_id,
            Monotributista.activo == True,
        )
    )
    todos = result.scalars().all()
    for mono in todos:
        if _normalizar_texto(mono.razon_social) == raw_norm:
            return mono, None

    # Búsqueda parcial (contiene)
    candidatos = [m for m in todos if raw_norm in _normalizar_texto(m.razon_social)]
    if len(candidatos) == 1:
        return candidatos[0], None
    if len(candidatos) > 1:
        nombres = ", ".join(m.razon_social for m in candidatos)
        return None, f"'{raw}' coincide con más de un monotributista: {nombres}"

    return None, f"No se encontró el monotributista '{raw}'"


async def _resolver_o_crear_cliente(
    nombre: str,
    dni: Optional[str],
    monotributista_id: int,
    tenant_id: int,
    db: AsyncSession,
) -> ClienteFinal:
    """Busca el cliente por nombre (y dni si viene). Si no existe, lo crea."""
    nombre = nombre.strip()
    nombre_norm = _normalizar_texto(nombre)

    result = await db.execute(
        select(ClienteFinal).where(
            ClienteFinal.monotributista_id == monotributista_id,
        )
    )
    existentes = result.scalars().all()

    for c in existentes:
        if _normalizar_texto(c.nombre) == nombre_norm:
            # Si viene DNI y el cliente no lo tiene, actualizarlo
            if dni and not c.dni:
                c.dni = dni.strip()
            return c

    # No existe — crear
    nuevo = ClienteFinal(
        monotributista_id=monotributista_id,
        tenant_id=tenant_id,
        nombre=nombre,
        dni=dni.strip() if dni else None,
    )
    db.add(nuevo)
    await db.flush()  # obtener el id antes de commit
    return nuevo


# ---------------------------------------------------------------------------
# Resultado del import
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field

@dataclass
class ResumenMonotributista:
    razon_social: str
    cuit: str
    filas_validas: int
    filas_con_error: int
    total_importe: Decimal
    errores: list[str] = field(default_factory=list)


@dataclass
class ResultadoImport:
    lote_id: Optional[int]
    total_filas: int
    filas_validas: int
    filas_con_error: int
    por_monotributista: list[ResumenMonotributista]
    errores_globales: list[str] = field(default_factory=list)
    listo_para_emitir: bool = False


async def importar_excel(
    file_bytes: bytes,
    nombre_archivo: str,
    tenant_id: int,
    user_id: int,
    db: AsyncSession,
) -> ResultadoImport:
    """
    Pipeline completo: parsea → resuelve → guarda lote en BD como borrador.
    No emite nada todavía — el contador revisa el preview y confirma.
    """
    # 1. Parsear
    resultado_parseo = parsear_excel(file_bytes)

    if resultado_parseo.errores_globales:
        return ResultadoImport(
            lote_id=None,
            total_filas=0,
            filas_validas=0,
            filas_con_error=0,
            por_monotributista=[],
            errores_globales=resultado_parseo.errores_globales,
        )

    # 2. Crear lote en BD
    lote = LoteEmision(
        tenant_id=tenant_id,
        creado_por=user_id,
        nombre=f"Importación {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        estado=EstadoLote.borrador,
        excel_filename=nombre_archivo,
    )
    db.add(lote)
    await db.flush()

    # 3. Resolver filas y crear FilaExcel
    resumen_por_mono: dict[int, ResumenMonotributista] = {}
    filas_validas = 0
    filas_con_error = 0

    for fila_parsed in resultado_parseo.filas:
        # Resolver monotributista
        mono, err_mono = await _resolver_monotributista(
            fila_parsed.monotributista_raw, tenant_id, db
        )
        if err_mono:
            fila_parsed.errores.append(err_mono)
            fila_parsed.valida = False

        # Resolver cliente (solo si el mono existe)
        cliente_id = None
        if mono and fila_parsed.valida:
            cliente = await _resolver_o_crear_cliente(
                nombre=fila_parsed.cliente_raw,
                dni=fila_parsed.dni_cliente_raw,
                monotributista_id=mono.id,
                tenant_id=tenant_id,
                db=db,
            )
            cliente_id = cliente.id

        # Guardar FilaExcel
        fila_db = FilaExcel(
            lote_id=lote.id,
            tenant_id=tenant_id,
            fila_numero=fila_parsed.fila_numero,
            fecha_raw=fila_parsed.fecha_raw,
            importe_raw=fila_parsed.importe_raw,
            cliente_raw=fila_parsed.cliente_raw,
            dni_cliente_raw=fila_parsed.dni_cliente_raw,
            concepto_raw=fila_parsed.concepto_raw,
            monotributista_raw=fila_parsed.monotributista_raw,
            monotributista_id=mono.id if mono else None,
            cliente_id=cliente_id,
            fecha_resuelta=fila_parsed.fecha,
            importe_resuelto=fila_parsed.importe,
            valida=fila_parsed.valida,
            error="; ".join(fila_parsed.errores) if fila_parsed.errores else None,
        )
        db.add(fila_db)

        # Acumular resumen
        if mono:
            if mono.id not in resumen_por_mono:
                resumen_por_mono[mono.id] = ResumenMonotributista(
                    razon_social=mono.razon_social,
                    cuit=mono.cuit,
                    filas_validas=0,
                    filas_con_error=0,
                    total_importe=Decimal("0"),
                )
            r = resumen_por_mono[mono.id]
            if fila_parsed.valida:
                r.filas_validas += 1
                r.total_importe += fila_parsed.importe or Decimal("0")
            else:
                r.filas_con_error += 1
                r.errores.extend(fila_parsed.errores)

        if fila_parsed.valida:
            filas_validas += 1
        else:
            filas_con_error += 1

    lote.total_facturas = len(resultado_parseo.filas)
    await db.commit()

    return ResultadoImport(
        lote_id=lote.id,
        total_filas=len(resultado_parseo.filas),
        filas_validas=filas_validas,
        filas_con_error=filas_con_error,
        por_monotributista=list(resumen_por_mono.values()),
        listo_para_emitir=filas_validas > 0,
    )
