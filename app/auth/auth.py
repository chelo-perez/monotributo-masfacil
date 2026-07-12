"""
Autenticación JWT para Monotributo Más Fácil.
Usa SessionMiddleware de Starlette — igual que Facturo Más Fácil.
El token se guarda en request.session["access_token"].
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS
from app.database import get_db
from app.auth.models import User, Tenant

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode()[:72], hashed.encode())
    except Exception:
        return False

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

ALGORITHM = "HS256"

def create_access_token(user_id: int, tenant_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "tenant": tenant_id, "exp": expire, "type": "access"},
        SECRET_KEY, algorithm=ALGORITHM,
    )

def create_refresh_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "exp": expire, "type": "refresh"},
        SECRET_KEY, algorithm=ALGORITHM,
    )

def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

# ---------------------------------------------------------------------------
# CurrentUser
# ---------------------------------------------------------------------------

class CurrentUser:
    def __init__(self, user: User, tenant: Tenant):
        self.id = user.id
        self.email = user.email
        self.nombre = user.nombre
        self.rol = user.rol
        self.tenant_id = tenant.id
        self.tenant_nombre = tenant.nombre
        self.plan = tenant.plan

# ---------------------------------------------------------------------------
# Core: cargar usuario desde token
# ---------------------------------------------------------------------------

async def _load_user_from_token(token: str, db: AsyncSession) -> Optional[CurrentUser]:
    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])
    except Exception:
        return None

    result = await db.execute(
        select(User, Tenant)
        .join(Tenant, User.tenant_id == Tenant.id)
        .where(User.id == user_id, User.activo == True)
    )
    row = result.one_or_none()
    if not row:
        return None

    user, tenant = row
    if not tenant.activo:
        return None

    return CurrentUser(user, tenant)

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

bearer_scheme = HTTPBearer(auto_error=False)

async def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    """Para endpoints HTMX — requiere Bearer token en header."""
    if not credentials:
        raise HTTPException(status_code=401, detail="No autenticado")
    user = await _load_user_from_token(credentials.credentials, db)
    if not user:
        raise HTTPException(status_code=401, detail="Token inválido")
    return user


async def get_current_user_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)] = None,
) -> CurrentUser:
    """
    Para páginas HTML completas. Igual que Facturo Más Fácil:
    1. Si hay Bearer token (HTMX) → usarlo
    2. Si hay token en request.session → usarlo
    3. Si no hay nada → redirect a /login
    """
    is_htmx = bool(credentials or request.headers.get("HX-Request"))

    token = None
    if credentials:
        token = credentials.credentials
    else:
        token = request.session.get("access_token")

    if token:
        user = await _load_user_from_token(token, db)
        if user:
            return user

    if is_htmx:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    # Navegación directa sin sesión → lanzar 401 (el exception handler lo convierte en redirect)
    raise HTTPException(status_code=401, detail="No autenticado")
