"""
Monotributo Más Fácil — Entry point FastAPI.

Lifespan:
  1. create_all (crea tablas si no existen)
  2. Migraciones inline (ALTER TABLE IF NOT EXISTS)
  3. Crea superadmin si no existe (primera vez)
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine, AsyncSessionLocal
from app.auth.models import Base as AuthBase
from app.facturas.models import Base as FacturasBase  # mismo Base via import
from app.auth.router import router as auth_router
from app.ui_router import router as ui_router


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Crear tablas
    async with engine.begin() as conn:
        # Importar todos los modelos para que Base los conozca
        from app.auth.models import Tenant, User, Monotributista, ClienteFinal, Certificado
        from app.facturas.models import LoteEmision, FilaExcel, Factura

        await conn.run_sync(AuthBase.metadata.create_all)

    # 2. Migraciones inline
    async with engine.begin() as conn:
        migraciones = [
            # Monotributistas
            "ALTER TABLE monotributistas ADD COLUMN IF NOT EXISTS telefono VARCHAR(50)",
            "ALTER TABLE monotributistas ADD COLUMN IF NOT EXISTS actividad VARCHAR(200)",
            "ALTER TABLE monotributistas ADD COLUMN IF NOT EXISTS afip_environment VARCHAR(20) DEFAULT 'production'",
            # Lotes
            "ALTER TABLE lotes_emision ADD COLUMN IF NOT EXISTS excel_filename VARCHAR(300)",
            # Facturas
            "ALTER TABLE facturas ADD COLUMN IF NOT EXISTS concepto VARCHAR(500)",
            "ALTER TABLE facturas ADD COLUMN IF NOT EXISTS pdf_path VARCHAR(500)",
            "ALTER TABLE facturas ADD COLUMN IF NOT EXISTS afip_obs TEXT",
        ]
        for sql in migraciones:
            try:
                await conn.execute(text(sql))
            except Exception as e:
                print(f"[migración] {e}")

    # 3. Crear cuenta superadmin en primera ejecución
    await _seed_superadmin()

    yield

    await engine.dispose()


async def _seed_superadmin():
    """Crea el tenant y usuario admin inicial si no existen."""
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
            return  # ya existe

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


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Monotributo Más Fácil",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs" if os.environ.get("ENVIRONMENT") != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Archivos estáticos
if os.path.exists("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Exception handler: 401 en requests de browser → redirect a /login
from fastapi import Request as FastAPIRequest
from fastapi.responses import RedirectResponse as FastAPIRedirect

@app.exception_handler(401)
async def authn_handler(request: FastAPIRequest, exc):
    # Si es una request HTMX o API (acepta JSON), devolver 401 normal
    accept = request.headers.get("accept", "")
    hx = request.headers.get("hx-request", "")
    if "application/json" in accept or hx:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    # Browser normal → redirect a login
    return FastAPIRedirect("/login", status_code=302)

# Routers
app.include_router(auth_router)
app.include_router(ui_router)
