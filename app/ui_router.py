"""
Router principal de UI para Monotributo Más Fácil.
Todos los endpoints que devuelven HTML.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.auth import CurrentUser, get_current_user, get_current_user_page
from app.auth.models import Monotributista, Tenant
from app.database import get_db
from app.excel.importer import importar_excel
from app.facturas.models import LoteEmision, FilaExcel, Factura, EstadoLote, EstadoFactura
from app.templates_config import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Admin → redirigir al panel de gestión
    if current_user.rol == "admin" and current_user.tenant_nombre == "Más Fácil (Admin)":
        return RedirectResponse("/admin/mmf-admin-2025", status_code=302)
    hoy = date.today()
    mes_nombre = [
        "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
    ][hoy.month]
    mes_actual = f"{mes_nombre} {hoy.year}"

    # Monotributistas activos
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.tenant_id == current_user.tenant_id,
            Monotributista.activo == True,
        ).order_by(Monotributista.razon_social)
    )
    monos = result.scalars().all()

    # Facturas del mes actual
    result_facts = await db.execute(
        select(Factura).where(
            Factura.tenant_id == current_user.tenant_id,
            func.extract("month", Factura.cbte_fecha) == hoy.month,
            func.extract("year", Factura.cbte_fecha) == hoy.year,
            Factura.anulada == False,
        )
    )
    facturas_mes = result_facts.scalars().all()

    aprobadas_mes = [f for f in facturas_mes if f.afip_result == EstadoFactura.aprobada]
    rechazadas_mes = [f for f in facturas_mes if f.afip_result == EstadoFactura.rechazada]
    total_mes = sum(f.imp_total for f in aprobadas_mes)

    # Facturas por monotributista este mes (para la columna de la tabla)
    facts_por_mono: dict[int, dict] = {}
    for f in facturas_mes:
        if f.monotributista_id not in facts_por_mono:
            facts_por_mono[f.monotributista_id] = {"aprobadas": 0, "rechazadas": 0}
        if f.afip_result == EstadoFactura.aprobada:
            facts_por_mono[f.monotributista_id]["aprobadas"] += 1
        elif f.afip_result == EstadoFactura.rechazada:
            facts_por_mono[f.monotributista_id]["rechazadas"] += 1

    # Construir datos para la tabla (con semáforo simulado por ahora)
    # TODO: integrar monotributo service real con AfipInvoiceHistory
    TOPES_CATEGORIA = {
        "A": 3_700_000, "B": 5_550_000, "C": 7_400_000,
        "D": 9_250_000, "E": 11_100_000, "F": 13_500_000,
        "G": 16_200_000, "H": 19_300_000, "I": 22_700_000,
        "J": 26_500_000, "K": 30_000_000,
    }

    monos_data = []
    alertas = 0
    sin_certificado = 0

    for m in monos:
        cat = m.categoria_actual.value if m.categoria_actual else "A"
        tope = TOPES_CATEGORIA.get(cat, 3_700_000)
        # Acumulado: suma de facturas aprobadas de este mono en los últimos 12 meses
        result_ac = await db.execute(
            select(func.coalesce(func.sum(Factura.imp_total), 0)).where(
                Factura.monotributista_id == m.id,
                Factura.afip_result == EstadoFactura.aprobada,
                Factura.anulada == False,
                Factura.fch_serv_desde >= date(hoy.year - 1, hoy.month, 1),
            )
        )
        acumulado = float(result_ac.scalar() or 0)
        pct = min((acumulado / tope * 100), 100) if tope else 0

        if pct >= 90:
            alertas += 1

        if not m.cert_encrypted:
            sin_certificado += 1

        facts_m = facts_por_mono.get(m.id, {"aprobadas": 0, "rechazadas": 0})

        monos_data.append({
            "id": m.id,
            "razon_social": m.razon_social,
            "cuit": m.cuit,
            "categoria": cat,
            "acumulado": acumulado,
            "tope": tope,
            "pct_tope": round(pct, 1),
            "aprobadas_mes": facts_m["aprobadas"],
            "rechazadas_mes": facts_m["rechazadas"],
        })

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "current_user": current_user,
        "active_page": "dashboard",
        "tenant_nombre": current_user.tenant_nombre,
        "mes_actual": mes_actual,
        "total_monotributistas": len(monos),
        "sin_certificado": sin_certificado,
        "facturas_mes": len(aprobadas_mes),
        "facturas_rechazadas": len(rechazadas_mes),
        "total_mes": total_mes,
        "alertas": alertas,
        "monotributistas": monos_data,
    })


# ---------------------------------------------------------------------------
# Upload Excel → Preview
# ---------------------------------------------------------------------------

@router.get("/emitir", response_class=HTMLResponse)
async def emitir_page(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
):
    return templates.TemplateResponse("emitir.html", {
        "request": request,
        "current_user": current_user,
        "active_page": "emitir",
        "tenant_nombre": current_user.tenant_nombre,
    })


@router.post("/emitir/upload", response_class=HTMLResponse)
async def upload_excel(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
    archivo: UploadFile = File(...),
):
    """Recibe el Excel, lo importa y devuelve el preview."""
    if not archivo.filename.endswith((".xlsx", ".xls")):
        return templates.TemplateResponse("partials/upload_error.html", {
            "request": request,
            "error": "El archivo debe ser Excel (.xlsx o .xls)",
        })

    contenido = await archivo.read()
    if len(contenido) > 5 * 1024 * 1024:  # 5 MB máximo
        return templates.TemplateResponse("partials/upload_error.html", {
            "request": request,
            "error": "El archivo es demasiado grande (máximo 5 MB)",
        })

    resultado = await importar_excel(
        file_bytes=contenido,
        nombre_archivo=archivo.filename,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        db=db,
    )

    if resultado.errores_globales:
        return templates.TemplateResponse("partials/upload_error.html", {
            "request": request,
            "errores": resultado.errores_globales,
        })

    # Cargar filas del lote para el preview
    result = await db.execute(
        select(FilaExcel).where(FilaExcel.lote_id == resultado.lote_id)
        .order_by(FilaExcel.monotributista_id, FilaExcel.fila_numero)
    )
    filas = result.scalars().all()

    # Agrupar por monotributista para el template
    grupos_dict: dict[str, dict] = {}
    for fila in filas:
        key = fila.monotributista_raw
        if key not in grupos_dict:
            grupos_dict[key] = {
                "razon_social": key,
                "cuit": "",
                "filas_validas": 0,
                "filas_con_error": 0,
                "total_importe": 0,
                "filas": [],
            }
        g = grupos_dict[key]
        g["filas"].append(fila)
        if fila.valida:
            g["filas_validas"] += 1
            g["total_importe"] += float(fila.importe_resuelto or 0)
        else:
            g["filas_con_error"] += 1

    # Completar CUIT desde el resumen del import
    for r in resultado.por_monotributista:
        if r.razon_social in grupos_dict:
            grupos_dict[r.razon_social]["cuit"] = r.cuit

    return templates.TemplateResponse("preview_emision.html", {
        "request": request,
        "current_user": current_user,
        "lote_id": resultado.lote_id,
        "nombre_archivo": archivo.filename,
        "filas_validas": resultado.filas_validas,
        "filas_con_error": resultado.filas_con_error,
        "grupos": list(grupos_dict.values()),
    })


# ---------------------------------------------------------------------------
# Confirmar emisión
# ---------------------------------------------------------------------------

@router.post("/lotes/{lote_id}/emitir", response_class=HTMLResponse)
async def confirmar_emision(
    lote_id: int,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Dispara la emisión del lote y devuelve la página de resultado."""
    # Verificar que el lote pertenece al tenant
    result = await db.execute(
        select(LoteEmision).where(
            LoteEmision.id == lote_id,
            LoteEmision.tenant_id == current_user.tenant_id,
            LoteEmision.estado == EstadoLote.borrador,
        )
    )
    lote = result.scalar_one_or_none()
    if not lote:
        raise HTTPException(status_code=404, detail="Lote no encontrado")

    # Importar el módulo wsfe y la config
    from app.config import FERNET_KEY
    try:
        from app import wsfe as wsfe_module
    except ImportError:
        # En desarrollo sin módulo AFIP real: simular
        wsfe_module = None

    if wsfe_module is None:
        # Modo demo: marcar todas las filas válidas como aprobadas
        result_filas = await db.execute(
            select(FilaExcel).where(
                FilaExcel.lote_id == lote_id,
                FilaExcel.valida == True,
            )
        )
        filas = result_filas.scalars().all()
        aprobadas = 0
        for fila in filas:
            from app.facturas.models import Factura, EstadoFactura
            factura = Factura(
                tenant_id=current_user.tenant_id,
                lote_id=lote_id,
                monotributista_id=fila.monotributista_id,
                cliente_id=fila.cliente_id,
                fila_excel_id=fila.id,
                cbte_tipo=11,
                cbte_nro=9999,
                punto_venta=1,
                cbte_fecha=fila.fecha_resuelta or date.today(),
                imp_total=fila.importe_resuelto,
                concepto=fila.concepto_raw,
                cae="DEMO00000000000",
                afip_result=EstadoFactura.aprobada,
            )
            db.add(factura)
            aprobadas += 1

        from app.facturas.models import EstadoLote
        lote.estado = EstadoLote.completado
        lote.aprobadas = aprobadas
        lote.rechazadas = 0
        await db.commit()

        return templates.TemplateResponse("resultado_emision.html", {
            "request": request,
            "current_user": current_user,
            "tenant_nombre": current_user.tenant_nombre,
            "lote_id": lote_id,
            "aprobadas": aprobadas,
            "rechazadas": 0,
            "modo_demo": True,
        })

    # Producción: emisión real en paralelo
    from app.facturas.emission import emitir_lote
    resultado = await emitir_lote(
        lote_id=lote_id,
        tenant_id=current_user.tenant_id,
        db=db,
        wsfe_module=wsfe_module,
        fernet_key=FERNET_KEY,
    )

    # Email de resumen al contador (no bloquea la respuesta)
    try:
        from app.email import enviar_resumen_lote
        await enviar_resumen_lote(
            to_email=current_user.email,
            nombre_contador=current_user.nombre or current_user.email,
            lote_id=lote_id,
            aprobadas=resultado.total_aprobadas,
            rechazadas=resultado.total_rechazadas,
            por_monotributista=resultado.por_monotributista,
        )
    except Exception as e:
        print(f"[email] Error al enviar resumen: {e}")

    return templates.TemplateResponse("resultado_emision.html", {
        "request": request,
        "current_user": current_user,
        "tenant_nombre": current_user.tenant_nombre,
        "lote_id": lote_id,
        "aprobadas": resultado.total_aprobadas,
        "rechazadas": resultado.total_rechazadas,
        "por_monotributista": resultado.por_monotributista,
        "duracion": resultado.duracion_segundos,
        "modo_demo": False,
    })


# ---------------------------------------------------------------------------
# Monotributistas
# ---------------------------------------------------------------------------

@router.get("/monotributistas", response_class=HTMLResponse)
async def lista_monotributistas(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.tenant_id == current_user.tenant_id,
        ).order_by(Monotributista.razon_social)
    )
    monos = result.scalars().all()

    return templates.TemplateResponse("monotributistas/lista.html", {
        "request": request,
        "current_user": current_user,
        "active_page": "monotributistas",
        "tenant_nombre": current_user.tenant_nombre,
        "monotributistas": monos,
    })


@router.post("/monotributistas/consultar-cuit", response_class=HTMLResponse)
async def consultar_cuit_arca(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Consulta el CUIT en ARCA y devuelve datos para pre-llenar el formulario."""
    form = await request.form()
    cuit_raw = str(form.get("cuit", "")).strip().replace("-", "").replace(" ", "")

    if not cuit_raw or len(cuit_raw) != 11:
        return HTMLResponse("""
            <div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;padding:10px 14px;
                        font-size:13px;color:#991B1B;margin-top:8px">
                ⚠️ CUIT inválido. Ingresá los 11 dígitos sin guiones.
            </div>""")

    # Buscar un monotributista con certificado para hacer la consulta
    from app.config import FERNET_KEY
    from app.wsfe import load_credentials
    from app.afip.padron import consultar_constancia

    result = await db.execute(
        select(Monotributista).where(
            Monotributista.tenant_id == current_user.tenant_id,
            Monotributista.activo == True,
            Monotributista.cert_encrypted.is_not(None),
        ).limit(1)
    )
    consultante = result.scalar_one_or_none()

    if not consultante:
        return HTMLResponse("""
            <div style="background:#FEF9C3;border:1px solid #F59E0B;border-radius:8px;padding:10px 14px;
                        font-size:13px;color:#854D0E;margin-top:8px">
                ⚠️ Necesitás tener al menos un monotributista con certificado ARCA configurado
                para poder consultar el padrón. Completá los datos manualmente por ahora.
            </div>""")

    try:
        cert_pem, key_pem = load_credentials(consultante, FERNET_KEY)
        cuit_representada = consultante.cuit.replace("-", "")
        constancia = await consultar_constancia(
            cuit_consulta=cuit_raw,
            cuit_representada=cuit_representada,
            cert_pem=cert_pem,
            key_pem=key_pem,
            environment=consultante.afip_environment or "production",
        )
    except Exception as e:
        return HTMLResponse(f"""
            <div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;padding:10px 14px;
                        font-size:13px;color:#991B1B;margin-top:8px">
                ⚠️ Error consultando ARCA: {e}
            </div>""")

    if constancia.error:
        return HTMLResponse(f"""
            <div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;padding:10px 14px;
                        font-size:13px;color:#991B1B;margin-top:8px">
                ⚠️ ARCA: {constancia.error}
            </div>""")

    dom_str = str(constancia.domicilio_fiscal) if constancia.domicilio_fiscal else ""
    cat = constancia.categoria_monotributo or ""
    actividad = constancia.actividades[0] if constancia.actividades else ""
    estado_color = "#166534" if constancia.estado_clave == "ACTIVO" else "#991B1B"

    return HTMLResponse(f"""
        <div style="background:#DCFCE7;border:1px solid #86EFAC;border-radius:8px;padding:10px 14px;
                    font-size:13px;color:#166534;margin-top:8px;margin-bottom:4px">
            ✓ CUIT encontrado en ARCA —
            <span style="color:{estado_color};font-weight:700">{constancia.estado_clave}</span>
        </div>
        <script>
            document.getElementById('razon_social').value = {constancia.razon_social!r};
            document.getElementById('domicilio').value = {dom_str!r};
            document.getElementById('actividad').value = {actividad!r};
            var catSelect = document.getElementById('categoria_actual');
            if (catSelect && {cat!r}) {{
                for (var i=0; i<catSelect.options.length; i++) {{
                    if (catSelect.options[i].value === {cat!r}) {{
                        catSelect.selectedIndex = i; break;
                    }}
                }}
            }}
        </script>
    """)


@router.get("/monotributistas/nuevo", response_class=HTMLResponse)
async def nuevo_monotributista_page(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Verificar límite del plan
    from app.config import PLAN_LIMITES
    result = await db.execute(
        select(func.count()).where(
            Monotributista.tenant_id == current_user.tenant_id,
            Monotributista.activo == True,
        )
    )
    total = result.scalar()
    limite = PLAN_LIMITES.get(current_user.plan, 10)

    if total >= limite:
        return templates.TemplateResponse("monotributistas/limite_alcanzado.html", {
            "request": request,
            "current_user": current_user,
            "tenant_nombre": current_user.tenant_nombre,
            "total": total,
            "limite": limite,
            "plan": current_user.plan,
        })

    return templates.TemplateResponse("monotributistas/nuevo.html", {
        "request": request,
        "current_user": current_user,
        "active_page": "monotributistas",
        "tenant_nombre": current_user.tenant_nombre,
        "categorias": ["A","B","C","D","E","F","G","H","I","J","K"],
    })


@router.post("/monotributistas/nuevo")
async def crear_monotributista(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    form = await request.form()

    # Formatear CUIT
    cuit_raw = str(form.get("cuit", "")).replace("-", "").replace(" ", "")
    if len(cuit_raw) == 11:
        cuit = f"{cuit_raw[:2]}-{cuit_raw[2:10]}-{cuit_raw[10]}"
    else:
        cuit = cuit_raw

    mono = Monotributista(
        tenant_id=current_user.tenant_id,
        cuit=cuit,
        razon_social=str(form.get("razon_social", "")).strip(),
        nombre_fantasia=str(form.get("nombre_fantasia", "")).strip() or None,
        logo_base64=str(form.get("logo_base64", "")).strip() or None,
        domicilio=str(form.get("domicilio", "")).strip() or None,
        email=str(form.get("email", "")).strip() or None,
        afip_punto_venta=None,  # Se detecta automáticamente al cargar el certificado
        categoria_actual=form.get("categoria_actual") or None,
        actividad=str(form.get("actividad", "")).strip() or None,
    )
    db.add(mono)
    await db.commit()
    await db.refresh(mono)

    return RedirectResponse(f"/monotributistas/{mono.id}", status_code=303)


@router.get("/monotributistas/{mono_id}", response_class=HTMLResponse)
async def detalle_monotributista(
    mono_id: int,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.id == mono_id,
            Monotributista.tenant_id == current_user.tenant_id,
        )
    )
    mono = result.scalar_one_or_none()
    if not mono:
        raise HTTPException(status_code=404, detail="Monotributista no encontrado")

    # Últimas facturas
    result_facts = await db.execute(
        select(Factura).options(
            selectinload(Factura.cliente),
        ).where(
            Factura.monotributista_id == mono_id,
            Factura.anulada == False,
        ).order_by(Factura.cbte_fecha.desc()).limit(20)
    )
    facturas = result_facts.scalars().all()

    # Acumulado anual
    hoy = date.today()
    result_ac = await db.execute(
        select(func.coalesce(func.sum(Factura.imp_total), 0)).where(
            Factura.monotributista_id == mono_id,
            Factura.afip_result == EstadoFactura.aprobada,
            Factura.anulada == False,
            Factura.fch_serv_desde >= date(hoy.year - 1, hoy.month, 1),
        )
    )
    acumulado = float(result_ac.scalar() or 0)

    TOPES = {
        "A": 3_700_000, "B": 5_550_000, "C": 7_400_000,
        "D": 9_250_000, "E": 11_100_000, "F": 13_500_000,
        "G": 16_200_000, "H": 19_300_000, "I": 22_700_000,
        "J": 26_500_000, "K": 30_000_000,
    }
    cat = mono.categoria_actual.value if mono.categoria_actual else "A"
    tope = TOPES.get(cat, 3_700_000)
    pct = min((acumulado / tope * 100), 100) if tope else 0

    return templates.TemplateResponse("monotributistas/detalle.html", {
        "request": request,
        "current_user": current_user,
        "active_page": "monotributistas",
        "tenant_nombre": current_user.tenant_nombre,
        "mono": mono,
        "facturas": facturas,
        "acumulado": acumulado,
        "tope": tope,
        "pct_tope": round(pct, 1),
        "categoria": cat,
    })


# ---------------------------------------------------------------------------
# Facturas emitidas
# ---------------------------------------------------------------------------

@router.get("/facturas", response_class=HTMLResponse)
async def lista_facturas(
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
    mono_id: int = None,
    estado: str = None,
    page: int = 1,
):
    limit = 50
    offset = (page - 1) * limit

    q = select(Factura).options(
        selectinload(Factura.monotributista),
        selectinload(Factura.cliente),
    ).where(
        Factura.tenant_id == current_user.tenant_id,
        Factura.anulada == False,
    )
    if mono_id:
        q = q.where(Factura.monotributista_id == mono_id)
    if estado:
        q = q.where(Factura.afip_result == EstadoFactura(estado))

    q = q.order_by(Factura.cbte_fecha.desc()).limit(limit).offset(offset)
    result = await db.execute(q)
    facturas = result.scalars().all()

    # Lista de monotributistas para el filtro
    result_monos = await db.execute(
        select(Monotributista).where(
            Monotributista.tenant_id == current_user.tenant_id,
            Monotributista.activo == True,
        ).order_by(Monotributista.razon_social)
    )
    monos = result_monos.scalars().all()

    return templates.TemplateResponse("facturas/lista.html", {
        "request": request,
        "current_user": current_user,
        "tenant_nombre": current_user.tenant_nombre,
        "active_page": "facturas",
        "facturas": facturas,
        "monotributistas": monos,
        "page": page,
        "mono_id": mono_id,
        "estado_filtro": estado,
    })


@router.post("/monotributistas/{mono_id}/generar-csr")
async def generar_csr(
    mono_id: int,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Genera un par RSA + CSR listo para subir a ARCA."""
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.id == mono_id,
            Monotributista.tenant_id == current_user.tenant_id,
        )
    )
    mono = result.scalar_one_or_none()
    if not mono:
        raise HTTPException(status_code=404)
    if not mono.cuit:
        raise HTTPException(status_code=400, detail="El monotributista no tiene CUIT configurado")

    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    # Generar clave privada RSA 2048 (requerido por ARCA)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    cuit_limpio = mono.cuit.replace("-", "")
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
            x509.NameAttribute(NameOID.COMMON_NAME, cuit_limpio),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit_limpio}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, mono.razon_social),
        ]))
        .sign(private_key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)

    # Guardar la .key encriptada en BD — se usará cuando el contador suba el .crt
    from app.config import FERNET_KEY
    from app.wsfe import encrypt_credentials
    if FERNET_KEY:
        from cryptography.fernet import Fernet
        f = Fernet(FERNET_KEY)
        mono.key_encrypted = f.encrypt(key_pem.decode().encode()).decode()
        await db.commit()

    # Devolver ambos archivos para descarga
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "csr_pem": csr_pem.decode("utf-8"),
        "key_pem": key_pem.decode("utf-8"),
        "nombre_csr": f"csr_{cuit_limpio}.csr",
        "nombre_key": f"clave_{cuit_limpio}.key",
    })


# ---------------------------------------------------------------------------
# Certificado ARCA
# ---------------------------------------------------------------------------

@router.get("/monotributistas/{mono_id}/certificado", response_class=HTMLResponse)
async def certificado_page(
    mono_id: int,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.id == mono_id,
            Monotributista.tenant_id == current_user.tenant_id,
        )
    )
    mono = result.scalar_one_or_none()
    if not mono:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse("monotributistas/certificado.html", {
        "request": request,
        "current_user": current_user,
        "tenant_nombre": current_user.tenant_nombre,
        "mono": mono,
    })


@router.post("/monotributistas/{mono_id}/certificado")
async def guardar_certificado(
    mono_id: int,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cert_file: UploadFile = File(...),
):
    """Solo recibe el .crt de ARCA. La .key la guardamos al generar el CSR."""
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.id == mono_id,
            Monotributista.tenant_id == current_user.tenant_id,
        )
    )
    mono = result.scalar_one_or_none()
    if not mono:
        raise HTTPException(status_code=404)

    cert_bytes = await cert_file.read()

    if b"CERTIFICATE" not in cert_bytes:
        raise HTTPException(status_code=400, detail="El archivo .crt no parece un certificado PEM válido")

    from app.config import FERNET_KEY
    from app.wsfe import encrypt_credentials, get_token_sign, get_puntos_venta
    from cryptography.fernet import Fernet

    cert_pem = cert_bytes.decode("utf-8", errors="replace")

    # Recuperar la .key que guardamos al generar el CSR
    if not mono.key_encrypted:
        raise HTTPException(status_code=400,
            detail="No encontramos la clave privada. Volvé al paso 1 y generá el CSR nuevamente.")

    f = Fernet(FERNET_KEY)
    key_pem = f.decrypt(mono.key_encrypted.encode()).decode()

    # Guardar el .crt encriptado junto con la .key
    cert_enc, key_enc = encrypt_credentials(cert_pem, key_pem, FERNET_KEY)
    mono.cert_encrypted = cert_enc
    mono.key_encrypted  = key_enc

    # Detectar puntos de venta automáticamente
    pvs_detectados = []
    error_pv = None
    try:
        token, sign = await get_token_sign(
            cert_pem, key_pem,
            environment=mono.afip_environment or "production"
        )
        cuit_limpio = mono.cuit.replace("-", "")
        pvs_detectados = await get_puntos_venta(
            token, sign, cuit_limpio,
            environment=mono.afip_environment or "production"
        )
        if len(pvs_detectados) == 1:
            mono.afip_punto_venta = pvs_detectados[0]
    except Exception as e:
        error_pv = str(e)

    await db.commit()

    # Si hay más de un PV → mostrar selector
    if len(pvs_detectados) > 1:
        return templates.TemplateResponse("monotributistas/seleccionar_pv.html", {
            "request": request,
            "current_user": current_user,
            "tenant_nombre": current_user.tenant_nombre,
            "mono": mono,
            "pvs": pvs_detectados,
        })

    return RedirectResponse(f"/monotributistas/{mono_id}", status_code=303)


@router.post("/monotributistas/{mono_id}/set-pv")
async def set_punto_venta(
    mono_id: int,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    form = await request.form()
    pv = int(form.get("punto_venta", 1))
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.id == mono_id,
            Monotributista.tenant_id == current_user.tenant_id,
        )
    )
    mono = result.scalar_one_or_none()
    if not mono:
        raise HTTPException(status_code=404)
    mono.afip_punto_venta = pv
    await db.commit()
    return RedirectResponse(f"/monotributistas/{mono_id}", status_code=303)


# ---------------------------------------------------------------------------
# Editar monotributista
# ---------------------------------------------------------------------------

@router.get("/monotributistas/{mono_id}/editar", response_class=HTMLResponse)
async def editar_page(
    mono_id: int,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.id == mono_id,
            Monotributista.tenant_id == current_user.tenant_id,
        )
    )
    mono = result.scalar_one_or_none()
    if not mono:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse("monotributistas/editar.html", {
        "request": request,
        "current_user": current_user,
        "active_page": "monotributistas",
        "tenant_nombre": current_user.tenant_nombre,
        "mono": mono,
        "categorias": ["A","B","C","D","E","F","G","H","I","J","K"],
    })


@router.post("/monotributistas/{mono_id}/editar")
async def guardar_edicion(
    mono_id: int,
    request: Request,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.id == mono_id,
            Monotributista.tenant_id == current_user.tenant_id,
        )
    )
    mono = result.scalar_one_or_none()
    if not mono:
        raise HTTPException(status_code=404)

    form = await request.form()

    cuit_raw = str(form.get("cuit", "")).replace("-", "").replace(" ", "")
    if len(cuit_raw) == 11:
        mono.cuit = f"{cuit_raw[:2]}-{cuit_raw[2:10]}-{cuit_raw[10]}"

    mono.razon_social      = str(form.get("razon_social", mono.razon_social)).strip()
    logo_b64 = str(form.get("logo_base64", "")).strip()
    if logo_b64:
        mono.logo_base64 = logo_b64
    mono.domicilio         = str(form.get("domicilio", "")).strip() or None
    mono.email             = str(form.get("email", "")).strip() or None
    mono.telefono          = str(form.get("telefono", "")).strip() or None
    mono.actividad         = str(form.get("actividad", "")).strip() or None
    mono.afip_punto_venta  = int(form.get("afip_punto_venta", mono.afip_punto_venta or 1))

    cat = str(form.get("categoria_actual", "")).strip()
    if cat:
        from app.auth.models import CategoriaMonotributo
        mono.categoria_actual = CategoriaMonotributo(cat)
    else:
        mono.categoria_actual = None

    await db.commit()
    return RedirectResponse(f"/monotributistas/{mono_id}", status_code=303)


@router.post("/monotributistas/{mono_id}/desactivar")
async def desactivar_monotributista(
    mono_id: int,
    current_user: Annotated[CurrentUser, Depends(get_current_user_page)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Monotributista).where(
            Monotributista.id == mono_id,
            Monotributista.tenant_id == current_user.tenant_id,
        )
    )
    mono = result.scalar_one_or_none()
    if not mono:
        raise HTTPException(status_code=404)

    mono.activo = False
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Redirect raíz → dashboard
# ---------------------------------------------------------------------------

@router.get("/")
async def root():
    return RedirectResponse("/dashboard", status_code=302)
