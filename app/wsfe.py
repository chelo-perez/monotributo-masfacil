"""
Módulo WSFE para Monotributo Más Fácil.
Adaptado de Facturo Más Fácil — misma lógica, interfaz simplificada
para el caso de uso de emisión masiva multi-CUIT.

Funciones principales:
  load_credentials(mono, fernet_key) → (cert_pem, key_pem)
  get_token_sign(cert_pem, key_pem)  → (token, sign)
  get_ultimo_cbte(...)               → int
  solicitar_cae(...)                 → (cae, cae_vto, obs)
"""

import base64
import hashlib
import ssl
import tempfile
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from xml.etree import ElementTree as ET

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

from app.auth.models import Monotributista

# ---------------------------------------------------------------------------
# URLs de los web services
# ---------------------------------------------------------------------------

WSAA_URLS = {
    "production":   "https://wsaa.afip.gov.ar/ws/services/LoginCms",
    "homologation": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
}
WSFE_URLS = {
    "production":   "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
    "homologation": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
}

# Cache de tickets en memoria (token, sign, expira)
_ticket_cache: dict[str, tuple[str, str, datetime]] = {}


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------

def load_credentials(mono: Monotributista, fernet_key: bytes) -> tuple[str, str]:
    """Desencripta cert y key almacenados en el modelo Monotributista."""
    if not fernet_key:
        raise ValueError("FERNET_KEY no configurada")
    if not mono.cert_encrypted or not mono.key_encrypted:
        raise ValueError(f"Monotributista {mono.cuit} no tiene certificado cargado")

    f = Fernet(fernet_key)
    cert_pem = f.decrypt(mono.cert_encrypted.encode()).decode()
    key_pem  = f.decrypt(mono.key_encrypted.encode()).decode()
    return cert_pem, key_pem


def encrypt_credentials(cert_pem: str, key_pem: str, fernet_key: bytes) -> tuple[str, str]:
    """Encripta cert y key para guardar en BD."""
    f = Fernet(fernet_key)
    return (
        f.encrypt(cert_pem.encode()).decode(),
        f.encrypt(key_pem.encode()).decode(),
    )


# ---------------------------------------------------------------------------
# WSAA — ticket de acceso
# ---------------------------------------------------------------------------

def _build_tra(service: str = "wsfe") -> str:
    now = datetime.now(timezone.utc)
    gen = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    exp = (now + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    uid = hashlib.md5(f"{service}{now}".encode()).hexdigest()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<loginTicketRequest version="1.0">
  <header>
    <uniqueId>{uid}</uniqueId>
    <generationTime>{gen}</generationTime>
    <expirationTime>{exp}</expirationTime>
  </header>
  <service>{service}</service>
</loginTicketRequest>"""


def _sign_tra(tra_xml: str, cert_pem: str, key_pem: str) -> str:
    """Firma el TRA y devuelve el CMS en base64."""
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
    key = serialization.load_pem_private_key(key_pem.encode(), password=None, backend=default_backend())

    data = tra_xml.encode()

    # Usar pkcs7 para firmar
    builder = pkcs7.PKCS7SignatureBuilder()
    builder = builder.set_data(data)
    builder = builder.add_signer(cert, key, hashes.SHA256())
    # ARCA requiere firma con contenido incluido (NO DetachedSignature)
    cms = builder.sign(serialization.Encoding.DER, [])
    return base64.b64encode(cms).decode()


async def get_token_sign(
    cert_pem: str,
    key_pem: str,
    environment: str = "production",
    service: str = "wsfe",
) -> tuple[str, str]:
    """
    Obtiene token y sign del WSAA.
    Cachea el ticket hasta 10 minutos antes de su vencimiento.
    """
    cache_key = f"{cert_pem[:20]}:{environment}:{service}"
    if cache_key in _ticket_cache:
        token, sign, expira = _ticket_cache[cache_key]
        if datetime.now(timezone.utc) < expira - timedelta(minutes=10):
            return token, sign

    tra = _build_tra(service)
    cms = _sign_tra(tra, cert_pem, key_pem)

    soap = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:tns="http://wsaa.view.sua.dvadac.desein.afip.gov">
  <soap:Body>
    <tns:loginCms>
      <tns:in0>{cms}</tns:in0>
    </tns:loginCms>
  </soap:Body>
</soap:Envelope>"""

    import logging as _log
    url = WSAA_URLS.get(environment, WSAA_URLS["production"])
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url, content=soap.encode(),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
        )
        if resp.status_code != 200:
            _log.getLogger(__name__).error(
                f"WSAA error {resp.status_code} para {environment}: {resp.text[:1000]}"
            )
            resp.raise_for_status()

    root = ET.fromstring(resp.text)
    ns = {"tns": "http://wsaa.view.sua.dvadac.desein.afip.gov"}
    result_text = root.find(".//loginCmsReturn", ns)
    if result_text is None:
        result_text = root.find(".//{http://wsaa.view.sua.dvadac.desein.afip.gov}loginCmsReturn")

    if result_text is None or not result_text.text:
        raise ValueError(f"WSAA no devolvió resultado. Response: {resp.text[:500]}")

    ta = ET.fromstring(result_text.text)
    token = ta.findtext(".//token") or ""
    sign  = ta.findtext(".//sign") or ""
    exp_str = ta.findtext(".//expirationTime") or ""

    expira = datetime.now(timezone.utc) + timedelta(hours=11)
    if exp_str:
        try:
            expira = datetime.fromisoformat(exp_str.replace("+00:00", "+00:00"))
        except Exception:
            pass

    _ticket_cache[cache_key] = (token, sign, expira)
    return token, sign


# ---------------------------------------------------------------------------
# WSFE — consultas y emisión
# ---------------------------------------------------------------------------

def _wsfe_header(token: str, sign: str, cuit: str) -> str:
    cuit_num = cuit.replace("-", "")
    return f"""<Auth>
      <Token>{token}</Token>
      <Sign>{sign}</Sign>
      <Cuit>{cuit_num}</Cuit>
    </Auth>"""


async def _wsfe_call(
    method: str,
    body: str,
    environment: str = "production",
) -> ET.Element:
    """Llamada genérica al WSFE."""
    soap = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:ar="http://ar.gov.afip.dif.FEV1/">
  <soap:Body>
    <ar:{method}>{body}</ar:{method}>
  </soap:Body>
</soap:Envelope>"""

    url = WSFE_URLS.get(environment, WSFE_URLS["production"])
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url, content=soap.encode(),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"http://ar.gov.afip.dif.FEV1/{method}",
            },
        )
        resp.raise_for_status()

    return ET.fromstring(resp.text)


async def get_ultimo_cbte(
    token: str,
    sign: str,
    cuit: str,
    punto_venta: int,
    cbte_tipo: int = 11,
    environment: str = "production",
) -> int:
    """Devuelve el último número de comprobante autorizado."""
    body = f"""{_wsfe_header(token, sign, cuit)}
    <PtoVta>{punto_venta}</PtoVta>
    <CbteTipo>{cbte_tipo}</CbteTipo>"""

    root = await _wsfe_call("FECompUltimoAutorizado", body, environment)
    nro = root.findtext(".//{http://ar.gov.afip.dif.FEV1/}CbteNro")
    return int(nro or 0)


async def solicitar_cae(
    token: str,
    sign: str,
    cuit: str,
    punto_venta: int,
    cbte_tipo: int,
    cbte_nro: int,
    cbte_fecha: date,
    imp_total: float,
    concepto: int = 2,           # 1=Productos, 2=Servicios, 3=P+S
    fch_serv_desde: Optional[date] = None,
    fch_serv_hasta: Optional[date] = None,
    cliente_nombre: str = "Consumidor Final",
    cliente_dni: Optional[str] = None,
    environment: str = "production",
    cond_iva_receptor: Optional[int] = None,  # 1=RI, 4=Exento, 5=CF, 6=Monotributo
) -> tuple[Optional[str], Optional[date], Optional[str]]:
    """
    Solicita CAE para una factura.
    Devuelve (cae, cae_vto, observaciones_error).
    Si ARCA rechaza, devuelve (None, None, mensaje_error).
    """
    fecha_str = cbte_fecha.strftime("%Y%m%d")
    import calendar as _cal
    _ultimo_dia = _cal.monthrange(cbte_fecha.year, cbte_fecha.month)[1]
    fch_desde = (fch_serv_desde or cbte_fecha.replace(day=1)).strftime("%Y%m%d")
    fch_hasta = (fch_serv_hasta or cbte_fecha.replace(day=_ultimo_dia)).strftime("%Y%m%d")

    # Receptor — default: Consumidor Final sin identificar (DocTipo 99, DocNro 0,
    # confirmado contra el WS; 96/0 no es el código correcto para CF anónimo)
    doc_tipo = 99
    doc_nro  = "0"
    if cliente_dni:
        dni_clean = cliente_dni.replace("-", "").replace(" ", "")
        if len(dni_clean) == 11 and dni_clean.isdigit():
            doc_tipo = 80  # CUIT
            doc_nro = dni_clean
        elif dni_clean.isdigit() and len(dni_clean) <= 8 and int(dni_clean) > 0:
            doc_tipo = 96  # DNI
            doc_nro = dni_clean

    # Condición IVA del receptor (CondicionIVAReceptorId — obligatorio desde
    # abril 2025, RG 5616). Si no viene explícita: 5 = Consumidor Final.
    cond_iva_rec = cond_iva_receptor if cond_iva_receptor is not None else 5

    body = f"""{_wsfe_header(token, sign, cuit)}
    <FeCAEReq>
      <FeCabReq>
        <CantReg>1</CantReg>
        <PtoVta>{punto_venta}</PtoVta>
        <CbteTipo>{cbte_tipo}</CbteTipo>
      </FeCabReq>
      <FeDetReq>
        <FECAEDetRequest>
          <Concepto>{concepto}</Concepto>
          <DocTipo>{doc_tipo}</DocTipo>
          <DocNro>{doc_nro}</DocNro>
          <CondicionIVAReceptorId>{cond_iva_rec}</CondicionIVAReceptorId>
          <CbteDesde>{cbte_nro}</CbteDesde>
          <CbteHasta>{cbte_nro}</CbteHasta>
          <CbteFch>{fecha_str}</CbteFch>
          <ImpTotal>{imp_total:.2f}</ImpTotal>
          <ImpTotConc>0</ImpTotConc>
          <ImpNeto>{imp_total:.2f}</ImpNeto>
          <ImpOpEx>0</ImpOpEx>
          <ImpIVA>0</ImpIVA>
          <ImpTrib>0</ImpTrib>
          <FchServDesde>{fch_desde}</FchServDesde>
          <FchServHasta>{fch_hasta}</FchServHasta>
          <FchVtoPago>{fch_hasta}</FchVtoPago>
          <MonId>PES</MonId>
          <MonCotiz>1</MonCotiz>
        </FECAEDetRequest>
      </FeDetReq>
    </FeCAEReq>"""

    root = await _wsfe_call("FECAESolicitar", body, environment)
    ns = "http://ar.gov.afip.dif.FEV1/"

    resultado = root.findtext(f".//{{{ns}}}Resultado")
    cae       = root.findtext(f".//{{{ns}}}CAE")
    cae_vto_s = root.findtext(f".//{{{ns}}}CAEFchVto")

    # Observaciones y errores
    obs_parts = []
    for obs in root.findall(f".//{{{ns}}}Obs"):
        msg  = obs.findtext(f"{{{ns}}}Msg") or ""
        code = obs.findtext(f"{{{ns}}}Code") or ""
        obs_parts.append(f"[{code}] {msg}")
    for err in root.findall(f".//{{{ns}}}Err"):
        msg  = err.findtext(f"{{{ns}}}Msg") or ""
        code = err.findtext(f"{{{ns}}}Code") or ""
        obs_parts.append(f"Error [{code}] {msg}")

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


async def get_puntos_venta(
    token: str,
    sign: str,
    cuit: str,
    environment: str = "production",
) -> list[int]:
    """
    Devuelve los puntos de venta activos habilitados para el CUIT.
    Usa FEParamGetPtosVenta del WSFE.
    """
    body = f"""{_wsfe_header(token, sign, cuit)}"""
    root = await _wsfe_call("FEParamGetPtosVenta", body, environment)
    ns = "http://ar.gov.afip.dif.FEV1/"

    pvs = []
    for pv in root.findall(f".//{{{ns}}}PtoVenta"):
        nro = pv.findtext(f"{{{ns}}}Nro")
        bloqueado = pv.findtext(f"{{{ns}}}Bloqueado")
        if nro and bloqueado == "N":
            pvs.append(int(nro))

    return sorted(pvs)
