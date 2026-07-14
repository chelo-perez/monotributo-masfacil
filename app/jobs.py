"""
Jobs en background — Monotributo Más Fácil.

Mismo patrón que Facturo Más Fácil: loops asyncio lanzados en el lifespan.
Cada iteración abre su propia sesión (nunca compartir una AsyncSession entre
tareas — causa errores fatales de asyncio).

Tareas:
  Diarias (9:00 AR):
    - check_certificados_vencimiento: avisa al contador cuando el certificado
      ARCA de un monotributista vence en 30/15/7/3/1 días.
  Semanales (domingos 3:00 AR):
    - sync_all_monotributistas: importa el historial ARCA de cada CUIT.
    - reconciliar_numeracion: compara el último comprobante autorizado en
      ARCA contra el máximo registrado localmente (red de seguridad para la
      ventana "CAE otorgado pero guardado local fallido").
"""
import asyncio
import logging
from datetime import timedelta, datetime

from sqlalchemy import select, text as sa_text

from .database import AsyncSessionLocal
from .fechas import hoy_ar, ahora_ar, TZ_AR

logger = logging.getLogger(__name__)

# Días antes del vencimiento en los que se envía alerta (evita spam diario)
DIAS_ALERTA_CERT = {30, 15, 7, 3, 1}


# ── Diaria: vencimiento de certificados ──────────────────────────

async def check_certificados_vencimiento():
    """Alerta al contador cuando un certificado ARCA está por vencer."""
    from .auth.models import Certificado, Monotributista, User
    from .email import enviar_alerta_certificado

    hoy = hoy_ar()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Certificado, Monotributista)
            .join(Monotributista, Monotributista.id == Certificado.monotributista_id)
            .where(
                Certificado.vence_el != None,
                Monotributista.activo == True,
            )
        )
        rows = result.all()

        for cert, mono in rows:
            vence = cert.vence_el.date() if isinstance(cert.vence_el, datetime) else cert.vence_el
            dias = (vence - hoy).days
            if dias not in DIAS_ALERTA_CERT:
                continue

            users_q = await db.execute(
                select(User.email).where(
                    User.tenant_id == mono.tenant_id,
                    User.activo == True,
                )
            )
            for email in users_q.scalars().all():
                try:
                    await enviar_alerta_certificado(
                        to_email=email,
                        razon_social=mono.nombre_fantasia or mono.razon_social,
                        cuit=mono.cuit,
                        dias_restantes=dias,
                        vence_el=vence.strftime("%d/%m/%Y"),
                    )
                    logger.info(f"Alerta cert enviada: {mono.cuit} vence en {dias} días → {email}")
                except Exception as e:
                    logger.error(f"Error enviando alerta cert {mono.cuit}: {e}")


# ── Semanal: sync de historial ARCA ──────────────────────────────

async def sync_all_monotributistas():
    """Sincroniza el historial ARCA de todos los monotributistas con certificado."""
    from .afip.sync_service import sync_mono_invoices
    from .auth.models import Monotributista

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Monotributista).where(
                Monotributista.activo == True,
                Monotributista.cert_encrypted != None,
            )
        )
        monos = result.scalars().all()

    for mono in monos:
        try:
            async with AsyncSessionLocal() as db:
                logger.info(f"Sync semanal: {mono.razon_social} ({mono.cuit})")
                result = await sync_mono_invoices(mono.id, mono.tenant_id, db)
                await db.commit()
                logger.info(
                    f"Sync semanal OK: {mono.cuit} — "
                    f"{result.get('importados', 0)} comprobantes importados"
                )
        except Exception as e:
            logger.error(f"Sync semanal error en {mono.cuit}: {e}")


# ── Semanal: reconciliación de numeración ────────────────────────

async def reconciliar_numeracion():
    """
    Compara el último comprobante autorizado en ARCA vs el máximo registrado
    localmente, por monotributista / PV / tipo. Si hay desfase, avisa por
    email a los usuarios del tenant (el contador).
    """
    from .auth.models import Monotributista, User
    from .config import FERNET_KEY
    from .wsfe import load_credentials, get_token_sign, get_ultimo_cbte
    from .email import enviar_alerta_desfase

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Monotributista).where(
                Monotributista.activo == True,
                Monotributista.cert_encrypted != None,
                Monotributista.afip_punto_venta != None,
            )
        )
        monos = result.scalars().all()

    desfases_por_tenant: dict[int, list[dict]] = {}

    for mono in monos:
        try:
            cert_pem, key_pem = load_credentials(mono, FERNET_KEY)
            token, sign = await get_token_sign(
                cert_pem, key_pem,
                environment=mono.afip_environment or "production",
            )

            async with AsyncSessionLocal() as db:
                for tipo in (11, 13):  # Factura C y Nota de Crédito C
                    try:
                        ultimo_arca = await get_ultimo_cbte(
                            token, sign, mono.cuit,
                            mono.afip_punto_venta, cbte_tipo=tipo,
                            environment=mono.afip_environment or "production",
                        )
                    except Exception:
                        continue
                    if not ultimo_arca:
                        continue  # PV sin emisiones por WS

                    local_q = await db.execute(sa_text(
                        "SELECT COALESCE(MAX(cbte_nro), 0) FROM afip_invoice_history "
                        "WHERE mono_id = :m AND punto_venta = :pv AND cbte_tipo = :t"
                    ), {"m": mono.id, "pv": mono.afip_punto_venta, "t": tipo})
                    ultimo_local = int(local_q.scalar() or 0)

                    if ultimo_arca > ultimo_local:
                        desfases_por_tenant.setdefault(mono.tenant_id, []).append({
                            "razon_social": mono.nombre_fantasia or mono.razon_social,
                            "cuit": mono.cuit,
                            "pv": mono.afip_punto_venta,
                            "tipo": tipo,
                            "arca": ultimo_arca,
                            "local": ultimo_local,
                            "faltan": ultimo_arca - ultimo_local,
                        })
                        logger.warning(
                            f"Desfase: {mono.cuit} PV{mono.afip_punto_venta} tipo {tipo} — "
                            f"ARCA={ultimo_arca} local={ultimo_local}"
                        )
        except Exception as e:
            logger.warning(f"Reconciliación: no se pudo verificar {mono.cuit}: {e}")

    for tenant_id, desfases in desfases_por_tenant.items():
        try:
            async with AsyncSessionLocal() as db:
                from .auth.models import User as _User
                users_q = await db.execute(
                    select(_User.email).where(
                        _User.tenant_id == tenant_id,
                        _User.activo == True,
                    )
                )
                emails = users_q.scalars().all()
            for email in emails:
                await enviar_alerta_desfase(to_email=email, desfases=desfases)
                logger.info(f"Alerta de desfase enviada a {email} ({len(desfases)} casos)")
        except Exception as e:
            logger.error(f"Reconciliación: error enviando alerta del tenant {tenant_id}: {e}")


# ── Loops ────────────────────────────────────────────────────────

async def run_daily_tasks():
    """Tareas diarias a las 9:00 hora argentina."""
    while True:
        try:
            ahora = ahora_ar()
            proxima = ahora.replace(hour=9, minute=0, second=0, microsecond=0)
            if proxima <= ahora:
                proxima += timedelta(days=1)
            espera = (proxima - ahora).total_seconds()
            logger.info(f"Próximas tareas diarias: {proxima.strftime('%d/%m/%Y %H:%M')} AR")
            await asyncio.sleep(espera)

            logger.info("Ejecutando tareas diarias...")
            await check_certificados_vencimiento()
            logger.info("Tareas diarias completadas")
        except Exception as e:
            logger.error(f"Error en tareas diarias: {e}")
            await asyncio.sleep(3600)  # reintento en 1 h ante error inesperado


async def run_weekly_tasks():
    """Tareas semanales: domingos a las 3:00 hora argentina."""
    while True:
        try:
            ahora = ahora_ar()
            dias_hasta_domingo = (6 - ahora.weekday()) % 7
            proxima = (ahora + timedelta(days=dias_hasta_domingo)).replace(
                hour=3, minute=0, second=0, microsecond=0
            )
            if proxima <= ahora:
                proxima += timedelta(days=7)
            espera = (proxima - ahora).total_seconds()
            logger.info(f"Próximo sync semanal: {proxima.strftime('%d/%m/%Y %H:%M')} AR")
            await asyncio.sleep(espera)

            logger.info("Ejecutando sync semanal ARCA...")
            await sync_all_monotributistas()
            logger.info("Sync semanal completado")

            logger.info("Reconciliando numeración contra ARCA...")
            await reconciliar_numeracion()
            logger.info("Reconciliación completada")
        except Exception as e:
            logger.error(f"Error en tareas semanales: {e}")
            await asyncio.sleep(3600)
