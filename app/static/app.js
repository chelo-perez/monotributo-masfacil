/**
 * Monotributo Más Fácil — app.js
 * La sesión se maneja via cookie httponly (mmf_session).
 * Este archivo solo agrega el token JWT en HTMX si existe en localStorage
 * (para compatibilidad futura), y expone utilidades globales.
 */

document.addEventListener('htmx:configRequest', (e) => {
  const token = localStorage.getItem('mmf_token');
  if (token) e.detail.headers['Authorization'] = 'Bearer ' + token;
});

function formatMoney(val) {
  if (val == null) return '—';
  return '$ ' + parseFloat(val).toLocaleString('es-AR', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

function flash(msg, tipo = 'success') {
  const el = document.createElement('div');
  el.style.cssText = `
    position: fixed; top: 20px; right: 20px; z-index: 9999;
    padding: 12px 20px; border-radius: 8px; font-size: 14px; font-weight: 700;
    box-shadow: 0 4px 12px rgba(0,0,0,.15);
    background: ${tipo === 'success' ? '#DCFCE7' : '#FEE2E2'};
    color: ${tipo === 'success' ? '#166534' : '#991B1B'};
    transition: opacity .3s;
  `;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}
