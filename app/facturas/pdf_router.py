"""
Router de PDF y compartir facturas para Monotributo Más Fácil.

GET  /facturas/{id}/pdf          → descarga el PDF de la factura
POST /facturas/{id}/enviar-mail  → envía la factura por mail al cliente final
GET  /facturas/{id}/whatsapp     → genera link de WhatsApp con mensaje
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.auth import get_current_user_page, CurrentUser
from app.auth.models import Monotributista, ClienteFinal
from app.database import get_db
from app.facturas.models import Factura, EstadoFactura
from app.templates_config import templates

router = APIRouter(tags=["pdf"])

WA_NUMBER = "541124659703"  # +54 11 24659703


def _nombre_emisor(mono: Monotributista) -> str:
    """Nombre a mostrar en el PDF: fantasía > nombre > razon_social."""
    return mono.nombre_fantasia or mono.razon_social


async def _generar_pdf_factura(
    factura: Factura,
    mono: Monotributista,
    cliente: ClienteFinal | None,
) -> bytes:
    """Genera el PDF de la factura usando el generador de Facturo Más Fácil."""
    from app.facturas.pdf_generator import generar_factura_pdf

    return generar_factura_pdf(
        razon_social=_nombre_emisor(mono),
        cuit_emisor=mono.cuit,
        punto_venta=factura.punto_venta or mono.afip_punto_venta or 1,
        cbte_nro=factura.cbte_nro or 0,
        cbte_tipo=factura.cbte_tipo or 11,
        cbte_fecha=factura.cbte_fecha or date.today(),
        imp_total=float(factura.imp_total),
        cae=factura.cae or "",
        cae_vto=factura.cae_vto,
        concepto=factura.concepto or "Servicios",
        domicilio_emisor=mono.domicilio or "",
        ingresos_brutos=None,
        logo_base64=getattr(mono, "logo_base64", None),
        cliente_nombre=cliente.nombre if cliente else "Consumidor Final",
        cliente_dni=cliente.dni if cliente else None,
        cliente_cuit=cliente.cuit if cliente else None,
        fch_serv_desde=factura.fch_serv_desde,
        fch_serv_hasta=factura.fch_serv_hasta,
    )


# ---------------------------------------------------------------------------
# Descarga de PDF
# ---------------------------------------------------------------------------

@router.get("/facturas/{factura_id}/pdf")
async def descargar_pdf(
    factura_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    current_user = await get_current_user_page(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    result = await db.execute(
        select(Factura).where(
            Factura.id == factura_id,
            Factura.tenant_id == current_user.tenant_id,
        )
    )
    factura = result.scalar_one_or_none()
    if not factura:
        raise HTTPException(404, "Factura no encontrada")

    mono = await db.get(Monotributista, factura.monotributista_id)
    cliente = await db.get(ClienteFinal, factura.cliente_id) if factura.cliente_id else None

    try:
        pdf_bytes = await _generar_pdf_factura(factura, mono, cliente)
    except Exception as e:
        raise HTTPException(500, f"Error generando PDF: {e}")

    filename = f"factura_C_{factura.punto_venta or 1:04d}_{factura.cbte_nro or 0:08d}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Enviar por mail al cliente final
# ---------------------------------------------------------------------------

@router.post("/facturas/{factura_id}/enviar-mail", response_class=HTMLResponse)
async def enviar_mail_cliente(
    factura_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    current_user = await get_current_user_page(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    result = await db.execute(
        select(Factura).where(
            Factura.id == factura_id,
            Factura.tenant_id == current_user.tenant_id,
        )
    )
    factura = result.scalar_one_or_none()
    if not factura:
        raise HTTPException(404)

    mono = await db.get(Monotributista, factura.monotributista_id)
    cliente = await db.get(ClienteFinal, factura.cliente_id) if factura.cliente_id else None

    if not cliente or not cliente.email:
        return HTMLResponse("""
            <div style="background:#FEF9C3;border:1px solid #F59E0B;border-radius:8px;
                        padding:10px 14px;font-size:13px;color:#854D0E">
                ⚠️ El cliente no tiene email registrado.
            </div>""")

    try:
        pdf_bytes = await _generar_pdf_factura(factura, mono, cliente)
    except Exception as e:
        return HTMLResponse(f"""
            <div style="background:#FEE2E2;border:1px solid #FECACA;border-radius:8px;
                        padding:10px 14px;font-size:13px;color:#991B1B">
                ✗ Error generando PDF: {e}
            </div>""")

    import base64
    from app.email import _send, _base_html

    pdf_b64 = base64.b64encode(pdf_bytes).decode()
    nombre_emisor = _nombre_emisor(mono)
    nro_str = f"{factura.punto_venta or 1:04d}-{factura.cbte_nro or 0:08d}"

    html = _base_html(f"""
        <h2>Tu factura</h2>
        <p>Hola <strong>{cliente.nombre}</strong>,</p>
        <p>Te adjuntamos la Factura C N° {nro_str} emitida por <strong>{nombre_emisor}</strong>.</p>
        <p style="margin-top:16px;font-size:12px;color:#6B7280">
            Importe total: <strong>$ {float(factura.imp_total):,.2f}</strong><br>
            CAE: {factura.cae or '—'}
        </p>
    """)

    # Envío con adjunto via Resend
    from app.config import RESEND_API_KEY, EMAIL_FROM
    import httpx

    ok = False
    if RESEND_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client_http:
                resp = await client_http.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                    json={
                        "from": EMAIL_FROM,
                        "to": [cliente.email],
                        "subject": f"Tu factura — {nombre_emisor}",
                        "html": html,
                        "attachments": [{
                            "filename": f"factura_{nro_str}.pdf",
                            "content": pdf_b64,
                        }],
                    }
                )
                ok = resp.status_code in (200, 201)
        except Exception:
            pass

    if ok:
        return HTMLResponse(f"""
            <div style="background:#DCFCE7;border:1px solid #86EFAC;border-radius:8px;
                        padding:10px 14px;font-size:13px;color:#166534">
                ✓ Factura enviada a <strong>{cliente.email}</strong>
            </div>""")
    else:
        return HTMLResponse("""
            <div style="background:#FEE2E2;border:1px solid #FECACA;border-radius:8px;
                        padding:10px 14px;font-size:13px;color:#991B1B">
                ✗ Error al enviar el mail. Verificá la configuración de Resend.
            </div>""")


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

@router.get("/facturas/{factura_id}/whatsapp")
async def whatsapp_link(
    factura_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Devuelve el link de WhatsApp para compartir la factura."""
    current_user = await get_current_user_page(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    result = await db.execute(
        select(Factura).where(
            Factura.id == factura_id,
            Factura.tenant_id == current_user.tenant_id,
        )
    )
    factura = result.scalar_one_or_none()
    if not factura:
        raise HTTPException(404)

    mono = await db.get(Monotributista, factura.monotributista_id)
    cliente = await db.get(ClienteFinal, factura.cliente_id) if factura.cliente_id else None

    nombre_emisor = _nombre_emisor(mono)
    nro_str = f"{factura.punto_venta or 1:04d}-{factura.cbte_nro or 0:08d}"
    saludo = f"Hola {cliente.nombre}!" if cliente else "Hola!"

    import urllib.parse
    texto = urllib.parse.quote(
        f"{saludo} Te comparto tu Factura C N° {nro_str} "
        f"emitida por {nombre_emisor}.\n"
        f"Importe: $ {float(factura.imp_total):,.2f}\n"
        f"CAE: {factura.cae or '—'}\n\n"
        f"Emitida con Monotributo Más Fácil"
    )

    # Link a número de WhatsApp Business de Monotributo Más Fácil
    wa_url = f"https://wa.me/{WA_NUMBER}?text={texto}"
    return JSONResponse({"url": wa_url})
