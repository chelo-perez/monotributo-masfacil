"""
Auth router para Monotributo Más Fácil.
Usa SessionMiddleware — igual que Facturo Más Fácil.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.auth.models import User, Tenant
from app.auth.auth import (
    verify_password, create_access_token, create_refresh_token, decode_token,
)
from app.templates_config import templates

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("auth/login.html", {"request": request})


@router.post("/auth/login-form")
async def login_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Login por form HTML. Guarda token en session y redirige al dashboard."""
    form = await request.form()
    email = str(form.get("email", "")).lower().strip()
    password = str(form.get("password", ""))

    result = await db.execute(
        select(User, Tenant)
        .join(Tenant, User.tenant_id == Tenant.id)
        .where(User.email == email)
    )
    row = result.one_or_none()

    error = None
    user = tenant = None
    if not row:
        error = "Email o contraseña incorrectos"
    else:
        user, tenant = row
        if not verify_password(password, user.hashed_password):
            error = "Email o contraseña incorrectos"
        elif not user.activo:
            error = "Usuario inactivo"
        elif not tenant.activo:
            error = "Cuenta suspendida"

    if error:
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "error": error,
        }, status_code=401)

    access_token = create_access_token(user.id, tenant.id)

    # Guardar en sesión — igual que Facturo Más Fácil
    request.session["access_token"] = access_token

    return RedirectResponse("/dashboard", status_code=303)


@router.post("/auth/login")
async def login_json(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Login JSON para llamadas fetch/HTMX."""
    result = await db.execute(
        select(User, Tenant)
        .join(Tenant, User.tenant_id == Tenant.id)
        .where(User.email == body.email.lower().strip())
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos")

    user, tenant = row
    if not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos")
    if not user.activo:
        raise HTTPException(status_code=403, detail="Usuario inactivo")
    if not tenant.activo:
        raise HTTPException(status_code=403, detail="Cuenta suspendida")

    access_token = create_access_token(user.id, tenant.id)
    refresh_token = create_refresh_token(user.id)

    request.session["access_token"] = access_token

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


@router.post("/auth/refresh")
async def refresh(
    body: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        payload = decode_token(body.refresh_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token inválido")

    user_id = int(payload["sub"])
    result = await db.execute(
        select(User).where(User.id == user_id, User.activo == True)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")

    return {"access_token": create_access_token(user.id, user.tenant_id), "token_type": "bearer"}


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
