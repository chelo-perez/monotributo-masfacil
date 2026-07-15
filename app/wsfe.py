# ============================================================
# wsfe.py — Monotributo Más Fácil
#
# Cliente WSAA + WSFE copiado de Facturo Más Fácil.
# Diferencia: recibe cert_pem/key_pem como str en lugar de bytes.
# ============================================================
import base64
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7, Encoding
from cryptography.x509 import load_pem_x509_certificate
from cryptography.hazmat.backends import default_backend

from app.auth.models import Monotributista

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
WSAA_URLS = {
    "production":   "https://wsaa.afip.gov.ar/ws/services/LoginCms",
    "homologation": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
}
WSFE_URLS = {
    "production":   "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
    "homologation": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
}

# ---------------------------------------------------------------------------
# Cache de tickets en memoria (por cuit:environment)
# ---------------------------------------------------------------------------
_ticket_cache: dict[str, tuple[str, str, datetime]] = {}


def _afip_http_client(timeout: int = 30) -> httpx.AsyncClient:
    """
    Cliente httpx con SSL bajado a SECLEVEL=1.
    AFIP usa claves DH pequeñas que Python 3.12+ rechaza por defecto.
    """
    import ssl
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return httpx.AsyncClient(timeout=timeout, verify=ctx)


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------

def load_credentials(mono: Monotributista, fernet_key: bytes) -> tuple[str, str]:
    """Desencripta cert y key almacenados en el modelo Monotributista."""
    if not fernet_key:
        raise ValueError("FERNET_KEY no configurada")
    if not mono.cert_encrypted or not mono.key_encrypted:
        raise ValueError(f"Monotributista {mono.cuit} no tiene certificado cargado")
    from cryptography.fernet import Fernet
    f = Fernet(fernet_key)
    cert_pem = f.decrypt(mono.cert_encrypted.encode()).decode()
    key_pem  = f.decrypt(mono.key_encrypted.encode()).decode()
    return cert_pem, key_pem


def encrypt_credentials(cert_pem: str, key_pem: str, fernet_key: bytes) -> tuple[str, str]:
    """Encripta cert y key para guardar en BD."""
    from cryptography.fernet import Fernet
    f = Fernet(fernet_key)
    return (
        f.encrypt(cert_pem.encode()).decode(),
        f.encrypt(key_pem.encode()).decode(),
    )


# ---------------------------------------------------------------------------
# WSAA — autenticación
# ---------------------------------------------------------------------------

def _build_tra(service: str = "wsfe") -> str:
    """Construye el Ticket de Requerimiento de Acceso (TRA)."""
    now      = datetime.now(timezone.utc)
    gen_time = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    exp_time = (now + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    # uniqueId debe ser un entero de hasta 10 dígitos (schema ARCA)
    import time as _time
    unique_id = str(int(_time.time()))[-10:]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<loginTicketRequest version="1.0">
  <header>
    <uniqueId>{unique_id}</uniqueId>
    <generationTime>{gen_time}</generationTime>
    <expirationTime>{exp_time}</expirationTime>
  </header>
  <service>{service}</service>
</loginTicketRequest>"""


def _sign_tra(tra_xml: str, cert_pem: str, key_pem: str) -> str:
    """
    Firma el TRA con PKCS7.
    ARCA requiere firma con contenido incluido (NO DetachedSignature).
    """
    cert = load_pem_x509_certificate(cert_pem.encode(), default_backend())
    key  = serialization.load_pem_private_key(key_pem.encode(), password=None, backend=default_backend())

    builder = pkcs7.PKCS7SignatureBuilder()
    builder = builder.set_data(tra_xml.encode())
    builder = builder.add_signer(cert, key, hashes.SHA256())
    # Sin DetachedSignature — ARCA requiere contenido incluido
    signed = builder.sign(Encoding.DER, [])
    return base64.b64encode(signed).decode()


async def get_token_sign(
    cert_pem: str,
    key_pem: str,
    environment: str = "production",
    service: str = "wsfe",
) -> tuple[str, str]:
    """Obtiene token y sign del WSAA. Cachea por cuit hasta el vencimiento."""
    cache_key = f"{cert_pem[:30]}:{environment}:{service}"
    if cache_key in _ticket_cache:
        token, sign, expira = _ticket_cache[cache_key]
        if datetime.now(timezone.utc) < expira - timedelta(minutes=5):
            return token, sign

    tra = _build_tra(service)
    cms = _sign_tra(tra, cert_pem, key_pem)

    soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov.ar">
  <soapenv:Header/>
  <soapenv:Body>
    <wsaa:loginCms>
      <wsaa:in0>{cms}</wsaa:in0>
    </wsaa:loginCms>
  </soapenv:Body>
</soapenv:Envelope>"""

    url = WSAA_URLS.get(environment, WSAA_URLS["production"])
    async with _afip_http_client(30) as client:
        resp = await client.post(
            url, content=soap.encode(),
            headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": ""},
        )
        if resp.status_code != 200:
            raise ValueError(f"WSAA error {resp.status_code}: {resp.text[:1000]}")

    root = ET.fromstring(resp.text)
    result_text = None
    for elem in root.iter():
        if "loginCmsReturn" in elem.tag:
            result_text = elem.text
            break

    if not result_text:
        raise ValueError(f"WSAA: no se encontró loginCmsReturn. Response: {resp.text[:500]}")

    ta = ET.fromstring(result_text)
    token   = ta.findtext(".//token") or ""
    sign    = ta.findtext(".//sign")  or ""
    exp_str = ta.findtext(".//expirationTime") or ""

    if not token or not sign:
        raise ValueError("WSAA: token o sign vacíos en la respuesta")

    expira = datetime.now(timezone.utc) + timedelta(hours=10)
    if exp_str:
        try:
            expira = datetime.fromisoformat(exp_str)
        except Exception:
            pass

    _ticket_cache[cache_key] = (token, sign, expira)
    return token, sign


# ---------------------------------------------------------------------------
# WSFE — último comprobante
# ---------------------------------------------------------------------------

async def get_ultimo_cbte(
    token: str,
    sign: str,
    cuit: str,
    punto_venta: int,
    cbte_tipo: int = 11,
    environment: str = "production",
) -> int:
    """Retorna el número del último comprobante autorizado."""
    url = WSFE_URLS.get(environment, WSFE_URLS["production"])
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ar="http://ar.gov.afip.dif.FEV1/">
  <soapenv:Header/>
  <soapenv:Body>
    <ar:FECompUltimoAutorizado>
      <ar:Auth>
        <ar:Token>{token}</ar:Token>
        <ar:Sign>{sign}</ar:Sign>
        <ar:Cuit>{cuit}</ar:Cuit>
      </ar:Auth>
      <ar:PtoVta>{punto_venta}</ar:PtoVta>
      <ar:CbteTipo>{cbte_tipo}</ar:CbteTipo>
    </ar:FECompUltimoAutorizado>
  </soapenv:Body>
</soapenv:Envelope>"""

    async with _afip_http_client(30) as client:
        resp = await client.post(
            url, content=body.encode(),
            headers={
                "Content-Type": "text/xml; charset=UTF-8",
                "SOAPAction": "http://ar.gov.afip.dif.FEV1/FECompUltimoAutorizado",
            },
        )
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    for elem in root.iter():
        if elem.tag.endswith("CbteNro"):
            try:
                return int(elem.text or 0)
            except ValueError:
                pass
    return 0


# ---------------------------------------------------------------------------
# WSFE — solicitar CAE
# ---------------------------------------------------------------------------

async def solicitar_cae(
    token: str,
    sign: str,
    cuit: str,
    punto_venta: int,
    cbte_tipo: int,
    cbte_nro: int,
    cbte_fecha: date,
    imp_total: float,
    concepto: int = 2,
    doc_tipo: int = 99,
    doc_nro: str = "0",
    fch_serv_desde: Optional[date] = None,
    fch_serv_hasta: Optional[date] = None,
    environment: str = "production",
    cond_iva_receptor: Optional[int] = None,
) -> tuple[Optional[str], Optional[date], Optional[str]]:
    """
    Emite un comprobante via FECAESolicitar.
    Retorna (cae, cae_vto, obs_str).
    """
    import calendar as _cal
    url      = WSFE_URLS.get(environment, WSFE_URLS["production"])
    fecha_str = cbte_fecha.strftime("%Y%m%d")

    # Fechas de servicio: primer y último día del mes del comprobante
    anio = cbte_fecha.year
    mes  = cbte_fecha.month
    ult  = _cal.monthrange(anio, mes)[1]
    fch_desde  = (fch_serv_desde or date(anio, mes, 1)).strftime("%Y%m%d")
    fch_hasta  = (fch_serv_hasta or date(anio, mes, ult)).strftime("%Y%m%d")
    fch_vto_pago = fch_hasta  # vencimiento = último día del mes

    # Condición IVA del receptor
    if cond_iva_receptor is not None:
        cond_iva = cond_iva_receptor
    elif doc_tipo == 99:
        cond_iva = 5  # Consumidor Final
    elif doc_tipo == 96:
        cond_iva = 5  # DNI → asumir CF
    else:
        cond_iva = 5

    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ar="http://ar.gov.afip.dif.FEV1/">
  <soapenv:Header/>
  <soapenv:Body>
    <ar:FECAESolicitar>
      <ar:Auth>
        <ar:Token>{token}</ar:Token>
        <ar:Sign>{sign}</ar:Sign>
        <ar:Cuit>{cuit}</ar:Cuit>
      </ar:Auth>
      <ar:FeCAEReq>
        <ar:FeCabReq>
          <ar:CantReg>1</ar:CantReg>
          <ar:PtoVta>{punto_venta}</ar:PtoVta>
          <ar:CbteTipo>{cbte_tipo}</ar:CbteTipo>
        </ar:FeCabReq>
        <ar:FeDetReq>
          <ar:FECAEDetRequest>
            <ar:Concepto>{concepto}</ar:Concepto>
            <ar:DocTipo>{doc_tipo}</ar:DocTipo>
            <ar:DocNro>{doc_nro}</ar:DocNro>
            <ar:CondicionIVAReceptorId>{cond_iva}</ar:CondicionIVAReceptorId>
            <ar:CbteDesde>{cbte_nro}</ar:CbteDesde>
            <ar:CbteHasta>{cbte_nro}</ar:CbteHasta>
            <ar:CbteFch>{fecha_str}</ar:CbteFch>
            <ar:ImpTotal>{imp_total:.2f}</ar:ImpTotal>
            <ar:ImpTotConc>0.00</ar:ImpTotConc>
            <ar:ImpNeto>{imp_total:.2f}</ar:ImpNeto>
            <ar:ImpOpEx>0.00</ar:ImpOpEx>
            <ar:ImpIVA>0.00</ar:ImpIVA>
            <ar:ImpTrib>0.00</ar:ImpTrib>
            <ar:FchServDesde>{fch_desde}</ar:FchServDesde>
            <ar:FchServHasta>{fch_hasta}</ar:FchServHasta>
            <ar:FchVtoPago>{fch_vto_pago}</ar:FchVtoPago>
            <ar:MonId>PES</ar:MonId>
            <ar:MonCotiz>1</ar:MonCotiz>
          </ar:FECAEDetRequest>
        </ar:FeDetReq>
      </ar:FeCAEReq>
    </ar:FECAESolicitar>
  </soapenv:Body>
</soapenv:Envelope>"""

    import logging as _log
    _log.getLogger(__name__).info(f"[WSFE] Enviando FECAESolicitar PtoVta={punto_venta} Tipo={cbte_tipo} Nro={cbte_nro} Fecha={cbte_fecha} Importe={imp_total}")
    async with _afip_http_client(30) as client:
        resp = await client.post(
            url, content=body.encode(),
            headers={
                "Content-Type": "text/xml; charset=UTF-8",
                "SOAPAction": "http://ar.gov.afip.dif.FEV1/FECAESolicitar",
            },
        )
        if not resp.is_success:
            _log.getLogger(__name__).error(f"[WSFE] Error {resp.status_code}: {resp.text[:2000]}")
            raise ValueError(f"WSFE FECAESolicitar error {resp.status_code}: {resp.text[:800]}")
        _log.getLogger(__name__).info(f"[WSFE] Respuesta: {resp.text[:500]}")

    root = ET.fromstring(resp.text)
    cae = resultado = cae_vto_s = None
    obs_parts = []

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "CAE":
            cae = elem.text or None
        elif tag == "CAEFchVto":
            cae_vto_s = elem.text
        elif tag == "Resultado":
            resultado = elem.text
        elif tag == "Msg" and elem.text:
            obs_parts.append(elem.text.strip())
        elif tag == "Cod" and elem.text:
            obs_parts.append(f"[{elem.text.strip()}]")

    obs_str = " | ".join(obs_parts) if obs_parts else None

    if resultado == "A" and cae:
        cae_vto = None
        if cae_vto_s:
            try:
                cae_vto = datetime.strptime(cae_vto_s, "%Y%m%d").date()
            except Exception:
                pass
        return cae, cae_vto, obs_str

    return None, None, obs_str or "ARCA rechazó la factura sin observaciones"


# ---------------------------------------------------------------------------
# Puntos de venta
# ---------------------------------------------------------------------------

async def get_puntos_venta(
    token: str,
    sign: str,
    cuit: str,
    environment: str = "production",
) -> list[int]:
    """Retorna todos los PVs habilitados para el CUIT."""
    url = WSFE_URLS.get(environment, WSFE_URLS["production"])
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ar="http://ar.gov.afip.dif.FEV1/">
  <soapenv:Header/>
  <soapenv:Body>
    <ar:FEParamGetPtosVenta>
      <ar:Auth>
        <ar:Token>{token}</ar:Token>
        <ar:Sign>{sign}</ar:Sign>
        <ar:Cuit>{cuit}</ar:Cuit>
      </ar:Auth>
    </ar:FEParamGetPtosVenta>
  </soapenv:Body>
</soapenv:Envelope>"""

    async with _afip_http_client(30) as client:
        r = await client.post(
            url, content=body.encode(),
            headers={
                "Content-Type": "text/xml; charset=UTF-8",
                "SOAPAction": "http://ar.gov.afip.dif.FEV1/FEParamGetPtosVenta",
            },
        )

    if not r.is_success:
        return []

    root = ET.fromstring(r.text)
    pvs = []
    for elem in root.iter():
        if elem.tag.endswith("Nro"):
            try:
                pvs.append(int(elem.text or 0))
            except ValueError:
                pass
    return sorted(set(pvs))
