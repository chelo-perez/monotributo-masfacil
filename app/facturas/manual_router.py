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
                ClienteFinal.cuit.ilike(f"%{q}%"),
            )
        ).order_by(ClienteFinal.nombre).limit(8)
    )
    clientes = results.scalars().all()
    return JSONResponse([
        {
            "id": c.id,
            "nombre": c.nombre,
            "dni": c.dni or "",
            "cuit": c.cuit or "",
            "email": c.email or "",
        }
        for c in clientes
    ])


# ── POST /factura-manual/enviar-email ───────────────────────────────
@router.post("/enviar-email", response_class=JSONResponse)
async def enviar_factura_email(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Envía una factura ya emitida por email (PDF adjunto) y guarda/actualiza el
    email en el cliente para la próxima vez.
    """
    from app.auth.models import ClienteFinal
    from sqlalchemy import or_ as _or
    body = await request.json()
    email = (body.get("email") or "").strip()
    mono_id = body.get("mono_id")
    pdf_b64 = body.get("pdf_b64") or ""
    nombre_archivo = body.get("nombre_archivo") or "comprobante.pdf"
    comprobante = body.get("comprobante") or "Factura C"
    cliente_nombre = body.get("cliente_nombre") or ""
    cliente_dni = (body.get("cliente_dni") or "").strip()
    cliente_cuit = (body.get("cliente_cuit") or "").strip()

    # Validación mínima de email
    if "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse({"ok": False, "error": "Ingresá un email válido"}, status_code=400)
    if not pdf_b64:
        return JSONResponse({"ok": False, "error": "No hay comprobante para enviar"}, status_code=400)

    mono = await db.get(Monotributista, mono_id) if mono_id else None
    razon_emisor = mono.razon_social if mono else ""

    # Enviar
    from app.email import enviar_factura_pdf
    ok, err = await enviar_factura_pdf(
        to=email, nombre_cliente=cliente_nombre, comprobante=comprobante,
        pdf_b64=pdf_b64, nombre_archivo=nombre_archivo,
        razon_social_emisor=razon_emisor,
    )
    if not ok:
        return JSONResponse({"ok": False, "error": err or "No se pudo enviar"}, status_code=200)

    # Guardar/actualizar el email en el cliente (si lo podemos identificar)
    try:
        if mono_id and (cliente_dni or cliente_cuit or cliente_nombre):
            filtros = []
            if cliente_cuit:
                filtros.append(ClienteFinal.cuit == cliente_cuit)
            if cliente_dni:
                filtros.append(ClienteFinal.dni == cliente_dni)
            if not filtros and cliente_nombre:
                filtros.append(ClienteFinal.nombre == cliente_nombre)
            if filtros:
                q = await db.execute(select(ClienteFinal).where(
                    ClienteFinal.monotributista_id == mono_id,
                    _or(*filtros),
                ).limit(1))
                cli = q.scalar_one_or_none()
                if cli and cli.email != email:
                    cli.email = email
                    await db.commit()
    except Exception:
        await db.rollback()  # guardar el email es best-effort; el envío ya se hizo

    return JSONResponse({"ok": True, "mensaje": f"Comprobante enviado a {email}"})


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
    cliente_cuit   = body.get("cliente_cuit", "")
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

        # Determinar DocTipo/DocNro según datos disponibles
        cuit_limpio = cliente_cuit.replace("-", "").replace(" ", "") if cliente_cuit else ""
        if cuit_limpio and cuit_limpio.isdigit() and len(cuit_limpio) == 11:
            doc_tipo = 80  # CUIT
            doc_nro  = cuit_limpio
        elif cliente_dni and cliente_dni.isdigit():
            doc_tipo = 96  # DNI
            doc_nro  = cliente_dni
        else:
            doc_tipo = 99  # Consumidor Final
            doc_nro  = "0"

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

        # Guardar cliente nuevo si no es CF y no existía en BD
        if cliente_nombre and cliente_nombre != "Consumidor Final":
            from app.auth.models import ClienteFinal
            from sqlalchemy import or_
            existe = None
            if cliente_cuit or cliente_dni:
                filtros = []
                if cliente_cuit:
                    filtros.append(ClienteFinal.cuit == cliente_cuit)
                if cliente_dni:
                    filtros.append(ClienteFinal.dni == cliente_dni)
                q_existe = await db.execute(
                    select(ClienteFinal).where(
                        ClienteFinal.monotributista_id == mono_id,
                        or_(*filtros)
                    )
                )
                existe = q_existe.scalar_one_or_none()
            if not existe:
                db.add(ClienteFinal(
                    monotributista_id=mono_id,
                    tenant_id=current_user.tenant_id,
                    nombre=cliente_nombre,
                    dni=cliente_dni or None,
                    cuit=cliente_cuit or None,
                    email=cliente_email or None,
                ))

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
            cliente_cuit=cliente_cuit or None,
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


# ── GET /factura-manual/consultar-cuit ────────────────────────
@router.get("/consultar-cuit", response_class=JSONResponse)
async def consultar_cuit_arca(
    cuit: str,
    mono_id: int,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Consulta el padrón de ARCA por CUIT y devuelve razón social, condición IVA
    y domicilio.

    Usa el certificado ÚNICO de Más Fácil SAS (tabla padron_config) como
    consultante para TODOS los clientes. Así ningún monotributista necesita
    habilitar el web service de constancia en su propio ARCA — la fricción se
    resuelve una sola vez a nivel plataforma.
    """
    # Validar que el mono pertenece al tenant (seguridad), pero NO usamos su cert
    mono = await db.get(Monotributista, mono_id)
    if not mono or mono.tenant_id != current_user.tenant_id:
        return JSONResponse({"error": "No encontrado"}, status_code=404)

    cuit_limpio = "".join(ch for ch in (cuit or "") if ch.isdigit())
    if len(cuit_limpio) != 11:
        return JSONResponse({"error": "El CUIT debe tener 11 dígitos"}, status_code=400)

    # Cargar el certificado de Más Fácil SAS desde padron_config
    from sqlalchemy import text as _txt
    _pc = await db.execute(_txt(
        "SELECT cert_encrypted, key_encrypted, cuit, environment "
        "FROM padron_config WHERE activo = TRUE ORDER BY updated_at DESC LIMIT 1"
    ))
    _row = _pc.fetchone()
    if not _row:
        return JSONResponse(
            {"error": "La consulta al padrón no está configurada todavía."},
            status_code=200)

    try:
        from app.config import FERNET_KEY
        from app.afip.padron import consultar_constancia
        from cryptography.fernet import Fernet
        _f = Fernet(FERNET_KEY)
        cert_pem = _f.decrypt(_row.cert_encrypted.encode()).decode()
        key_pem  = _f.decrypt(_row.key_encrypted.encode()).decode()
        cuit_rep = (_row.cuit or "").replace("-", "").replace(" ", "")

        r = await consultar_constancia(
            cuit_consulta=cuit_limpio,
            cert_pem=cert_pem,
            key_pem=key_pem,
            cuit_representada=cuit_rep,
            environment=_row.environment or "production",
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"padron consulta fallo: {e}")
        return JSONResponse(
            {"error": "ARCA no respondió. Podés cargar el nombre a mano."},
            status_code=200)

    if getattr(r, "error", None) or not getattr(r, "razon_social", ""):
        return JSONResponse(
            {"error": "No se encontró ese CUIT en el padrón de ARCA"},
            status_code=200)

    _es_mono = getattr(r, "es_monotributo", False)
    _cond_txt = getattr(r, "condicion_iva", "") or (
        "Responsable Monotributo" if _es_mono else "Responsable Inscripto")
    return JSONResponse({
        "razon_social": r.razon_social,
        "condicion_iva": _cond_txt,
        "cond_iva_cod": 6 if _es_mono else 1,
        "domicilio": str(getattr(r, "domicilio_fiscal", "")),
        "cuit": cuit_limpio,
    })


# ── POST /factura-manual/anular ─────────────────────────────────────
@router.post("/anular", response_class=JSONResponse)
async def anular_factura(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Anula una factura del historial emitiendo una Nota de Crédito C (tipo 13).
    La NC se guarda en AfipInvoiceHistory con source='nc'.
    """
    from app.afip.history_models import AfipInvoiceHistory
    from app.wsfe import get_token_sign, get_ultimo_cbte, solicitar_cae, load_credentials
    from app.config import FERNET_KEY
    from datetime import timedelta
    import calendar as _cal

    body = await request.json()
    hist_id  = int(body.get("hist_id", 0))
    mono_id  = int(body.get("mono_id", 0))

    # Cargar el comprobante original del historial
    result = await db.execute(
        select(AfipInvoiceHistory).where(
            AfipInvoiceHistory.id == hist_id,
            AfipInvoiceHistory.tenant_id == current_user.tenant_id,
            AfipInvoiceHistory.mono_id == mono_id,
        )
    )
    hist = result.scalar_one_or_none()
    if not hist:
        return JSONResponse({"ok": False, "error": "Comprobante no encontrado"}, status_code=404)

    if hist.cbte_tipo not in (11, 1, 6):
        return JSONResponse({"ok": False, "error": "Solo se pueden anular facturas (no NC)"})

    NC_TIPOS = {11: 13, 1: 3, 6: 8}
    nc_tipo = NC_TIPOS.get(hist.cbte_tipo, 13)

    # Cargar monotributista y credenciales
    mono = await db.get(Monotributista, mono_id)
    if not mono or mono.tenant_id != current_user.tenant_id:
        return JSONResponse({"ok": False, "error": "Monotributista no encontrado"}, status_code=404)
    if not mono.cert_encrypted:
        return JSONResponse({"ok": False, "error": "Sin certificado configurado"})

    try:
        cert_pem, key_pem = load_credentials(mono, FERNET_KEY)
        token, sign = await get_token_sign(
            cert_pem, key_pem, environment=mono.afip_environment or "production"
        )
        cuit = mono.cuit.replace("-", "")

        # Último número de NC para ese PV
        ultimo_nc = await get_ultimo_cbte(
            token, sign, cuit, mono.afip_punto_venta, nc_tipo,
            environment=mono.afip_environment or "production",
        )
        nc_nro = ultimo_nc + 1

        # Fecha de la NC: la del comprobante original, ajustada al límite de 10 días
        hoy = date.today()
        min_valida = hoy - timedelta(days=10)
        nc_fecha = hist.cbte_fecha or hoy
        if nc_fecha < min_valida:
            nc_fecha = min_valida
        if nc_fecha > hoy:
            nc_fecha = hoy

        # Período de servicio: mes de la NC
        ult_dia = _cal.monthrange(nc_fecha.year, nc_fecha.month)[1]
        fch_desde = nc_fecha.replace(day=1)
        fch_hasta = nc_fecha.replace(day=ult_dia)

        cae, cae_vto, obs_list = await solicitar_cae(
            token=token, sign=sign,
            cuit=cuit,
            punto_venta=mono.afip_punto_venta,
            cbte_tipo=nc_tipo,
            cbte_nro=nc_nro,
            cbte_fecha=nc_fecha,
            imp_total=float(hist.imp_total),
            concepto=2,
            doc_tipo=99,
            doc_nro="0",
            fch_serv_desde=fch_desde,
            fch_serv_hasta=fch_hasta,
            environment=mono.afip_environment or "production",
            # Referencia al comprobante original (requerida por ARCA para NC)
            cbte_asoc_tipo=hist.cbte_tipo,
            cbte_asoc_nro=hist.cbte_nro,
            cbte_asoc_fecha=hist.cbte_fecha,
            cbte_asoc_cuit=cuit,
        )

        if not cae:
            obs = "; ".join(obs_list) if obs_list else "ARCA rechazó la NC"
            return JSONResponse({"ok": False, "error": obs})

        # Guardar NC en historial
        db.add(AfipInvoiceHistory(
            tenant_id=current_user.tenant_id,
            mono_id=mono_id,
            cbte_tipo=nc_tipo,
            punto_venta=mono.afip_punto_venta,
            cbte_nro=nc_nro,
            cbte_fecha=nc_fecha,
            fch_serv_desde=fch_desde,
            fch_serv_hasta=fch_hasta,
            imp_total=hist.imp_total,
            cae=cae,
            source="nc",
            cbte_asoc_nro=hist.cbte_nro,  # referencia exacta a la factura anulada
        ))
        await db.commit()

        return JSONResponse({
            "ok": True,
            "nc_nro": nc_nro,
            "nc_cae": cae,
            "nc_tipo": nc_tipo,
            "mensaje": f"NC C {mono.afip_punto_venta:04d}-{nc_nro:08d} — CAE {cae}",
        })

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error anulando factura: {e}", exc_info=True)
        await db.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
