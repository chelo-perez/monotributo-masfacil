"""
Consulta al Padrón ARCA para Monotributo Más Fácil.
Standalone — no depende del wsfe.py de Facturo Más Fácil.
Adaptado de app/afip/padron.py de Facturo Más Fácil.

Servicios:
  - ws_sr_constancia_inscripcion (Alcance 5): razón social, domicilio, monotributo
"""

from __future__ import annotations

import base64
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging

import httpx

logger = logging.getLogger(__name__)

PADRON_A5_URL_PROD = "https://aws.arca.gob.ar/sr-padron/webservices/personaServiceA5"
PADRON_A5_URL_HOMO = "https://awshomo.arca.gob.ar/sr-padron/webservices/personaServiceA5"
WSAA_URL_PROD = "https://wsaa.afip.gov.ar/ws/services/LoginCms"
WSAA_URL_HOMO = "https://wsaahomo.afip.gov.ar/ws/services/LoginCms"
WS_ID_A5 = "ws_sr_constancia_inscripcion"


@dataclass
class DomicilioFiscal:
    direccion: str = ""
    localidad: str = ""
    provincia: str = ""

    def __str__(self) -> str:
        return ", ".join(p for p in [self.direccion, self.localidad, self.provincia] if p)


@dataclass
class ConstanciaInscripcion:
    cuit: str
    razon_social: str
    tipo_persona: str
    estado_clave: str
    domicilio_fiscal: DomicilioFiscal = field(default_factory=DomicilioFiscal)
    es_monotributo: bool = False
    categoria_monotributo: Optional[str] = None
    actividades: list[str] = field(default_factory=list)
    error: Optional[str] = None


def _texto(elem, tag: str, default: str = "") -> str:
    found = elem.find(tag)
    return (found.text or "").strip() if found is not None and found.text else default


async def _get_ticket_a5(cert_pem: str, key_pem: str, environment: str = "production") -> tuple[str, str]:
    """Obtiene ticket WSAA para ws_sr_constancia_inscripcion."""
    from cryptography.x509 import load_pem_x509_certificate
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7 as crypto_pkcs7, Encoding
    from cryptography.hazmat.backends import default_backend

    now = datetime.now(timezone.utc)
    gen_time = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    exp_time = (now + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    unique_id = str(int(time.time()))[-10:]

    tra_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<loginTicketRequest version="1.0">
  <header>
    <uniqueId>{unique_id}</uniqueId>
    <generationTime>{gen_time}</generationTime>
    <expirationTime>{exp_time}</expirationTime>
  </header>
  <service>{WS_ID_A5}</service>
</loginTicketRequest>"""

    cert = load_pem_x509_certificate(cert_pem.encode(), default_backend())
    private_key = serialization.load_pem_private_key(cert_pem.encode() and key_pem.encode(), password=None, backend=default_backend())
    builder = crypto_pkcs7.PKCS7SignatureBuilder()
    builder = builder.set_data(tra_xml.encode())
    builder = builder.add_signer(cert, private_key, hashes.SHA256())
    signed = builder.sign(Encoding.DER, [])
    cms = base64.b64encode(signed).decode()

    wsaa_url = WSAA_URL_PROD if environment == "production" else WSAA_URL_HOMO
    soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov.ar">
  <soapenv:Header/>
  <soapenv:Body>
    <wsaa:loginCms><wsaa:in0>{cms}</wsaa:in0></wsaa:loginCms>
  </soapenv:Body>
</soapenv:Envelope>"""

    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        resp = await client.post(wsaa_url, content=soap.encode(),
                                 headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": ""})
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    result_text = None
    for elem in root.iter():
        if "loginCmsReturn" in elem.tag:
            result_text = elem.text
            break

    if not result_text:
        raise ValueError("WSAA: no se encontró loginCmsReturn")

    ticket = ET.fromstring(result_text)
    token = ticket.findtext(".//token") or ""
    sign = ticket.findtext(".//sign") or ""
    if not token or not sign:
        raise ValueError("WSAA: token o sign vacíos")
    return token, sign


async def consultar_constancia(
    cuit_consulta: str,
    cuit_representada: str,
    cert_pem: str,
    key_pem: str,
    environment: str = "production",
) -> ConstanciaInscripcion:
    """
    Consulta la constancia de inscripción de un CUIT en ARCA (Alcance 5).
    Usa el cert/key del monotributista consultante (cuit_representada).
    """
    try:
        token, sign = await _get_ticket_a5(cert_pem, key_pem, environment)
    except Exception as e:
        logger.error(f"Padrón A5: error WSAA: {e}")
        return ConstanciaInscripcion(cuit=cuit_consulta, razon_social="", tipo_persona="",
                                     estado_clave="", error=f"Error de autenticación ARCA: {e}")

    url = PADRON_A5_URL_PROD if environment == "production" else PADRON_A5_URL_HOMO
    soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:a5="http://a5.soap.ws.server.puc.sr/">
  <soapenv:Header/>
  <soapenv:Body>
    <a5:getPersona_v2>
      <token>{token}</token>
      <sign>{sign}</sign>
      <cuitRepresentada>{cuit_representada}</cuitRepresentada>
      <idPersona>{cuit_consulta}</idPersona>
    </a5:getPersona_v2>
  </soapenv:Body>
</soapenv:Envelope>"""

    try:
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            resp = await client.post(url, content=soap.encode(),
                                     headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": ""})
            resp.raise_for_status()
    except Exception as e:
        return ConstanciaInscripcion(cuit=cuit_consulta, razon_social="", tipo_persona="",
                                     estado_clave="", error=f"Error de conexión con ARCA: {e}")

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return ConstanciaInscripcion(cuit=cuit_consulta, razon_social="", tipo_persona="",
                                     estado_clave="", error=f"Error XML: {e}")

    persona = None
    for elem in root.iter():
        if "personaReturn" in elem.tag:
            persona = elem
            break

    if persona is None:
        return ConstanciaInscripcion(cuit=cuit_consulta, razon_social="", tipo_persona="",
                                     estado_clave="", error="ARCA no devolvió datos para ese CUIT")

    dg = persona.find("datosGenerales")
    if dg is None:
        error_text = next((e.text for e in persona.iter() if "error" in e.tag.lower() and e.text), "Sin datos generales")
        return ConstanciaInscripcion(cuit=cuit_consulta, razon_social="", tipo_persona="",
                                     estado_clave="", error=error_text)

    tipo_persona = _texto(dg, "tipoPersona")
    if tipo_persona == "FISICA":
        razon_social = f"{_texto(dg, 'apellido')} {_texto(dg, 'nombre')}".strip()
    else:
        razon_social = _texto(dg, "razonSocial") or _texto(dg, "apellido")

    dom_elem = dg.find("domicilioFiscal")
    domicilio = DomicilioFiscal()
    if dom_elem is not None:
        domicilio.direccion = _texto(dom_elem, "direccion")
        domicilio.localidad = _texto(dom_elem, "localidad")
        domicilio.provincia = _texto(dom_elem, "descripcionProvincia")

    actividades = [_texto(a, "descripcionActividad") for a in persona.findall(".//actividad")
                   if _texto(a, "descripcionActividad")][:3]

    es_monotributo = False
    categoria_mono = None
    dm = persona.find("datosMonotributo")
    if dm is not None:
        cat_elem = dm.find("categoriaMonotributo")
        if cat_elem is not None:
            es_monotributo = True
            cat_id = _texto(cat_elem, "idCategoria") or _texto(cat_elem, "descripcionCategoria")
            if cat_id:
                categoria_mono = cat_id.replace("CATEGORIA_", "").strip()

    if not es_monotributo:
        for imp in persona.findall(".//impuesto"):
            if _texto(imp, "idImpuesto") == "20":
                es_monotributo = True
                break

    return ConstanciaInscripcion(
        cuit=cuit_consulta,
        razon_social=razon_social,
        tipo_persona=tipo_persona,
        estado_clave=_texto(dg, "estadoClave"),
        domicilio_fiscal=domicilio,
        es_monotributo=es_monotributo,
        categoria_monotributo=categoria_mono,
        actividades=actividades,
    )
