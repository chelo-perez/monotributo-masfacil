/**
 * Monotributo Más Fácil — app.js
 * Manejo de JWT: inyección en HTMX, refresh automático, logout.
 */

// ---------------------------------------------------------------------------
// JWT helpers
// ---------------------------------------------------------------------------

function getToken() { return localStorage.getItem('mmf_token'); }
function getRefresh() { return localStorage.getItem('mmf_refresh'); }

function isTokenExpired(token) {
  if (!token) return true;
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    return payload.exp * 1000 < Date.now() + 60_000; // 1 min de margen
  } catch { return true; }
}

async function refreshToken() {
  const refresh = getRefresh();
  if (!refresh) { window.location.href = '/login'; return null; }
  try {
    const res = await fetch('/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!res.ok) { window.location.href = '/login'; return null; }
    const data = await res.json();
    localStorage.setItem('mmf_token', data.access_token);
    return data.access_token;
  } catch {
    window.location.href = '/login';
    return null;
  }
}

async function getValidToken() {
  let token = getToken();
  if (isTokenExpired(token)) token = await refreshToken();
  return token;
}

// ---------------------------------------------------------------------------
// HTMX: inyectar token en cada request
// ---------------------------------------------------------------------------

document.addEventListener('htmx:configRequest', async (e) => {
  const token = await getValidToken();
  if (token) e.detail.headers['Authorization'] = 'Bearer ' + token;
});

// Redirigir a login si el servidor devuelve 401
document.addEventListener('htmx:responseError', (e) => {
  if (e.detail.xhr.status === 401) window.location.href = '/login';
});

// ---------------------------------------------------------------------------
// Redirect automático — deshabilitado (la sesión la maneja la cookie server-side)
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Utilidades globales
// ---------------------------------------------------------------------------

/** Formatea un número como $ 1.234.567,89 */
function formatMoney(val) {
  if (val == null) return '—';
  return '$ ' + parseFloat(val).toLocaleString('es-AR', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

/** Flash message temporal */
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
