"""
Sincronización de comprobantes desde ARCA para Monotributo Más Fácil.
Adaptado de afip/sync_service.py de Facturo Más Fácil.
"""

import logging
from datetime import date, timedelta
from ..fechas import hoy_ar
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.afip.history_models import AfipInvoiceHistory
from app.auth.models import Monotributista

log = logging.getLogger(__name__)

CBTE_TIPOS = [
    (11, "Factura C"),
    (12, "Nota de Débito C"),
    (13, "Nota de Crédito C"),
]


def get_fecha_corte() -> date:
    """365 días atrás — cubre el período de exclusión de monotributo."""
    return hoy_ar() - timedelta(days=365)


async def sync_mono_invoices(
    mono_id: int,
    tenant_id: int,
    db: AsyncSession,
) -> dict:
    """
    Sincroniza comprobantes de un monotributista desde ARCA
    usando FECompConsultar para los últimos 365 días.
    """
    from app.wsfe import get_token_sign, _wsfe_call
    from app.auth.models import Monotributista
    from app.config import FERNET_KEY
    from app.wsfe import load_credentials
    import xml.etree.ElementTree as ET

    mono = await db.get(Monotributista, mono_id)
    if not mono or not mono.cert_encrypted:
        return {"error": "Monotributista sin certificado configurado", "importados": 0}

    fecha_corte = get_fecha_corte()

    try:
        cert_pem, key_pem = load_credentials(mono, FERNET_KEY)
        token, sign = await get_token_sign(
            cert_pem, key_pem,
            environment=mono.afip_environment or "production"
        )
    except Exception as e:
        return {"error": f"Error de autenticación ARCA: {e}", "importados": 0}

    cuit_limpio = mono.cuit.replace("-", "")
    pvs = [mono.afip_punto_venta] if mono.afip_punto_venta else []

    if not pvs:
        return {"error": "Sin punto de venta configurado", "importados": 0}

    total_importados = 0
    total_ya_existian = 0
    total_errores = 0

    ns = "http://ar.gov.afip.dif.FEV1/"

    for pv in pvs:
        for cbte_tipo, nombre_tipo in CBTE_TIPOS:
            # Obtener último número para este PV/tipo
            try:
                from app.wsfe import get_ultimo_cbte
                ultimo = await get_ultimo_cbte(
                    token, sign, cuit_limpio, pv, cbte_tipo,
                    environment=mono.afip_environment or "production"
                )
            except Exception:
                continue

            if ultimo == 0:
                continue

            # Consultar comprobante por comprobante desde el corte
            for nro in range(max(1, ultimo - 500), ultimo + 1):
                try:
                    body = f"""<Auth>
                        <Token>{token}</Token><Sign>{sign}</Sign><Cuit>{cuit_limpio}</Cuit>
                    </Auth>
                    <FeCompConsReq>
                        <CbteTipo>{cbte_tipo}</CbteTipo>
                        <CbteNro>{nro}</CbteNro>
                        <PtoVta>{pv}</PtoVta>
                    </FeCompConsReq>"""

                    root = await _wsfe_call("FECompConsultar", body, mono.afip_environment or "production")

                    fecha_str = root.findtext(f".//{{{ns}}}CbteFch") or ""
                    if not fecha_str:
                        continue

                    try:
                        cbte_fecha = date(int(fecha_str[:4]), int(fecha_str[4:6]), int(fecha_str[6:8]))
                    except Exception:
                        continue

                    if cbte_fecha < fecha_corte:
                        continue

                    imp_total_str = root.findtext(f".//{{{ns}}}ImpTotal") or "0"
                    imp_total = Decimal(imp_total_str)

                    desde_str = root.findtext(f".//{{{ns}}}FchServDesde") or fecha_str
                    hasta_str = root.findtext(f".//{{{ns}}}FchServHasta") or fecha_str

                    def parse_d(s):
                        try:
                            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                        except Exception:
                            return cbte_fecha

                    fch_desde = parse_d(desde_str)
                    fch_hasta = parse_d(hasta_str)
                    cae = root.findtext(f".//{{{ns}}}CodAutorizacion") or None

                    # Verificar si ya existe
                    existing = await db.execute(
                        select(AfipInvoiceHistory).where(
                            AfipInvoiceHistory.mono_id == mono_id,
                            AfipInvoiceHistory.cbte_tipo == cbte_tipo,
                            AfipInvoiceHistory.cbte_nro == nro,
                            AfipInvoiceHistory.punto_venta == pv,
                        )
                    )
                    if existing.scalar_one_or_none():
                        total_ya_existian += 1
                        continue

                    db.add(AfipInvoiceHistory(
                        tenant_id=tenant_id,
                        mono_id=mono_id,
                        cbte_tipo=cbte_tipo,
                        punto_venta=pv,
                        cbte_nro=nro,
                        cbte_fecha=cbte_fecha,
                        fch_serv_desde=fch_desde,
                        fch_serv_hasta=fch_hasta,
                        imp_total=imp_total,
                        cae=cae,
                        source="wsfe",
                    ))
                    await db.flush()
                    total_importados += 1

                except Exception as e:
                    log.warning(f"Skip PV={pv} tipo={cbte_tipo} nro={nro}: {e}")
                    total_errores += 1
                    continue

    await db.commit()
    return {
        "importados": total_importados,
        "ya_existian": total_ya_existian,
        "errores": total_errores,
    }
