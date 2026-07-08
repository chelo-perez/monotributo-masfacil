"""
Endpoints de autenticación para Monotributo Más Fácil.

POST /auth/login        → devuelve access_token + refresh_token (JSON)
POST /auth/refresh      → renueva access_token con refresh_token
GET  /auth/logout       → redirige a /login (limpia cookie si existe)
GET  /login             → página de login
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.auth.models import User, Tenant
from app.auth.auth import (
    verify_password, create_access_token, create_refresh_token,
    decode_token, hash_password,
)
from app.templates_config import templates

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# Página de login
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("auth/login.html", {"request": request})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.post("/auth/login")
async def login(
    body: LoginRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Buscar usuario
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
        raise HTTPException(status_code=403, detail="Cuenta suspendida. Contactá soporte.")

    access_token = create_access_token(user.id, tenant.id)
    refresh_token = create_refresh_token(user.id)

    # Cookie para endpoints de página (PDFs, reportes)
    response.set_cookie(
        "mmf_session", access_token,
        httponly=True, secure=True, samesite="lax", max_age=60 * 60 * 8,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "nombre": user.nombre,
            "email": user.email,
            "tenant_nombre": tenant.nombre,
            "plan": tenant.plan,
        }
    }


@router.post("/auth/refresh")
async def refresh(
    body: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token inválido")

    user_id = int(payload["sub"])
    result = await db.execute(
        select(User).where(User.id == user_id, User.activo == True)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")

    new_token = create_access_token(user.id, user.tenant_id)
    return {"access_token": new_token, "token_type": "bearer"}


@router.get("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("mmf_session")
    return RedirectResponse("/login", status_code=302)
