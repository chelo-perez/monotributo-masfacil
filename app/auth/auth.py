"""
Autenticación JWT para Monotributo Más Fácil.
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
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

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
# Dependencies
# ---------------------------------------------------------------------------

bearer_scheme = HTTPBearer(auto_error=False)

async def _load_user(token: str, db: AsyncSession) -> Optional[CurrentUser]:
    """Carga el usuario desde un token. Devuelve None si es inválido."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
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


async def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    """Para endpoints HTMX — requiere Bearer token en header."""
    if not credentials:
        raise HTTPException(status_code=401, detail="No autenticado")
    user = await _load_user(credentials.credentials, db)
    if not user:
        raise HTTPException(status_code=401, detail="Token inválido")
    return user


async def get_current_user_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    """
    Para páginas HTML completas.
    Lee el token de: cookie 'mmf_session', query param ?token=, o header Authorization.
    Si no encuentra token válido, redirige a /login.
    """
    token = None

    # 1. Cookie
    token = request.cookies.get("mmf_session")

    # 2. Query param
    if not token:
        token = request.query_params.get("token")

    # 3. Header Authorization
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]

    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/login"})

    user = await _load_user(token, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})

    return user


async def get_current_user_page_or_redirect(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Igual que get_current_user_page pero compatible con FastAPI — lanza 401."""
    token = (
        request.cookies.get("mmf_session")
        or request.query_params.get("token")
        or (request.headers.get("Authorization", "")[7:]
            if request.headers.get("Authorization", "").startswith("Bearer ") else None)
    )
    if not token:
        raise HTTPException(status_code=401, detail="No autenticado")
    user = await _load_user(token, db)
    if not user:
        raise HTTPException(status_code=401, detail="Token inválido")
    return user
