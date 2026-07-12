"""
Parser del Excel multi-monotributista para Monotributo Más Fácil.

Columnas esperadas (case-insensitive, acepta variantes):
  fecha         → date
  importe       → Decimal
  cliente       → str
  dni_cliente   → str (opcional)
  concepto      → str
  monotributista → str (nombre o CUIT — se resuelve contra la BD)

El parser es tolerante: acepta variantes de nombres de columna,
formatos de fecha argentinos, importes con $ y puntos de miles.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

import openpyxl


# ---------------------------------------------------------------------------
# Alias de columnas aceptados (todo lowercase)
# ---------------------------------------------------------------------------
ALIAS_COLUMNAS = {
    "fecha": ["fecha", "date", "fecha_pago", "fecha de pago"],
    "importe": ["importe", "monto", "total", "amount", "precio", "valor"],
    "cliente": ["cliente", "alumno", "paciente", "receptor", "nombre_cliente", "nombre cliente"],
    "dni_cliente": ["dni", "dni_cliente", "cuit_cliente", "documento", "doc"],
    "email_cliente": ["email", "email_cliente", "mail", "mail_cliente", "correo"],
    "concepto": ["concepto", "descripcion", "descripción", "detalle", "servicio"],
    "monotributista": ["monotributista", "emisor", "profesional", "cuit_emisor", "nombre_emisor"],
}


@dataclass
class FilaParsed:
    """Una fila del Excel después de parsear, antes de resolver contra la BD."""
    fila_numero: int
    fecha_raw: str
    importe_raw: str
    cliente_raw: str
    dni_cliente_raw: Optional[str]
    email_cliente_raw: Optional[str]
    concepto_raw: str
    monotributista_raw: str

    # Resueltos
    fecha: Optional[date] = None
    importe: Optional[Decimal] = None
    valida: bool = True
    errores: list[str] = field(default_factory=list)


@dataclass
class ResultadoParseo:
    filas: list[FilaParsed]
    errores_globales: list[str] = field(default_factory=list)
    monotributistas_detectados: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utilidades de parseo
# ---------------------------------------------------------------------------

def _normalizar_col(nombre: str) -> str:
    return str(nombre).lower().strip().replace(" ", "_")


def _detectar_columnas(headers: list) -> dict[str, int]:
    """
    Devuelve {campo_canonico: indice_columna}.
    Lanza ValueError si falta alguna columna obligatoria.
    """
    mapa = {}
    headers_norm = [_normalizar_col(h) for h in headers]

    for campo, aliases in ALIAS_COLUMNAS.items():
        for alias in aliases:
            alias_norm = alias.replace(" ", "_")
            if alias_norm in headers_norm:
                mapa[campo] = headers_norm.index(alias_norm)
                break

    obligatorias = ["fecha", "importe", "cliente", "concepto", "monotributista"]
    faltantes = [c for c in obligatorias if c not in mapa]
    if faltantes:
        raise ValueError(
            f"El Excel no tiene las columnas requeridas: {', '.join(faltantes)}. "
            f"Columnas detectadas: {', '.join(str(h) for h in headers)}"
        )
    return mapa


def _parsear_fecha(valor) -> tuple[Optional[date], Optional[str]]:
    """Parsea fechas en formatos DD/MM/YYYY, YYYY-MM-DD, DD-MM-YYYY, objeto date."""
    if valor is None or str(valor).strip() == "":
        return None, "Fecha vacía"

    if isinstance(valor, date):
        return valor, None

    s = str(valor).strip()

    # Formato DD/MM/YYYY o DD-MM-YYYY
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))), None
        except ValueError:
            return None, f"Fecha inválida: {s}"

    # Formato YYYY-MM-DD
    m = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None
        except ValueError:
            return None, f"Fecha inválida: {s}"

    return None, f"Formato de fecha no reconocido: {s}"


def _parsear_importe(valor) -> tuple[Optional[Decimal], Optional[str]]:
    """Parsea importes: acepta $1.500,50 / 1500.50 / 1500,50 / 1500"""
    if valor is None or str(valor).strip() == "":
        return None, "Importe vacío"

    if isinstance(valor, (int, float)):
        d = Decimal(str(valor)).quantize(Decimal("0.01"))
        if d <= 0:
            return None, f"El importe debe ser positivo: {valor}"
        return d, None

    s = str(valor).strip()
    s = s.replace("$", "").replace(" ", "")

    # Si tiene punto como separador de miles y coma como decimal: 1.500,50
    if re.search(r"\d\.\d{3},", s):
        s = s.replace(".", "").replace(",", ".")
    # Si solo tiene coma como decimal: 1500,50
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    # Si solo tiene punto (puede ser decimal): 1500.50
    # No hacemos nada

    try:
        d = Decimal(s).quantize(Decimal("0.01"))
        if d <= 0:
            return None, f"El importe debe ser positivo: {valor}"
        return d, None
    except InvalidOperation:
        return None, f"Importe no reconocido: {valor}"


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def parsear_excel(file_bytes: bytes) -> ResultadoParseo:
    """
    Parsea el Excel y devuelve ResultadoParseo con todas las filas.
    No toca la base de datos — solo parsea y valida estructura/tipos.
    La resolución de monotributistas y clientes se hace en el servicio.
    """
    try:
        wb = openpyxl.load_workbook(filename=__import__("io").BytesIO(file_bytes), data_only=True)
    except Exception as e:
        return ResultadoParseo(
            filas=[],
            errores_globales=[f"No se pudo abrir el archivo Excel: {e}"]
        )

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    if len(rows) < 2:
        return ResultadoParseo(
            filas=[],
            errores_globales=["El archivo está vacío o solo tiene encabezados."]
        )

    # Detectar columnas desde la primera fila
    try:
        mapa = _detectar_columnas(list(rows[0]))
    except ValueError as e:
        return ResultadoParseo(filas=[], errores_globales=[str(e)])

    filas_parsed = []
    monotributistas_set = set()

    for i, row in enumerate(rows[1:], start=2):  # fila 2 en adelante (1-indexed para el usuario)
        def celda(campo: str) -> str:
            idx = mapa.get(campo)
            if idx is None:
                return ""
            v = row[idx]
            return str(v).strip() if v is not None else ""

        monotributista_raw = celda("monotributista")
        cliente_raw = celda("cliente")
        concepto_raw = celda("concepto")
        fecha_raw = row[mapa["fecha"]] if mapa.get("fecha") is not None else None
        importe_raw = row[mapa["importe"]] if mapa.get("importe") is not None else None
        dni_raw = celda("dni_cliente") if "dni_cliente" in mapa else None
        email_raw = celda("email_cliente") if "email_cliente" in mapa else None

        # Saltear filas completamente vacías
        if not any([monotributista_raw, cliente_raw, str(fecha_raw or "").strip(), str(importe_raw or "").strip()]):
            continue

        fila = FilaParsed(
            fila_numero=i,
            fecha_raw=str(fecha_raw) if fecha_raw is not None else "",
            importe_raw=str(importe_raw) if importe_raw is not None else "",
            cliente_raw=cliente_raw,
            dni_cliente_raw=dni_raw if dni_raw else None,
            email_cliente_raw=email_raw if email_raw else None,
            concepto_raw=concepto_raw,
            monotributista_raw=monotributista_raw,
        )

        # Parsear fecha
        fila.fecha, err_fecha = _parsear_fecha(fecha_raw)
        if err_fecha:
            fila.errores.append(err_fecha)

        # Parsear importe
        fila.importe, err_importe = _parsear_importe(importe_raw)
        if err_importe:
            fila.errores.append(err_importe)

        # Validaciones básicas
        if not monotributista_raw:
            fila.errores.append("Falta el nombre/CUIT del monotributista")
        if not cliente_raw:
            fila.errores.append("Falta el nombre del cliente")
        if not concepto_raw:
            fila.errores.append("Falta el concepto")

        fila.valida = len(fila.errores) == 0

        if monotributista_raw:
            monotributistas_set.add(monotributista_raw)

        filas_parsed.append(fila)

    return ResultadoParsed(
        filas=filas_parsed,
        monotributistas_detectados=sorted(monotributistas_set),
    )


# Typo intencional corregido — alias para compatibilidad
ResultadoParsed = ResultadoParseo
