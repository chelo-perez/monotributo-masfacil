"""
PDF de factura — replica exacta del formato ARCA/AFIP.
Proporciones medidas del PDF original de AFIP.
"""
import io, base64
from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.pdfgen import canvas as rl_canvas

DARK = HexColor("#1A1A1A")
GRAY = HexColor("#555555")
LINE = HexColor("#999999")

W, H = A4  # 595.3 x 841.9 pts
ML = 12*mm  # margen izquierdo
MR = 12*mm  # margen derecho
MT = 8*mm   # margen superior
MB = 8*mm   # margen inferior
CW = W - ML - MR  # ~171mm

# Línea divisoria medida del PDF original de AFIP: 101.3mm desde borde izq
# La línea pasa por el CENTRO del cuadro de la C
MID = 101.3*mm  # posición absoluta desde el borde izquierdo

def _fmt(v):
    return "{:,.2f}".format(float(v)).replace(",","X").replace(".","," ).replace("X",".")

def _cuit(s):
    c = s.replace("-","").replace(" ","")
    return f"{c[:2]}-{c[2:10]}-{c[10]}" if len(c)==11 else s

def B(c, sz): c.setFont("Helvetica-Bold", sz)
def R(c, sz): c.setFont("Helvetica", sz)
def BI(c, sz): c.setFont("Helvetica-BoldOblique", sz)
def I(c, sz): c.setFont("Helvetica-Oblique", sz)

def hline(c, y, x1=None, x2=None, lw=0.4):
    c.setStrokeColor(LINE); c.setLineWidth(lw)
    c.line(x1 if x1 is not None else ML, y,
           x2 if x2 is not None else W-MR, y)

def vline(c, x, y1, y2, lw=0.4):
    c.setStrokeColor(LINE); c.setLineWidth(lw)
    c.line(x, y1, x, y2)

def box(c, x, y, w, h, lw=0.5, fill=False):
    c.setStrokeColor(LINE); c.setLineWidth(lw)
    if fill:
        c.setFillColor(HexColor("#F5F5F5"))
        c.rect(x, y, w, h, fill=1)
    else:
        c.rect(x, y, w, h, fill=0)


def generar_factura_pdf(
    *, razon_social, cuit_emisor, punto_venta, cbte_nro, cbte_tipo,
    fecha, fch_serv_desde, fch_serv_hasta, concepto, importe,
    cae, cae_vto, cliente_nombre,
    cliente_dni=None, cliente_cuit=None, logo_base64=None,
    ingresos_brutos=None, domicilio_emisor=None,
    imp_neto=None, imp_iva=None,  # solo para Factura A
    condicion_iva_emisor=None,    # label de condición IVA del emisor
) -> bytes:

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    letra = "C" if cbte_tipo==11 else ("B" if cbte_tipo==6 else "A")
    cod   = f"COD. 0{cbte_tipo:02d}"

    # Texto de Ingresos Brutos según la opción configurada
    _IB_LABELS = {
        "exento": "Exento",
        "regimen_simplificado": "Contrib. Local",
        "convenio_multilateral": "Convenio Multilateral",
    }
    ib_label = _IB_LABELS.get(ingresos_brutos or "exento", "Exento")

    # Condición IVA del emisor
    _COND_IVA_LABELS = {
        "monotributo":           "Resp. Monotributo",
        "exento_iva":            "IVA Exento",
        "responsable_inscripto": "Resp. Inscripto",
    }
    cond_iva_label = condicion_iva_emisor or _COND_IVA_LABELS.get("monotributo")

    # ══ MEDIDAS (en pts desde la base de la página) ══════════
    # Convertimos mm a pts: 1mm = 2.835pts
    # A4 height = 841.9pts = 297mm

    # Bloque ORIGINAL: 0→8mm desde arriba = 297→289mm desde base
    OR_top = H - MT
    OR_bot = OR_top - 8*mm
    box(c, ML, OR_bot, CW, OR_top-OR_bot, lw=0.7)
    c.setFillColor(DARK); B(c, 11)
    c.drawCentredString(W/2, OR_bot + 2.5*mm, "ORIGINAL")

    # Bloque HEADER: 8→57mm desde arriba = alto 49mm
    HDR_top = OR_bot
    HDR_bot = HDR_top - 49*mm
    box(c, ML, HDR_bot, CW, HDR_top-HDR_bot, lw=0.7)

    # ── CUADRO LETRA — medidas exactas del PDF de AFIP ──
    # El cuadro está en la parte SUPERIOR del header, compacto
    # Ancho: 15.7mm centrado en MID | Alto: 9.7mm pegado al tope
    # La línea divisoria pasa por MID y baja hasta el fin del header
    BOX_W = 15.7*mm
    BOX_H = 13*mm
    BOX_x = MID - BOX_W/2
    BOX_y = HDR_top - BOX_H   # pegado al tope del header
    box(c, BOX_x, BOX_y, BOX_W, BOX_H, lw=1.2)

    # La línea vertical solo desde el FONDO del cuadro hasta el fin del header
    # (no pasa por encima del cuadro)
    vline(c, MID, HDR_bot, BOX_y, lw=0.7)

    # C y COD.011 dentro del cuadro
    c.setFillColor(DARK); B(c, 22)
    c.drawCentredString(MID, BOX_y + 4.5*mm, letra)
    B(c, 6)
    c.drawCentredString(MID, BOX_y + 1*mm, cod)

    # ── ZONA IZQUIERDA ──
    # Logo del estudio
    logo_used = 0
    if logo_base64:
        try:
            # Limpiar prefijo data URI si existe
            _logo = logo_base64
            if "," in _logo:
                _logo = _logo.split(",", 1)[1]
            lb = base64.b64decode(_logo)
            from reportlab.lib.utils import ImageReader as _IR
            # Zona izquierda disponible para el logo
            area_w = MID - BOX_W/2 - ML - 6*mm  # ancho disponible
            max_lh = 22*mm   # alto máximo del logo
            max_lw = area_w  # ancho máximo
            # Centrar horizontalmente en la zona izquierda
            left_cx_logo = ML + (MID - BOX_W/2 - ML) / 2
            logo_y = HDR_top - max_lh - 3*mm
            c.drawImage(_IR(io.BytesIO(lb)), left_cx_logo - max_lw/2, logo_y,
                        width=max_lw, height=max_lh,
                        preserveAspectRatio=True, anchor='c', mask='auto')
            logo_used = max_lh + 4*mm
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).error(f"PDF logo error: {_e}")

    # Razón social: solo si NO hay logo (cuando hay logo ya está en los datos del emisor)
    left_cx = ML + (MID - BOX_W/2 - ML) / 2
    if not logo_base64:
        c.setFillColor(DARK)
        max_w = MID - BOX_W/2 - ML - 6*mm  # margen extra para no solapar el cuadro C
        rs_upper = razon_social.upper()
        B(c, 7.5)
        if c.stringWidth(rs_upper, "Helvetica-Bold", 7.5) <= max_w:
            c.drawCentredString(left_cx, HDR_top - 6*mm, rs_upper)
        else:
            # Partir en dos líneas lo más equilibradas posible
            words = rs_upper.split()
            best_split = len(words) // 2
            # Buscar el split que minimice la diferencia de longitud entre líneas
            best_diff = float('inf')
            for i in range(1, len(words)):
                l1 = c.stringWidth(" ".join(words[:i]), "Helvetica-Bold", 7.5)
                l2 = c.stringWidth(" ".join(words[i:]), "Helvetica-Bold", 7.5)
                if l1 <= max_w and l2 <= max_w:
                    diff = abs(l1 - l2)
                    if diff < best_diff:
                        best_diff = diff
                        best_split = i
            line1 = " ".join(words[:best_split])
            line2 = " ".join(words[best_split:])
            c.drawCentredString(left_cx, HDR_top - 4*mm, line1)
            c.drawCentredString(left_cx, HDR_top - 9.5*mm, line2)

    # Datos del emisor — el header tiene 49mm de alto
    # Zona nombre/logo: primeros ~20mm desde arriba
    # Zona datos: los ~29mm restantes
    # ey = posición Y de la primera línea de datos
    if logo_used > 0:
        ey = HDR_top - logo_used - 5*mm
    elif not logo_base64 and c.stringWidth(razon_social.upper(), "Helvetica-Bold", 8) > (MID - BOX_W/2 - ML - 6*mm):
        ey = HDR_top - 20*mm  # 2 líneas de nombre → más espacio arriba
    else:
        ey = HDR_top - 17*mm  # 1 línea de nombre
    lx = ML + 3*mm
    def draw_label_val(y, label, val, font_size=7.5):
        B(c, 7.5); c.setFillColor(DARK)
        c.drawString(lx, y, label)
        lw = c.stringWidth(label, "Helvetica-Bold", 7.5)
        R(c, font_size)
        c.drawString(lx + lw + 2, y, val)

    # Razón social — partir en 2 líneas si no entra
    rs_label = "Razón Social:"
    rs_label_w = c.stringWidth(rs_label + " ", "Helvetica-Bold", 7.5)
    rs_area_w = MID - BOX_W/2 - ML - 3*mm  # ancho total del área izquierda
    rs_val_w  = rs_area_w - rs_label_w      # ancho disponible para el valor en línea 1
    if c.stringWidth(razon_social, "Helvetica", 7.5) <= rs_val_w:
        draw_label_val(ey, rs_label, razon_social); ey -= 5.5*mm
    else:
        # Partir: línea 1 después del label, línea 2 indentada al mismo nivel
        words = razon_social.split()
        line1, line2 = [], []
        for w in words:
            test = " ".join(line1 + [w])
            if c.stringWidth(test, "Helvetica", 7.5) <= rs_val_w:
                line1.append(w)
            else:
                line2.append(w)
        draw_label_val(ey, rs_label, " ".join(line1)); ey -= 5*mm
        R(c, 7.5); c.setFillColor(DARK)
        c.drawString(lx + rs_label_w, ey, " ".join(line2)); ey -= 5.5*mm
    if domicilio_emisor:
        draw_label_val(ey, "Domicilio Comercial:", domicilio_emisor); ey -= 5.5*mm
    draw_label_val(ey, "Condición frente al IVA:", cond_iva_label); ey -= 5.5*mm
    # CUIT solo va en zona derecha (no duplicar)

    # ── ZONA DERECHA ──
    # "FACTURA" grande — empieza después del cuadro
    rx = MID + BOX_W/2 + 4*mm
    c.setFillColor(DARK)
    B(c, 22)
    c.drawString(rx, HDR_top - 11*mm, "FACTURA")
    B(c, 8)
    c.drawString(rx, HDR_top - 19*mm,
        f"Punto de Venta: {punto_venta:05d}    Comp. Nro: {cbte_nro:08d}")
    B(c, 8)
    c.drawString(rx, HDR_top - 25*mm, f"Fecha de Emisión: {fecha.strftime('%d/%m/%Y')}")
    B(c, 7.5)
    c.drawString(rx, HDR_top - 31*mm, f"CUIT: {_cuit(cuit_emisor)}")
    B(c, 7.5)
    c.drawString(rx, HDR_top - 37*mm, f"Ingresos Brutos:  {ib_label}")
    B(c, 7.5)
    c.drawString(rx, HDR_top - 43*mm, f"Cond. IVA: {cond_iva_label}")

    # ══ PERÍODO FACTURADO ════════════════════════════════════
    PER_top = HDR_bot
    PER_bot = PER_top - 8*mm
    box(c, ML, PER_bot, CW, PER_top-PER_bot, lw=0.6)
    B(c, 8); c.setFillColor(DARK)
    c.drawString(ML+3*mm, PER_bot + 2*mm,
        f"Período Facturado Desde:   {fch_serv_desde.strftime('%d/%m/%Y')}   "
        f"Hasta: {fch_serv_hasta.strftime('%d/%m/%Y')}")

    # ══ RECEPTOR ════════════════════════════════════════════
    REC_top = PER_bot
    REC_bot = REC_top - 20*mm
    box(c, ML, REC_bot, CW, REC_top-REC_bot, lw=0.6)
    vline(c, MID, REC_bot, REC_top, lw=0.4)

    # Izquierda
    ry = REC_top - 5*mm
    c.setFillColor(DARK)
    # Determinar si el documento es CUIT o DNI
    _doc_val = cliente_cuit or cliente_dni or ""
    _doc_limpio = str(_doc_val).replace("-","").replace(" ","").strip()
    _es_cuit = len(_doc_limpio) == 11 and _doc_limpio.isdigit()
    if _doc_val:
        _doc_label = "CUIT:" if (_es_cuit or cliente_cuit) else "DNI:"
        _doc_str   = _cuit(_doc_limpio) if _es_cuit else _doc_limpio
        _doc_offset = 13*mm if _es_cuit else 11*mm
        B(c, 7.5); c.drawString(ML+3*mm, ry, _doc_label)
        R(c, 7.5); c.drawString(ML+_doc_offset, ry, _doc_str)
    else:
        B(c, 7.5); c.drawString(ML+3*mm, ry, "Consumidor Final")
    ry -= 5.5*mm
    B(c, 7.5); c.drawString(ML+3*mm, ry, "Condición frente al IVA:")
    R(c, 7.5); c.drawString(ML+43*mm, ry, "Consumidor Final")
    ry -= 5.5*mm
    B(c, 7.5); c.drawString(ML+3*mm, ry, "Condición de venta:")
    R(c, 7.5); c.drawString(ML+37*mm, ry, "Transferencia Bancaria")

    # Derecha
    B(c, 7.5); c.setFillColor(DARK)
    c.drawString(MID+3*mm, REC_top - 5*mm, "Apellido y Nombre / Razón Social:")
    R(c, 7.5)
    c.drawString(MID+3*mm, REC_top - 11*mm, cliente_nombre)

    # ══ TABLA DE ÍTEMS ════════════════════════════════════════
    # Columnas — mismo ancho relativo que AFIP
    TBL_top = REC_bot
    TH = 6*mm   # alto cabecera
    TBL_cab = TBL_top - TH
    box(c, ML, TBL_cab, CW, TH, lw=0.5, fill=True)

    # Posiciones x de columnas
    C0 = ML           # Código
    C1 = ML+13*mm     # Descripción
    C2 = ML+87*mm     # Cantidad
    C3 = ML+99*mm     # U.Medida
    C4 = ML+114*mm    # Precio Unit.
    C5 = ML+130*mm    # % Bonif
    C6 = ML+141*mm    # Imp. Bonif.
    C7 = ML+154*mm    # Subtotal
    CE = ML+CW        # fin

    for x in [C1, C2, C3, C4, C5, C6, C7]:
        vline(c, x, TBL_cab, TBL_top, lw=0.3)

    B(c, 6.5); c.setFillColor(DARK)
    c.drawString(C0+1*mm, TBL_cab+2*mm, "Código")
    c.drawString(C1+1*mm, TBL_cab+2*mm, "Producto / Servicio")
    c.drawRightString(C3-1*mm, TBL_cab+2*mm, "Cantidad")
    c.drawString(C3+1*mm, TBL_cab+2*mm, "U. Medida")
    c.drawString(C4+1*mm, TBL_cab+2*mm, "Precio Unit.")
    c.drawString(C5+1*mm, TBL_cab+2*mm, "% Bonif")
    c.drawString(C6+1*mm, TBL_cab+2*mm, "Imp. Bonif.")
    c.drawString(C7+1*mm, TBL_cab+2*mm, "Subtotal")

    # Fila de datos
    RH = 8*mm
    ROW_y = TBL_cab - RH
    hline(c, TBL_cab, lw=0.4)

    R(c, 8); c.setFillColor(DARK)
    c.drawString(C1+1*mm,    ROW_y+2.5*mm, concepto)
    c.drawRightString(C3-1*mm, ROW_y+2.5*mm, "1,00")
    c.drawString(C3+1*mm,    ROW_y+2.5*mm, "unidades")
    c.drawRightString(C5-1*mm, ROW_y+2.5*mm, _fmt(importe))   # Precio Unit.
    c.drawRightString(C6-1*mm, ROW_y+2.5*mm, "0,00")           # % Bonif
    c.drawRightString(C7-1*mm, ROW_y+2.5*mm, "0,00")           # Imp. Bonif.
    c.drawRightString(CE-1*mm, ROW_y+2.5*mm, _fmt(importe))   # Subtotal

    hline(c, ROW_y, lw=0.5)

    # ══ TOTALES ═══════════════════════════════════════════════
    TOT_H = 24*mm
    TOT_y = MB + 30*mm   # desde abajo
    box(c, ML, TOT_y, CW, TOT_H, lw=0.6)
    TOT_sep = ML + CW * 0.68
    vline(c, TOT_sep, TOT_y, TOT_y+TOT_H, lw=0.3)

    ty = TOT_y + TOT_H - 6*mm
    # Factura A: mostrar Neto Gravado + IVA 21% + Total
    # Factura B/C: Subtotal + Otros Tributos + Total
    if cbte_tipo == 1 and imp_neto is not None and imp_iva is not None:
        filas_tot = [
            ("Neto Gravado: $",    _fmt(imp_neto), False),
            ("IVA 21%: $",         _fmt(imp_iva),  False),
            ("Importe Total: $",   _fmt(importe),  True),
        ]
    else:
        filas_tot = [
            ("Subtotal: $",             _fmt(importe), False),
            ("Importe Otros Tributos: $","0,00",       False),
            ("Importe Total: $",        _fmt(importe), True),
        ]
    for label, val, bold in filas_tot:
        (B if bold else R)(c, 8.5); c.setFillColor(DARK)
        c.drawRightString(TOT_sep - 2*mm, ty, label)
        c.drawRightString(CE - 1*mm, ty, val)
        if "Tributos" in label or "IVA" in label:
            hline(c, ty - 1.5*mm, x1=TOT_sep, lw=0.3)
        ty -= 6*mm

    # ══ PIE ═══════════════════════════════════════════════════
    PIE_H = 28*mm
    PIE_y = MB
    box(c, ML, PIE_y, CW, PIE_H, lw=0.6)

    # QR real — URL de verificación AFIP
    QS = 20*mm
    qx = ML + 3*mm
    qy = PIE_y + (PIE_H - QS) / 2
    try:
        import qrcode as _qr, json as _json, base64 as _b64, tempfile as _tmp, os as _os
        cuit_num = int(cuit_emisor.replace("-","").replace(" ","")[:11])
        qr_data = _json.dumps({
            "ver": 1, "fecha": fecha.strftime("%Y-%m-%d"),
            "cuit": cuit_num, "ptoVta": punto_venta,
            "tipoCmp": cbte_tipo, "nroCmp": cbte_nro,
            "importe": float(importe), "moneda": "PES", "ctz": 1,
            "tipoDocRec": 99, "nroDocRec": 0,
            "tipoCodAut": "E",
            "codAut": int(cae) if cae.isdigit() else cae,
        }, separators=(",",":"))
        qr_payload = _b64.b64encode(qr_data.encode()).decode()
        qr_url = f"https://www.afip.gob.ar/fe/qr/?p={qr_payload}"
        qr_img = _qr.make(qr_url)
        tmp = _tmp.NamedTemporaryFile(suffix=".png", delete=False)
        qr_img.save(tmp.name, format="PNG")
        tmp.close()
        c.drawImage(tmp.name, qx, qy, width=QS, height=QS)
        _os.unlink(tmp.name)
    except Exception:
        c.setFillColor(DARK)
        c.rect(qx, qy, QS, QS, fill=1, stroke=0)

    # Logo manito Facturo Más Fácil
    lpath = str(Path(__file__).parent.parent / "static" / "img" / "logo-report.png")
    lx = qx + QS + 3*mm
    try:
        c.drawImage(lpath, lx, PIE_y + 7*mm,
                    width=10*mm, height=15*mm,
                    preserveAspectRatio=True, anchor='sw', mask='auto')
        lx += 13*mm
    except Exception:
        pass

    BI(c, 7.5); c.setFillColor(DARK)
    c.drawString(lx, PIE_y + 19*mm, "Comprobante Autorizado")
    I(c, 6.5); c.setFillColor(GRAY)
    c.drawString(lx, PIE_y + 13*mm, "Emitido con Monotributo Más Fácil · monotributo.masfacil.com.ar")

    # Pág. 1/1 centrado
    R(c, 8); c.setFillColor(DARK)
    c.drawCentredString(W/2, PIE_y + 22*mm, "Pág. 1/1")

    # CAE
    B(c, 9)
    c.drawString(W/2 + 8*mm, PIE_y + 22*mm, f"CAE N°:  {cae}")
    R(c, 8)
    c.drawString(W/2 + 8*mm, PIE_y + 16*mm,
        f"Fecha de Vto. de CAE:  {cae_vto.strftime('%d/%m/%Y')}")

    c.save()
    return buf.getvalue()
