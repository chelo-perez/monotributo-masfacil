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
            """CREATE TABLE IF NOT EXISTS tablas_categorias (
                id SERIAL PRIMARY KEY,
                vigente_desde DATE NOT NULL UNIQUE,
                vigente_hasta DATE,
                label VARCHAR(50) NOT NULL,
                fuente VARCHAR(200),
                activa BOOLEAN DEFAULT TRUE,
                topes JSONB NOT NULL,
                cuotas_servicios JSONB,
                cuotas_bienes JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS afip_invoice_history (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                mono_id INTEGER NOT NULL REFERENCES monotributistas(id) ON DELETE CASCADE,
                cbte_tipo INTEGER NOT NULL,
                punto_venta INTEGER NOT NULL,
                cbte_nro INTEGER NOT NULL,
                cbte_fecha DATE NOT NULL,
                concepto INTEGER DEFAULT 2,
                fch_serv_desde DATE,
                fch_serv_hasta DATE,
                imp_total NUMERIC(14,2) NOT NULL,
                cae VARCHAR(20),
                source VARCHAR(20) DEFAULT 'wsfe',
                synced_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_afip_history_cbte UNIQUE (mono_id, cbte_tipo, cbte_nro, punto_venta)
            )""",
        ]
        for sql in migraciones:
            try:
                await conn.execute(text(sql))
            except Exception as e:
                print(f"[migración] {e}")

    await _seed_superadmin()
    # Jobs en background (cada tarea abre su propia sesión de DB)
    import asyncio as _asyncio
    from app.jobs import run_daily_tasks, run_weekly_tasks
    _asyncio.create_task(run_daily_tasks())
    _asyncio.create_task(run_weekly_tasks())

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

    # Seed tablas de categorías ARCA
    await _seed_tablas_categorias(db)


async def _seed_tablas_categorias(db):
    """Inserta las tablas históricas de categorías si no existen."""
    from datetime import date
    from app.monotributo.models import TablaCategorias
    from sqlalchemy import select

    TABLAS = [
        {
            "vigente_desde": date(2025, 8, 1),
            "vigente_hasta": date(2026, 1, 31),
            "label": "Ago 2025 – Ene 2026",
            "fuente": "https://www.afip.gob.ar/monotributo/documentos/categorias/monotributo-categorias-agosto-2025-enero-2026.pdf",
            "topes": {"A":8992597.87,"B":13175201.52,"C":17566935.37,"D":21824384.17,"E":25683982.47,"F":32176855.36,"G":38508474.38,"H":58453432.83,"I":65462413.42,"J":74925499.19,"K":90264073.02},
            "cuotas_servicios": {"A":37085.74,"B":44436.65,"C":52459.76,"D":66729.08,"E":93641.91,"F":116696.78,"G":177978.63,"H":399526.94,"I":716407.77,"J":851897.75,"K":1212025.49},
            "cuotas_bienes": {"A":37085.74,"B":44436.65,"C":51184.97,"D":64976.24,"E":84267.13,"F":101148.73,"G":123637.10,"H":246963.29,"I":370445.22,"J":453219.97,"K":549027.92},
        },
        {
            "vigente_desde": date(2026, 2, 1),
            "vigente_hasta": None,
            "label": "Feb 2026 – (vigente)",
            "fuente": "https://www.afip.gob.ar/monotributo/categorias.asp",
            "topes": {"A":10277988.13,"B":15058447.71,"C":21113696.52,"D":26212853.42,"E":30833964.37,"F":38642048.36,"G":46211109.37,"H":70113407.33,"I":78479211.62,"J":89872640.30,"K":108357084.05},
            "cuotas_servicios": {"A":42386.74,"B":48250.78,"C":56501.85,"D":72414.10,"E":102537.97,"F":129045.32,"G":197108.23,"H":447346.93,"I":824802.26,"J":999007.65,"K":1381687.90},
            "cuotas_bienes": {"A":42386.74,"B":48250.78,"C":55227.06,"D":70661.26,"E":92658.35,"F":111198.27,"G":135918.34,"H":272063.40,"I":406512.05,"J":497059.41,"K":600879.51},
        },
    ]

    for t in TABLAS:
        existing = await db.execute(
            select(TablaCategorias).where(
                TablaCategorias.vigente_desde == t["vigente_desde"]
            )
        )
        if existing.scalar_one_or_none():
            continue
        db.add(TablaCategorias(**t, activa=True))

    try:
        await db.commit()
        print("[seed] ✓ Tablas de categorías ARCA cargadas")
    except Exception as e:
        await db.rollback()
        print(f"[seed] tablas_categorias: {e}")


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
