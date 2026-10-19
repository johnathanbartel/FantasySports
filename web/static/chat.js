// Chat widget shared by the league page (Chat tab) and the draft room.
//   const c = Chat.mount(hostElement, {leagueId, thread: 'league' | 'draft:<id>' | 'dm:<user id>', meId, canModerate});
//   c.setThread('dm:7');  c.destroy();
// Polls for newer messages every few seconds while the page is visible; the server marks a thread read when it is opened.
const Chat = (() => {
  const esc = (t) => String(t ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const POLL_MS = 4000;
  const MAX_BODY = 1000;

  const fmtTime = (iso) => {
    const d = new Date(iso);
    const today = new Date();
    const hm = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    return d.toDateString() === today.toDateString() ? hm : d.toLocaleDateString([], {month: 'short', day: 'numeric'}) + ' ' + hm;
  };
  const linkify = (escaped) => escaped.replace(/\bhttps?:\/\/[^\s<]+/g, u => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);

  function mount(host, opts) {
    const api = FSC.api;
    let thread = opts.thread;
    let msgs = [];
    let timer = null;
    let dead = false;
    let loading = false;
    const base = `/leagues/${opts.leagueId}/chat`;

    host.innerHTML = `<div class="chat-box" style="display:flex;flex-direction:column;border:1px solid var(--color-divider);border-radius:var(--radius-md);background:var(--color-surface);min-height:320px">
      <div class="chat-log" role="log" aria-live="polite" aria-label="Chat messages" style="flex:1;overflow-y:auto;max-height:${opts.maxHeight || '480px'};min-height:240px;padding:var(--space-3)"></div>
      <div class="chat-err" style="display:none;color:var(--color-accent-2-800);padding:0 var(--space-3);font-size:12px"></div>
      <form class="chat-form" style="display:flex;gap:var(--space-2);padding:var(--space-3);border-top:1px solid var(--color-divider)">
        <textarea rows="1" maxlength="${MAX_BODY}" placeholder="Write a message" aria-label="Message" style="flex:1;resize:vertical;min-height:36px;box-sizing:border-box;padding:var(--space-2)"></textarea>
        <button type="submit" class="btn btn-primary">Send</button>
      </form></div>`;
    const log = host.querySelector('.chat-log');
    const form = host.querySelector('.chat-form');
    const input = form.querySelector('textarea');
    const errEl = host.querySelector('.chat-err');
    const showErr = (m) => { errEl.textContent = m || ''; errEl.style.display = m ? 'block' : 'none'; };

    function row(m) {
      const mine = m.sender_user_id === opts.meId;
      const canRemove = !m.deleted && (mine || (opts.canModerate && !thread.startsWith('dm:')));
      const who = m.team_name ? `${esc(m.sender_name)} <span class="muted" style="font-weight:normal">· ${esc(m.team_name)}</span>` : esc(m.sender_name);
      const text = m.deleted ? '<em class="muted">Message removed</em>' : linkify(esc(m.body)).replace(/\n/g, '<br>');
      return `<div class="chat-msg" data-id="${m.id}" style="margin-bottom:var(--space-3);${mine ? 'text-align:right' : ''}">
        <div style="font-size:12px"><strong>${who}</strong> <span class="muted">${fmtTime(m.created_at)}</span>
          ${canRemove ? `<button type="button" class="btn btn-ghost" data-del="${m.id}" style="font-size:11px;padding:0 6px" aria-label="Remove message">Remove</button>` : ''}</div>
        <div style="overflow-wrap:anywhere">${text}</div></div>`;
    }

    function draw(stickBottom) {
      log.innerHTML = msgs.length ? msgs.map(row).join('') : '<p class="muted" style="margin:0">No messages yet. Say something.</p>';
      if (stickBottom) log.scrollTop = log.scrollHeight;
      log.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        try { await FSC.del(`${base}/${b.dataset.del}`); const m = msgs.find(x => x.id === Number(b.dataset.del)); if (m) { m.deleted = true; m.body = null; } draw(false); }
        catch (e) { showErr(e.message); }
      });
    }

    async function markRead() {
      const last = msgs.length ? msgs[msgs.length - 1].id : null;
      if (last == null) return;
      try { await FSC.post(`${base}/read`, {thread, up_to_id: last}); if (opts.onRead) opts.onRead(thread); } catch (e) { /* unread count is cosmetic */ }
    }

    async function load() {
      loading = true;
      const forThread = thread;
      try {
        const out = await api(`${base}?thread=${encodeURIComponent(thread)}&limit=100`);
        if (dead || forThread !== thread) return;
        msgs = out.messages;
        showErr('');
        draw(true);
        markRead();
      } catch (e) { if (!dead) { log.innerHTML = ''; showErr(e.message); } }
      finally { loading = false; }
    }

    async function poll() {
      if (dead || loading || document.hidden) return;
      const forThread = thread;
      const after = msgs.length ? msgs[msgs.length - 1].id : 0;
      try {
        const out = await api(`${base}?thread=${encodeURIComponent(thread)}&after_id=${after}`);
        if (dead || forThread !== thread) return;
        const known = new Set(msgs.map(m => m.id));
        const fresh = out.messages.filter(m => !known.has(m.id));
        if (!fresh.length) return;
        const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
        msgs = msgs.concat(fresh);
        draw(atBottom);
        markRead();
      } catch (e) { /* a missed poll is retried on the next tick */ }
    }

    async function send() {
      const body = input.value.trim();
      if (!body) return;
      form.querySelector('button').disabled = true;
      try {
        const m = await FSC.post(base, {thread, body});
        input.value = '';
        showErr('');
        if (!msgs.some(x => x.id === m.id)) msgs.push(m);
        draw(true);
        if (opts.onRead) opts.onRead(thread);
      } catch (e) { showErr(e.message); }
      finally { form.querySelector('button').disabled = false; input.focus(); }
    }

    form.onsubmit = (ev) => { ev.preventDefault(); send(); };
    input.onkeydown = (ev) => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); send(); } };
    timer = setInterval(poll, POLL_MS);
    document.addEventListener('visibilitychange', poll);
    load();

    return {
      setThread(t) { thread = t; msgs = []; log.innerHTML = '<p class="muted" style="margin:0">Loading...</p>'; load(); },
      destroy() { dead = true; clearInterval(timer); document.removeEventListener('visibilitychange', poll); host.innerHTML = ''; },
    };
  }

  const summary = (leagueId) => FSC.api(`/leagues/${leagueId}/chat/summary`);
  const members = (leagueId) => FSC.api(`/leagues/${leagueId}/chat/members`);
  return {mount, summary, members};
})();
