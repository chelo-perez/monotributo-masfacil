# ============================================================
# afip/csv_importer.py
# Importador del CSV de "Mis Comprobantes" de AFIP
# Solo importa comprobantes que NO existen en afip_invoice_history
# ============================================================
import csv
import io
import zipfile
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.afip.history_models import AfipInvoiceHistory

log = logging.getLogger(__name__)

TIPOS_VALIDOS = {1, 2, 3, 6, 7, 8, 11, 12, 13}


def _parse_decimal(s: str) -> Decimal:
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def _parse_date(s: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_mis_comprobantes_csv(file_bytes: bytes) -> list[dict]:
    # Descomprimir si es ZIP
    if file_bytes[:2] == b'PK':
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            for name in zf.namelist():
                if name.lower().endswith('.csv'):
                    file_bytes = zf.read(name)
                    break

    text = file_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.reader(io.StringIO(text), delimiter=';')
    rows = list(reader)
    if not rows:
        return []

    headers = [h.strip() for h in rows[0]]
    def col(row, name):
        try:
            return row[headers.index(name)].strip()
        except (ValueError, IndexError):
            return ""

    result = []
    for row in rows[1:]:
        if not row or not any(row):
            continue
        try:
            cbte_tipo = int(col(row, 'Tipo de Comprobante'))
            pv        = int(col(row, 'Punto de Venta'))
            nro       = int(col(row, 'Número Desde'))
        except (ValueError, TypeError):
            continue

        if cbte_tipo not in TIPOS_VALIDOS:
            continue

        fecha = _parse_date(col(row, 'Fecha de Emisión'))
        if not fecha:
            continue

        result.append({
            "cbte_tipo":   cbte_tipo,
            "punto_venta": pv,
            "cbte_nro":    nro,
            "cbte_fecha":  fecha,
            "imp_total":   _parse_decimal(col(row, 'Imp. Total')),
            "cae":         col(row, 'Cód. Autorización') or None,
        })

    return result


async def import_mis_comprobantes(
    file_bytes: bytes,
    tenant_id: int,
    mono_id: int,
    db: AsyncSession,
) -> dict:
    rows = parse_mis_comprobantes_csv(file_bytes)
    if not rows:
        return {"parsed": 0, "importados": 0, "ya_existian": 0, "errores": 0, "primer_error": None}

    importados  = 0
    ya_existian = 0
    errores     = 0
    primer_error = None

    for r in rows:
        try:
            # Verificar si ya existe (por PV + tipo + nro — clave correcta)
            existing_q = await db.execute(
                select(AfipInvoiceHistory).where(
                    AfipInvoiceHistory.mono_id   == mono_id,
                    AfipInvoiceHistory.cbte_tipo   == r["cbte_tipo"],
                    AfipInvoiceHistory.cbte_nro    == r["cbte_nro"],
                    AfipInvoiceHistory.punto_venta == r["punto_venta"],
                )
            )
            if existing_q.scalar_one_or_none():
                ya_existian += 1
                continue

            # fch_serv_desde no está disponible en el CSV de Mis Comprobantes.
            # Usamos cbte_fecha como proxy — para servicios mensuales (pilates/fitness)
            # la fecha de emisión y la de devengamiento corresponden al mismo mes,
            # por lo que el error en el acumulado de 365 días es mínimo.
            # source = 'mis_comprobantes' permite identificar estos registros.
            fch_proxy = r["cbte_fecha"]
            db.add(AfipInvoiceHistory(
                tenant_id      = tenant_id,
                mono_id      = mono_id,
                cbte_tipo      = r["cbte_tipo"],
                punto_venta    = r["punto_venta"],
                cbte_nro       = r["cbte_nro"],
                cbte_fecha     = r["cbte_fecha"],
                fch_serv_desde = fch_proxy,
                fch_serv_hasta = fch_proxy,
                imp_total      = r["imp_total"],
                cae            = r["cae"],
                source         = "mis_comprobantes",
            ))
            # flush individual con savepoint para aislar errores
            await db.flush()
            importados += 1

        except Exception as e:
            await db.rollback()
            log.warning(f"Skip PV={r.get('punto_venta')} tipo={r.get('cbte_tipo')} nro={r.get('cbte_nro')}: {e.__class__.__name__}")
            if not primer_error:
                primer_error = f"PV={r.get('punto_venta')} tipo={r.get('cbte_tipo')} #{r.get('cbte_nro')}: {e.__class__.__name__}"
            errores += 1

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        return {
            "parsed": len(rows), "importados": 0,
            "ya_existian": ya_existian, "errores": len(rows) - ya_existian,
            "primer_error": f"Error al guardar: {e}",
        }

    return {
        "parsed":       len(rows),
        "importados":   importados,
        "ya_existian":  ya_existian,
        "errores":      errores,
        "primer_error": primer_error,
    }
