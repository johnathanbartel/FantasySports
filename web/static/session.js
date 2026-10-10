// Dev-grade sign-in shared by the pages (see src/fantasy_sports/api/auth.py):
// POST /sessions with a user id -> bearer token kept in localStorage, sent on
// every API call. There is no password yet; this identifies you, it doesn't
// prove who you are.
const FSC = (() => {
  const KEY = 'fsc_session';
  const read = () => { try { return JSON.parse(localStorage.getItem(KEY)) || null; } catch (e) { return null; } };
  const write = (s) => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {} };

  async function api(path, opts = {}) {
    const s = read();
    const headers = Object.assign({}, opts.headers || {});
    if (s) headers['Authorization'] = 'Bearer ' + s.token;
    const res = await fetch(path, Object.assign({}, opts, {headers}));
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `${res.status} ${res.statusText}`);
    }
    return res.status === 204 ? null : res.json();
  }
  const post = (path, body) => api(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });

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
    try { await api('/sessions/me'); return read(); } catch (e) { return null; }
  }

  return {api, post, signIn, createGuest, current, user: read};
})();
