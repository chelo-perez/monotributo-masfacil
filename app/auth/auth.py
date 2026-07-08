"""
Autenticación JWT para Monotributo Más Fácil.
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Query, Request
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
# Dataclass del usuario actual
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

async def _load_user(token: str, db: AsyncSession) -> CurrentUser:
    payload = decode_token(token)
    user_id = int(payload["sub"])

    result = await db.execute(
        select(User, Tenant)
        .join(Tenant, User.tenant_id == Tenant.id)
        .where(User.id == user_id, User.activo == True)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=401, detail="Usuario no encontrado o inactivo")

    user, tenant = row
    if not tenant.activo:
        raise HTTPException(status_code=403, detail="Cuenta suspendida")

    return CurrentUser(user, tenant)


async def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    if not credentials:
        raise HTTPException(status_code=401, detail="No autenticado")
    return await _load_user(credentials.credentials, db)


async def get_current_user_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Optional[str] = Query(default=None),
    session_token: Optional[str] = Cookie(default=None, alias="mmf_session"),
) -> CurrentUser:
    t = token or session_token
    if not t:
        raise HTTPException(status_code=401, detail="No autenticado")
    return await _load_user(t, db)
