"""
Router de facturación manual — emite una factura individual desde la UI.
Flujo: elegir monotributista → cliente → datos → emitir → PDF + compartir
"""
import base64
import calendar
import logging
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.auth import get_current_user_page, get_current_user, CurrentUser
from app.database import get_db
from app.auth.models import Monotributista
from app.facturas.models import Factura, EstadoFactura
from app.facturas.pdf_generator import generar_factura_pdf
from app.templates_config import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/factura-manual", tags=["manual"])


# ── GET /factura-manual ─────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
async def page_factura_manual(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.tenant_id == current_user.tenant_id,
            Monotributista.activo == True,
            Monotributista.cert_encrypted != None,
            Monotributista.afip_punto_venta != None,
        ).order_by(Monotributista.razon_social)
    )
    monos = result.scalars().all()

    return templates.TemplateResponse("facturas/manual.html", {
        "request": request,
        "current_user": current_user,
        "tenant_nombre": current_user.tenant_nombre,
        "active_page": "facturas",
        "monos": monos,
    })


# ── GET /factura-manual/ultimo-cbte ────────────────────────────────
@router.get("/ultimo-cbte", response_class=JSONResponse)
async def ultimo_cbte(
    mono_id: int,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    mono = await db.get(Monotributista, mono_id)
    if not mono or mono.tenant_id != current_user.tenant_id:
        return JSONResponse({"error": "No encontrado"}, status_code=404)

    try:
        from app.wsfe import get_token_sign, get_ultimo_cbte as _ultimo
        from app.config import FERNET_KEY
        from app.wsfe import load_credentials

        cert_pem, key_pem = load_credentials(mono, FERNET_KEY)
        token, sign = await get_token_sign(
            cert_pem, key_pem,
            environment=mono.afip_environment or "production"
        )
        cuit = mono.cuit.replace("-", "")
        ultimo = await _ultimo(token, sign, cuit, mono.afip_punto_venta, 11,
                               environment=mono.afip_environment or "production")
        return JSONResponse({
            "ultimo": ultimo,
            "siguiente": ultimo + 1,
            "punto_venta": mono.afip_punto_venta,
        })
    except Exception as e:
        log.error(f"Error obteniendo último cbte: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── GET /factura-manual/buscar-cliente ─────────────────────────────
@router.get("/buscar-cliente", response_class=JSONResponse)
async def buscar_cliente(
    q: str,
    mono_id: int,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.auth.models import ClienteFinal
    results = await db.execute(
        select(ClienteFinal).where(
            ClienteFinal.monotributista_id == mono_id,
            or_(
                ClienteFinal.nombre.ilike(f"%{q}%"),
                ClienteFinal.dni.ilike(f"%{q}%"),
            )
        ).order_by(ClienteFinal.nombre).limit(8)
    )
    clientes = results.scalars().all()
    return JSONResponse([
        {"id": c.id, "nombre": c.nombre, "dni": c.dni or "", "email": c.email or ""}
        for c in clientes
    ])


# ── POST /factura-manual/emitir ─────────────────────────────────────
@router.post("/emitir", response_class=JSONResponse)
async def emitir_manual(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    body = await request.json()

    mono_id        = int(body["mono_id"])
    importe        = Decimal(str(body["importe"]))
    concepto       = body.get("concepto", "Honorarios")
    fecha_str      = body["fecha"]
    cliente_nombre = body.get("cliente_nombre", "Consumidor Final")
    cliente_dni    = body.get("cliente_dni", "")
    cliente_email  = body.get("cliente_email", "")
    tipo_cbte_str  = body.get("tipo_cbte", "factura")

    fecha_original = date.fromisoformat(fecha_str)
    cbte_tipo = 13 if tipo_cbte_str == "nc" else 11

    # Aplicar límite de 10 días de ARCA
    from datetime import timedelta
    hoy = date.today()
    min_valida = hoy - timedelta(days=10)
    fecha = max(fecha_original, min_valida)
    if fecha != fecha_original:
        import logging as _log
        _log.getLogger(__name__).info(
            f"Fecha ajustada: {fecha_original} → {fecha} (límite 10 días ARCA)"
        )

    mono = await db.get(Monotributista, mono_id)
    if not mono or mono.tenant_id != current_user.tenant_id:
        return JSONResponse({"ok": False, "error": "Monotributista no encontrado"}, status_code=404)

    if not mono.cert_encrypted:
        return JSONResponse({"ok": False, "error": "El monotributista no tiene certificado configurado"})

    try:
        from app.wsfe import get_token_sign, get_ultimo_cbte as _ultimo, solicitar_cae, load_credentials
        from app.config import FERNET_KEY

        cert_pem, key_pem = load_credentials(mono, FERNET_KEY)
        token, sign = await get_token_sign(
            cert_pem, key_pem,
            environment=mono.afip_environment or "production"
        )
        cuit = mono.cuit.replace("-", "")

        ultimo = await _ultimo(token, sign, cuit, mono.afip_punto_venta, cbte_tipo,
                               environment=mono.afip_environment or "production")
        cbte_nro = ultimo + 1

        doc_tipo = 96 if (cliente_dni and cliente_dni.isdigit()) else 99
        doc_nro  = cliente_dni if (cliente_dni and cliente_dni.isdigit()) else "0"

        last_day = calendar.monthrange(fecha.year, fecha.month)[1]
        fch_desde = fecha.replace(day=1)
        fch_hasta = fecha.replace(day=last_day)

        cae, cae_vto, obs_list = await solicitar_cae(
            token=token, sign=sign,
            cuit=cuit,
            punto_venta=mono.afip_punto_venta,
            cbte_tipo=cbte_tipo,
            cbte_nro=cbte_nro,
            cbte_fecha=fecha,
            imp_total=float(importe),
            concepto=2,
            doc_tipo=doc_tipo,
            doc_nro=doc_nro,
            fch_serv_desde=fch_desde,
            fch_serv_hasta=fch_hasta,
            environment=mono.afip_environment or "production",
        )

        if not cae:
            obs = "; ".join(obs_list) if obs_list else "ARCA rechazó la factura"
            return JSONResponse({"ok": False, "error": obs})

        # Guardar en AfipInvoiceHistory
        from app.afip.history_models import AfipInvoiceHistory
        db.add(AfipInvoiceHistory(
            tenant_id=current_user.tenant_id,
            mono_id=mono_id,
            cbte_tipo=cbte_tipo,
            punto_venta=mono.afip_punto_venta,
            cbte_nro=cbte_nro,
            cbte_fecha=fecha,
            fch_serv_desde=fch_desde,
            fch_serv_hasta=fch_hasta,
            imp_total=importe,
            cae=cae,
            source="manual",
        ))
        await db.commit()

        # Generar PDF
        nombre_emisor = mono.nombre_fantasia or mono.razon_social
        pdf_bytes = generar_factura_pdf(
            razon_social=nombre_emisor,
            cuit_emisor=mono.cuit,
            punto_venta=mono.afip_punto_venta,
            cbte_nro=cbte_nro,
            cbte_tipo=cbte_tipo,
            fecha=fecha,
            fch_serv_desde=fch_desde,
            fch_serv_hasta=fch_hasta,
            importe=float(importe),
            cae=cae,
            cae_vto=cae_vto,
            concepto=concepto,
            domicilio_emisor=mono.domicilio or "",
            ingresos_brutos=None,
            logo_base64=getattr(mono, "logo_base64", None),
            cliente_nombre=cliente_nombre,
            cliente_dni=cliente_dni or None,
            cliente_cuit=None,
        )

        tipo_label = "NC_C" if cbte_tipo == 13 else "Factura_C"
        return JSONResponse({
            "ok": True,
            "cae": cae,
            "cbte_nro": cbte_nro,
            "pdf_b64": base64.b64encode(pdf_bytes).decode(),
            "nombre_archivo": f"{tipo_label}_{mono.afip_punto_venta:04d}_{cbte_nro:08d}.pdf",
        })

    except Exception as e:
        log.error(f"Error emitiendo factura manual: {e}", exc_info=True)
        await db.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
