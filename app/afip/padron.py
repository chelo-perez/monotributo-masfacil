# ============================================================
# afip/padron.py
#
# Consulta al Padrón ARCA:
#   - ws_sr_constancia_inscripcion (Alcance 5):
#       Razón social, domicilio fiscal, condición IVA,
#       actividades, estado de clave.
#   - ws_sr_padron_a4 (Alcance 4):
#       Situación tributaria completa (impuestos inscripto,
#       categoría monotributo actual).
#
# Ambos servicios usan el mismo WSAA para autenticación.
# Se reutiliza get_ticket() y _afip_http_client() de wsfe.py.
# ============================================================

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
import logging

from app.wsfe import _afip_http_client, get_token_sign
import logging

logger = logging.getLogger(__name__)

WSAA_URL_PROD = "https://wsaa.afip.gov.ar/ws/services/LoginCms"
WSAA_URL_HOMO = "https://wsaahomo.afip.gov.ar/ws/services/LoginCms"


# ── URLs de los servicios ─────────────────────────────────────

PADRON_A5_URL_PROD = "https://aws.arca.gob.ar/sr-padron/webservices/personaServiceA5"
PADRON_A5_URL_HOMO = "https://awshomo.arca.gob.ar/sr-padron/webservices/personaServiceA5"

PADRON_A4_URL_PROD = "https://aws.arca.gob.ar/sr-padron/webservices/personaServiceA4"
PADRON_A4_URL_HOMO = "https://awshomo.arca.gob.ar/sr-padron/webservices/personaServiceA4"

WS_ID_A5 = "ws_sr_constancia_inscripcion"
WS_ID_A4 = "ws_sr_padron_a4"


# ── Dataclasses de resultado ──────────────────────────────────

@dataclass
class DomicilioFiscal:
    direccion: str = ""
    localidad: str = ""
    provincia: str = ""
    cod_postal: str = ""

    def __str__(self) -> str:
        partes = [p for p in [self.direccion, self.localidad, self.provincia] if p]
        return ", ".join(partes)


@dataclass
class ConstanciaInscripcion:
    """Resultado de ws_sr_constancia_inscripcion (Alcance 5)."""
    cuit: str
    razon_social: str               # nombre + apellido o razón social jurídica
    tipo_persona: str               # "FISICA" | "JURIDICA"
    estado_clave: str               # "ACTIVO" | "INACTIVO" | otro
    domicilio_fiscal: DomicilioFiscal = field(default_factory=DomicilioFiscal)
    es_monotributo: bool = False
    categoria_monotributo: Optional[str] = None   # "A", "B", ... "K"
    actividades: list[str] = field(default_factory=list)
    # Raw para debugging
    error: Optional[str] = None


@dataclass
class SituacionTributaria:
    """Resultado de ws_sr_padron_a4 (Alcance 4) — datos tributarios."""
    cuit: str
    condicion_iva: str              # "Responsable Monotributo" | "Responsable Inscripto" | etc.
    categoria_monotributo: Optional[str] = None
    impuestos_inscripto: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ── Helpers internos ──────────────────────────────────────────

async def _get_ticket_padron(cert_pem: str, key_pem: str, ws_id: str, environment: str = "production"):
    """Obtiene ticket WSAA para el servicio de padrón indicado — sin caché compartida."""
    # Usamos la función directamente sin caché para evitar colisiones con wsfe
    import base64
    import time
    import xml.etree.ElementTree as _ET
    from datetime import datetime, timezone, timedelta
    from cryptography.x509 import load_pem_x509_certificate
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7 as _pkcs7, Encoding
    from cryptography.hazmat.backends import default_backend
    from app.wsfe import WSAA_URLS, _afip_http_client

    now      = datetime.now(timezone.utc)
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
  <service>{ws_id}</service>
</loginTicketRequest>"""

    cert = load_pem_x509_certificate(cert_pem.encode(), default_backend())
    key  = serialization.load_pem_private_key(cert_pem.encode() if False else key_pem.encode(), password=None, backend=default_backend())

    builder = _pkcs7.PKCS7SignatureBuilder()
    builder = builder.set_data(tra_xml.encode())
    builder = builder.add_signer(cert, key, hashes.SHA256())
    signed  = builder.sign(Encoding.DER, [])
    cms     = base64.b64encode(signed).decode()

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
            raise ValueError(f"WSAA error {resp.status_code}: {resp.text[:500]}")

    root = _ET.fromstring(resp.text)
    result_text = None
    for elem in root.iter():
        if "loginCmsReturn" in elem.tag:
            result_text = elem.text
            break
    if not result_text:
        raise ValueError("WSAA: no loginCmsReturn")

    ta = _ET.fromstring(result_text)
    token = ta.findtext(".//token") or ""
    sign  = ta.findtext(".//sign")  or ""
    if not token or not sign:
        raise ValueError("WSAA: token/sign vacíos")
    return token, sign


def _texto(elem, tag: str, default: str = "") -> str:
    """Busca un tag en el elemento y devuelve su texto o default."""
    found = elem.find(tag)
    return (found.text or "").strip() if found is not None and found.text else default


# ── Consulta Alcance 5: Constancia de Inscripción ─────────────

async def consultar_constancia(
    cuit_consulta: str,
    cert_pem: str,
    key_pem: str,
    cuit_representada: str,
    environment: str = "production",
) -> ConstanciaInscripcion:
    """Consulta la constancia de inscripción de un CUIT en ARCA."""
    try:
        token, sign = await _get_ticket_padron(cert_pem, key_pem, WS_ID_A5, environment)
    except Exception as e:
        logger.error(f"Padrón A5: error obteniendo ticket WSAA: {e}")
        return ConstanciaInscripcion(
            cuit=cuit_consulta, razon_social="", tipo_persona="",
            estado_clave="", error=f"Error de autenticación ARCA: {e}",
        )

    url = PADRON_A5_URL_PROD if environment == "production" else PADRON_A5_URL_HOMO

    soap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
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
        async with _afip_http_client(20) as client:
            response = await client.post(
                url,
                content=soap_body.encode(),
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPAction": "",
                },
            )
            response.raise_for_status()
    except Exception as e:
        logger.error(f"Padrón A5: error HTTP: {e}")
        return ConstanciaInscripcion(
            cuit=cuit_consulta,
            razon_social="",
            tipo_persona="",
            estado_clave="",
            error=f"Error de conexión con ARCA: {e}",
        )

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as e:
        return ConstanciaInscripcion(
            cuit=cuit_consulta,
            razon_social="",
            tipo_persona="",
            estado_clave="",
            error=f"Error parseando respuesta XML: {e}",
        )

    # Buscar personaReturn (namespace-agnostic)
    persona = None
    for elem in root.iter():
        if "personaReturn" in elem.tag:
            persona = elem
            break

    if persona is None:
        return ConstanciaInscripcion(
            cuit=cuit_consulta,
            razon_social="",
            tipo_persona="",
            estado_clave="",
            error="ARCA no devolvió datos para ese CUIT",
        )

    # Extraer datosGenerales
    dg = persona.find("datosGenerales")
    if dg is None:
        error_text = ""
        for elem in persona.iter():
            if "error" in elem.tag.lower() and elem.text:
                error_text = elem.text
        return ConstanciaInscripcion(
            cuit=cuit_consulta,
            razon_social="",
            tipo_persona="",
            estado_clave="",
            error=error_text or "Sin datos generales en respuesta ARCA",
        )

    # Razón social: para FISICA es nombre + apellido, para JURIDICA es razonSocial
    tipo_persona = _texto(dg, "tipoPersona")
    if tipo_persona == "FISICA":
        nombre = _texto(dg, "nombre")
        apellido = _texto(dg, "apellido")
        razon_social = f"{apellido} {nombre}".strip()
    else:
        razon_social = _texto(dg, "razonSocial") or _texto(dg, "apellido")

    estado_clave = _texto(dg, "estadoClave")

    # Domicilio fiscal
    dom_elem = dg.find("domicilioFiscal")
    domicilio = DomicilioFiscal()
    if dom_elem is not None:
        domicilio.direccion = _texto(dom_elem, "direccion")
        domicilio.localidad = _texto(dom_elem, "localidad")
        domicilio.provincia = _texto(dom_elem, "descripcionProvincia")
        domicilio.cod_postal = _texto(dom_elem, "codPostal")

    # Actividades
    actividades = []
    for act in dg.findall(".//actividad") or persona.findall(".//actividad"):
        desc = _texto(act, "descripcionActividad")
        if desc:
            actividades.append(desc)

    # Monotributo: buscar en datosMonotributo
    es_monotributo = False
    categoria_mono = None
    dm = persona.find("datosMonotributo")
    if dm is not None:
        cat_elem = dm.find("categoriaMonotributo")
        if cat_elem is not None:
            es_monotributo = True
            # idCategoria: "A", "B", etc. o descripcionCategoria
            cat_id = _texto(cat_elem, "idCategoria") or _texto(cat_elem, "descripcionCategoria")
            if cat_id:
                # Tomar solo la letra si viene como "CATEGORIA_A" o similar
                categoria_mono = cat_id.replace("CATEGORIA_", "").strip()

    # Fallback: buscar en impuestos de regimenGeneral si tiene monotributo (id 20)
    if not es_monotributo:
        for imp in persona.findall(".//impuesto"):
            if _texto(imp, "idImpuesto") == "20":
                es_monotributo = True
                break

    return ConstanciaInscripcion(
        cuit=cuit_consulta,
        razon_social=razon_social,
        tipo_persona=tipo_persona,
        estado_clave=estado_clave,
        domicilio_fiscal=domicilio,
        es_monotributo=es_monotributo,
        categoria_monotributo=categoria_mono,
        actividades=actividades[:5],  # máximo 5 actividades
    )


# ── Consulta Alcance 4: Situación Tributaria ──────────────────

async def consultar_situacion_tributaria(
    cuit_consulta: str,
    cert_pem: str,
    key_pem: str,
    cuit_representada: str,
    environment: str = "production",
) -> SituacionTributaria:
    """
    Consulta la situación tributaria completa de un CUIT (Alcance 4).
    Útil para verificar condición IVA real y categoría de monotributo.
    """
    try:
        token, sign = await _get_ticket_padron(cert_pem, key_pem, WS_ID_A4, environment)
    except Exception as e:
        return SituacionTributaria(
            cuit=cuit_consulta,
            condicion_iva="",
            error=f"Error de autenticación: {e}",
        )

    url = PADRON_A4_URL_PROD if environment == "production" else PADRON_A4_URL_HOMO

    soap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:a4="http://a4.soap.ws.server.puc.sr/">
  <soapenv:Header/>
  <soapenv:Body>
    <a4:getPersona>
      <token>{token}</token>
      <sign>{sign}</sign>
      <cuitRepresentada>{cuit_representada}</cuitRepresentada>
      <idPersona>{cuit_consulta}</idPersona>
    </a4:getPersona>
  </soapenv:Body>
</soapenv:Envelope>"""

    try:
        async with _afip_http_client(20) as client:
            response = await client.post(
                url,
                content=soap_body.encode(),
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPAction": "",
                },
            )
            response.raise_for_status()
    except Exception as e:
        return SituacionTributaria(
            cuit=cuit_consulta,
            condicion_iva="",
            error=f"Error de conexión: {e}",
        )

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as e:
        return SituacionTributaria(
            cuit=cuit_consulta,
            condicion_iva="",
            error=f"Error XML: {e}",
        )

    persona = None
    for elem in root.iter():
        if "personaReturn" in elem.tag:
            persona = elem
            break

    if persona is None:
        return SituacionTributaria(
            cuit=cuit_consulta,
            condicion_iva="",
            error="Sin datos en respuesta ARCA",
        )

    # Condición IVA: buscar en impuestos
    # id 30 = IVA, id 20 = Monotributo
    impuestos = []
    condicion_iva = "No inscripto"
    categoria_mono = None

    for imp in persona.findall(".//impuesto"):
        id_imp = _texto(imp, "idImpuesto")
        desc = _texto(imp, "descripcionImpuesto")
        estado = _texto(imp, "estadoImpuesto")

        if estado != "AC":  # Solo activos
            continue

        if id_imp == "20":  # Monotributo
            condicion_iva = "Responsable Monotributo"
            # Buscar categoría
            cat = _texto(imp, "descripcionCategoria") or _texto(imp, "idCategoria")
            if cat:
                categoria_mono = cat
        elif id_imp == "30" and condicion_iva == "No inscripto":  # IVA
            condicion_iva = "Responsable Inscripto"

        if desc:
            impuestos.append(desc)

    return SituacionTributaria(
        cuit=cuit_consulta,
        condicion_iva=condicion_iva,
        categoria_monotributo=categoria_mono,
        impuestos_inscripto=impuestos,
    )
