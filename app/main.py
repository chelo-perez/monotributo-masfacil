"""
Monotributo Más Fácil — Entry point FastAPI.
Usa SessionMiddleware igual que Facturo Más Fácil.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import SECRET_KEY
from app.database import engine, AsyncSessionLocal
from app.auth.models import Base as AuthBase
from app.facturas.models import Base as FacturasBase
from app.auth.router import router as auth_router
from app.ui_router import router as ui_router
from app.superadmin.router import router as superadmin_router
from app.facturas.pdf_router import router as pdf_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        from app.auth.models import Tenant, User, Monotributista, ClienteFinal, Certificado
        from app.facturas.models import LoteEmision, FilaExcel, Factura
        await conn.run_sync(AuthBase.metadata.create_all)

    async with engine.begin() as conn:
        migraciones = [
            "ALTER TABLE monotributistas ADD COLUMN IF NOT EXISTS nombre_fantasia VARCHAR(200)",
            "ALTER TABLE monotributistas ADD COLUMN IF NOT EXISTS logo_base64 TEXT",
            "ALTER TABLE monotributistas ADD COLUMN IF NOT EXISTS telefono VARCHAR(50)",
            "ALTER TABLE monotributistas ADD COLUMN IF NOT EXISTS actividad VARCHAR(200)",
            "ALTER TABLE monotributistas ADD COLUMN IF NOT EXISTS afip_environment VARCHAR(20) DEFAULT 'production'",
            "ALTER TABLE lotes_emision ADD COLUMN IF NOT EXISTS excel_filename VARCHAR(300)",
            "ALTER TABLE facturas ADD COLUMN IF NOT EXISTS concepto VARCHAR(500)",
            "ALTER TABLE facturas ADD COLUMN IF NOT EXISTS pdf_path VARCHAR(500)",
            "ALTER TABLE facturas ADD COLUMN IF NOT EXISTS afip_obs TEXT",
            "ALTER TABLE filas_excel ADD COLUMN IF NOT EXISTS email_cliente_raw VARCHAR(200)",
        ]
        for sql in migraciones:
            try:
                await conn.execute(text(sql))
            except Exception as e:
                print(f"[migración] {e}")

    await _seed_superadmin()
    yield
    await engine.dispose()


async def _seed_superadmin():
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@masfacil.com.ar")
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_password:
        print("[seed] ADMIN_PASSWORD no configurado, saltando seed.")
        return

    from sqlalchemy import select
    from app.auth.models import User, Tenant, PlanEstudio
    from app.auth.auth import hash_password

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == admin_email))
        if result.scalar_one_or_none():
            return

        tenant = Tenant(
            nombre="Más Fácil (Admin)",
            email_admin=admin_email,
            plan=PlanEstudio.pro,
            activo=True,
        )
        db.add(tenant)
        await db.flush()

        user = User(
            tenant_id=tenant.id,
            email=admin_email,
            hashed_password=hash_password(admin_password),
            nombre="Admin",
            rol="admin",
            activo=True,
        )
        db.add(user)
        await db.commit()
        print(f"[seed] ✓ Admin creado: {admin_email}")


app = FastAPI(
    title="Monotributo Más Fácil",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs" if os.environ.get("ENVIRONMENT") != "production" else None,
    redoc_url=None,
)

# SessionMiddleware — igual que Facturo Más Fácil
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="mmf_session",
    max_age=60 * 60 * 8,
    same_site="lax",
    https_only=os.environ.get("ENVIRONMENT") == "production",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.exists("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_router)
app.include_router(superadmin_router)
app.include_router(pdf_router)
app.include_router(ui_router)


@app.exception_handler(401)
async def authn_handler(request: Request, exc):
    accept = request.headers.get("accept", "")
    hx = request.headers.get("hx-request", "")
    if "application/json" in accept or hx:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    return RedirectResponse("/login", status_code=302)
