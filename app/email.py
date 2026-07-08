"""
Email para Monotributo Más Fácil — Resend.
Templates inline (sin archivos separados para simplificar el deploy).
"""

import httpx
from app.config import RESEND_API_KEY, EMAIL_FROM, APP_BASE_URL


async def _send(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        print(f"[email] RESEND_API_KEY no configurada. Destino: {to} | Asunto: {subject}")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        print(f"[email] Error al enviar a {to}: {e}")
        return False


def _base_html(contenido: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background: #F5F6FA; margin: 0; padding: 0; }}
  .wrap {{ max-width: 560px; margin: 32px auto; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,.06); }}
  .header {{ background: #2C3178; padding: 24px 32px; }}
  .header h1 {{ color: white; font-size: 18px; font-weight: 800; margin: 0; }}
  .header h1 span {{ color: #F07B5A; }}
  .body {{ padding: 28px 32px; color: #1A1D3B; font-size: 14px; line-height: 1.7; }}
  .body h2 {{ font-size: 20px; font-weight: 800; color: #2C3178; margin: 0 0 16px; }}
  .kpi-row {{ display: flex; gap: 16px; margin: 20px 0; }}
  .kpi {{ flex: 1; background: #F5F6FA; border-radius: 8px; padding: 16px; text-align: center; }}
  .kpi-val {{ font-size: 28px; font-weight: 800; color: #2C3178; }}
  .kpi-val.green {{ color: #22C55E; }}
  .kpi-val.red {{ color: #EF4444; }}
  .kpi-label {{ font-size: 12px; color: #6B7280; margin-top: 4px; }}
  .table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  .table th {{ padding: 8px 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: #6B7280; text-align: left; border-bottom: 2px solid #E5E7EB; }}
  .table td {{ padding: 10px 12px; font-size: 13px; border-bottom: 1px solid #E5E7EB; }}
  .badge-green {{ color: #166534; font-weight: 700; }}
  .badge-red {{ color: #991B1B; font-weight: 700; }}
  .btn {{ display: inline-block; background: #F07B5A; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 800; font-size: 14px; margin-top: 20px; }}
  .footer {{ padding: 20px 32px; border-top: 1px solid #E5E7EB; font-size: 12px; color: #6B7280; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header"><h1>Monotributo <span>Más Fácil</span></h1></div>
  <div class="body">{contenido}</div>
  <div class="footer">Monotributo Más Fácil · <a href="{APP_BASE_URL}" style="color:#F07B5A;">monotributo.masfacil.com.ar</a></div>
</div>
</body></html>"""


async def enviar_resumen_lote(
    to_email: str,
    nombre_contador: str,
    lote_id: int,
    aprobadas: int,
    rechazadas: int,
    por_monotributista: list,   # lista de ResultadoMonotributista
) -> bool:
    """Email al contador con el resumen del lote emitido."""
    filas = ""
    for r in por_monotributista:
        estado = f'<span class="badge-green">✓ {r.aprobadas}</span>'
        if r.rechazadas:
            estado += f' <span class="badge-red">✗ {r.rechazadas}</span>'
        if r.error_general:
            estado = f'<span class="badge-red">Error: {r.error_general[:60]}</span>'
        filas += f"<tr><td>{r.razon_social}</td><td>{r.cuit}</td><td>{estado}</td></tr>"

    tabla = f"""<table class="table">
      <thead><tr><th>Monotributista</th><th>CUIT</th><th>Resultado</th></tr></thead>
      <tbody>{filas}</tbody>
    </table>""" if filas else ""

    color_aprobadas = "green" if aprobadas > 0 else ""
    color_rechazadas = "red" if rechazadas > 0 else ""

    contenido = f"""
      <h2>Emisión completada</h2>
      <p>Hola {nombre_contador}, el lote #{lote_id} terminó de procesarse.</p>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-val {color_aprobadas}">{aprobadas}</div>
          <div class="kpi-label">Aprobadas</div>
        </div>
        <div class="kpi">
          <div class="kpi-val {color_rechazadas}">{rechazadas}</div>
          <div class="kpi-label">Rechazadas</div>
        </div>
      </div>
      {tabla}
      <a href="{APP_BASE_URL}/facturas" class="btn">Ver facturas emitidas →</a>
    """

    return await _send(
        to=to_email,
        subject=f"Emisión completada — {aprobadas} facturas aprobadas",
        html=_base_html(contenido),
    )


async def enviar_activacion(
    to_email: str,
    nombre_estudio: str,
    token: str,
) -> bool:
    """Email de activación de cuenta para un nuevo estudio contable."""
    url = f"{APP_BASE_URL}/auth/activar/{token}"
    contenido = f"""
      <h2>¡Bienvenido a Monotributo Más Fácil!</h2>
      <p>Hola, tu cuenta para <strong>{nombre_estudio}</strong> fue creada.</p>
      <p>Hacé clic en el botón para activarla y configurar tu contraseña:</p>
      <a href="{url}" class="btn">Activar cuenta →</a>
      <p style="margin-top:20px; font-size:12px; color:#6B7280;">
        Si no esperabas este email, podés ignorarlo.
      </p>
    """
    return await _send(
        to=to_email,
        subject="Activá tu cuenta — Monotributo Más Fácil",
        html=_base_html(contenido),
    )


async def enviar_alerta_monotributo(
    to_email: str,
    razon_social: str,
    cuit: str,
    categoria: str,
    pct: float,
    acumulado: float,
    tope: float,
) -> bool:
    """Alerta cuando un monotributista supera el 80% del tope."""
    contenido = f"""
      <h2>⚠️ Alerta de monotributo</h2>
      <p><strong>{razon_social}</strong> ({cuit}) alcanzó el <strong>{pct:.0f}%</strong>
      del tope de la Categoría {categoria}.</p>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-val" style="font-size:20px;">$ {acumulado:,.0f}</div>
          <div class="kpi-label">Acumulado</div>
        </div>
        <div class="kpi">
          <div class="kpi-val" style="font-size:20px;">$ {tope:,.0f}</div>
          <div class="kpi-label">Tope Cat. {categoria}</div>
        </div>
      </div>
      <p>Revisá si corresponde recategorizar antes de emitir nuevas facturas.</p>
      <a href="{APP_BASE_URL}/monotributistas" class="btn">Ver panel →</a>
    """
    return await _send(
        to=to_email,
        subject=f"⚠️ {razon_social} llegó al {pct:.0f}% del tope de monotributo",
        html=_base_html(contenido),
    )
