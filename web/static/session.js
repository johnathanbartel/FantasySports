// Shared session management and authentication client:
// Supports email/password login and registration, guest tokens, and dev switcher.
const FSC = (() => {
  const KEY = 'fsc_session';
  const read = () => { try { return JSON.parse(localStorage.getItem(KEY)) || null; } catch (e) { return null; } };
  const write = (s) => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {} };
  const clear = () => { try { localStorage.removeItem(KEY); } catch (e) {} };

  async function api(path, opts = {}) {
    const s = read();
    const headers = Object.assign({}, opts.headers || {});
    if (s && s.token) headers['Authorization'] = 'Bearer ' + s.token;
    const res = await fetch(path, Object.assign({}, opts, {headers}));
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      // detail is a string for most routes and {code, message} for the simulator routes.
      const d = body.detail;
      const err = new Error((d && typeof d === 'object' ? d.message : d) || `${res.status} ${res.statusText}`);
      err.status = res.status;
      err.code = d && typeof d === 'object' ? d.code : undefined;
      throw err;
    }
    return res.status === 204 ? null : res.json();
  }
  const post = (path, body) => api(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
  const put = (path, body) => api(path, {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
  const patch = (path, body) => api(path, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
  const del = (path) => api(path, { method: 'DELETE' });

  async function register(email, displayName, password) {
    const s = await fetch('/sessions/register', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, display_name: displayName, password}),
    });
    if (!s.ok) throw new Error((await s.json().catch(() => ({}))).detail || 'Registration failed');
    const data = await s.json();
    write(data);
    return data;
  }

  async function login(email, password) {
    const s = await fetch('/sessions/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password}),
    });
    if (!s.ok) throw new Error((await s.json().catch(() => ({}))).detail || 'Sign-in failed');
    const data = await s.json();
    write(data);
    return data;
  }

  async function logout() {
    try { await post('/sessions/logout'); } catch (e) {}
    clear();
    return null;
  }

  async function signIn(userId) {
    const s = await fetch('/sessions', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: userId}),
    });
    if (!s.ok) throw new Error((await s.json().catch(() => ({}))).detail || 'Sign-in failed');
    const data = await s.json();
    write(data);
    return data;
  }

  async function createGuest(displayName) {
    const s = await fetch('/sessions/guest', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({display_name: displayName}),
    });
    if (!s.ok) throw new Error((await s.json().catch(() => ({}))).detail || 'Could not start a guest session');
    const data = await s.json();
    write(data);
    return data;
  }

  // Returns the stored session if its token is still valid, else null.
  async function current() {
    if (!read()) return null;
    try {
      const u = await api('/sessions/me');
      const cur = read();
      if (cur) {
        cur.id = cur.id || u.id;
        cur.user_id = cur.user_id || u.id;
        cur.display_name = u.display_name;
        if (u.email) cur.email = u.email;
        write(cur);
      }
      return cur;
    } catch (e) {
      clear();
      return null;
    }
  }

  function renderNav(containerId = 'nav-auth') {
    const el = document.getElementById(containerId);
    if (!el) return;
    const u = read();
    const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    if (u) {
      el.innerHTML = `
        <span class="text-muted" style="font-size:13px">Hi, <strong>${esc(u.display_name)}</strong></span>
        <button type="button" class="btn btn-ghost" id="fsc-logout-btn" style="font-size:12px;padding:3px 8px">Sign out</button>
      `;
      const btn = document.getElementById('fsc-logout-btn');
      if (btn) btn.onclick = async () => {
        await logout();
        location.reload();
      };
    } else {
      const currentLoc = encodeURIComponent(location.pathname + location.search);
      el.innerHTML = `
        <a href="/static/login.html?redirect=${currentLoc}" class="btn btn-secondary" style="font-size:12px;padding:3px 10px">Sign in</a>
      `;
    }
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => renderNav());
    } else {
      renderNav();
    }
  }

  return {api, post, put, patch, del, delete: del, register, login, logout, signOut: logout, signIn, createGuest, current, user: read, renderNav};
})();
