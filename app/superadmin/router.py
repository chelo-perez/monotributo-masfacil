"""
Superadmin de Monotributo Más Fácil.
Panel de gestión de Estudios Contables (tenants).
Adaptado de Facturo Más Fácil.
"""

from typing import Annotated
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text

from app.database import get_db
from app.auth.auth import get_current_user_page, CurrentUser
from app.auth.models import Tenant, User, Monotributista, PlanEstudio
from app.facturas.models import Factura
from app.templates_config import templates

router = APIRouter(prefix="/admin", tags=["superadmin"])

SECRET_PATH = "mmf-admin-2025"

PLAN_PRICES = {"basico": 30, "estudio": 60, "pro": 100}


async def _require_superadmin(request: Request, db: AsyncSession) -> CurrentUser:
    user = await get_current_user_page(request, db)
    if not isinstance(user, CurrentUser):
        return user  # es un RedirectResponse
    if user.rol != "admin":
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return user


async def _get_tenants_data(db: AsyncSession):
    result = await db.execute(
        select(Tenant)
        .where(Tenant.nombre != "Más Fácil (Admin)")
        .order_by(Tenant.created_at.desc())
    )
    tenants = result.scalars().all()
    rows = []
    for t in tenants:
        owner_q = await db.execute(
            select(User.email).where(User.tenant_id == t.id)
        )
        owner_email = owner_q.scalar_one_or_none() or "—"

        monos_q = await db.execute(
            select(func.count()).select_from(Monotributista)
            .where(Monotributista.tenant_id == t.id, Monotributista.activo == True)
        )
        n_monos = monos_q.scalar() or 0

        facts_q = await db.execute(
            select(func.count()).select_from(Factura)
            .where(Factura.tenant_id == t.id)
        )
        n_facturas = facts_q.scalar() or 0

        estado = "activo" if t.activo else "inactivo"

        rows.append({
            "tenant": t,
            "owner_email": owner_email,
            "n_monos": n_monos,
            "n_facturas": n_facturas,
            "estado": estado,
        })
    return rows


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get(f"/{SECRET_PATH}", response_class=HTMLResponse)
async def superadmin_panel(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    current_user = await _require_superadmin(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    rows = await _get_tenants_data(db)
    now = datetime.now(timezone.utc)

    mrr = sum(
        PLAN_PRICES.get(r["tenant"].plan, 0)
        for r in rows if r["estado"] == "activo"
    )
    mes_inicio = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nuevos_mes = sum(1 for r in rows if r["tenant"].created_at >= mes_inicio)

    return templates.TemplateResponse("superadmin/panel.html", {
        "request": request,
        "current_user": current_user,
        "rows": rows,
        "secret": SECRET_PATH,
        "now": now,
        "kpis": {
            "mrr": mrr,
            "total": len(rows),
            "activos": sum(1 for r in rows if r["estado"] == "activo"),
            "nuevos_mes": nuevos_mes,
        },
    })


# ---------------------------------------------------------------------------
# Lista de estudios contables (HTMX partial)
# ---------------------------------------------------------------------------

@router.get(f"/{SECRET_PATH}/lista", response_class=HTMLResponse)
async def lista_tenants(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    current_user = await _require_superadmin(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    rows = await _get_tenants_data(db)
    return templates.TemplateResponse("superadmin/tenants_list.html", {
        "request": request,
        "rows": rows,
        "secret": SECRET_PATH,
        "now": datetime.now(timezone.utc),
    })


# ---------------------------------------------------------------------------
# Detalle de un estudio contable
# ---------------------------------------------------------------------------

@router.get(f"/{SECRET_PATH}/tenant/{{tenant_id}}", response_class=HTMLResponse)
async def tenant_detail(
    tenant_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    current_user = await _require_superadmin(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    t = await db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Estudio no encontrado")

    users_q = await db.execute(select(User).where(User.tenant_id == t.id))
    users = users_q.scalars().all()

    monos_q = await db.execute(
        select(Monotributista).where(Monotributista.tenant_id == t.id)
        .order_by(Monotributista.razon_social)
    )
    monos = monos_q.scalars().all()

    facts_q = await db.execute(
        select(func.count()).select_from(Factura).where(Factura.tenant_id == t.id)
    )
    n_facturas = facts_q.scalar() or 0

    return templates.TemplateResponse("superadmin/tenant_detail.html", {
        "request": request,
        "current_user": current_user,
        "t": t,
        "users": users,
        "monos": monos,
        "n_facturas": n_facturas,
        "secret": SECRET_PATH,
        "planes": [p.value for p in PlanEstudio],
    })


# ---------------------------------------------------------------------------
# Acciones sobre tenants
# ---------------------------------------------------------------------------

@router.post(f"/{SECRET_PATH}/tenant/{{tenant_id}}/toggle-active", response_class=HTMLResponse)
async def toggle_active(
    tenant_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    current_user = await _require_superadmin(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    t = await db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404)
    t.activo = not t.activo
    await db.commit()
    rows = await _get_tenants_data(db)
    return templates.TemplateResponse("superadmin/tenants_list.html", {
        "request": request, "rows": rows,
        "secret": SECRET_PATH, "now": datetime.now(timezone.utc),
    })


@router.post(f"/{SECRET_PATH}/tenant/{{tenant_id}}/set-plan", response_class=HTMLResponse)
async def set_plan(
    tenant_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    plan: str = Form(...),
):
    current_user = await _require_superadmin(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    t = await db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404)
    t.plan = PlanEstudio(plan)
    await db.commit()
    rows = await _get_tenants_data(db)
    return templates.TemplateResponse("superadmin/tenants_list.html", {
        "request": request, "rows": rows,
        "secret": SECRET_PATH, "now": datetime.now(timezone.utc),
    })


@router.post(f"/{SECRET_PATH}/tenant/{{tenant_id}}/reset-password", response_class=HTMLResponse)
async def reset_password(
    tenant_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    new_password: str = Form(...),
):
    current_user = await _require_superadmin(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    from app.auth.auth import hash_password
    user_q = await db.execute(select(User).where(User.tenant_id == tenant_id))
    user = user_q.scalar_one_or_none()
    if not user:
        raise HTTPException(404)
    user.hashed_password = hash_password(new_password)
    await db.commit()
    return HTMLResponse("<span style='color:#16a34a;font-size:.82rem'>✓ Contraseña actualizada</span>")


@router.post(f"/{SECRET_PATH}/crear-tenant", response_class=HTMLResponse)
async def crear_tenant(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    nombre: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    plan: str = Form("basico"),
):
    current_user = await _require_superadmin(request, db)
    if not isinstance(current_user, CurrentUser):
        return current_user

    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        return HTMLResponse("<div style='color:#b91c1c;font-size:.82rem'>Ya existe un usuario con ese email</div>")

    from app.auth.auth import hash_password

    tenant = Tenant(
        nombre=nombre,
        email_admin=email,
        plan=PlanEstudio(plan),
        activo=True,
    )
    db.add(tenant)
    await db.flush()

    user = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password(password),
        nombre=nombre,
        rol="admin",
        activo=True,
    )
    db.add(user)
    await db.commit()

    rows = await _get_tenants_data(db)
    return templates.TemplateResponse("superadmin/tenants_list.html", {
        "request": request, "rows": rows,
        "secret": SECRET_PATH, "now": datetime.now(timezone.utc),
    })
