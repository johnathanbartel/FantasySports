// Shared player card: click any element carrying data-pc="<draftable_asset_id>" (see PlayerCard.link) and a card opens with
// bio, news and injury status, fantasy game log, schedule, simulation stats, league transactions, how he was acquired,
// % owned across leagues, the trade-block flag, add / claim / drop / trade buttons, and a Watch star (the team's watchlist).
// All data comes from GET /leagues/{id}/players/{asset}/card; the buttons call the same endpoints as the Transactions tab.
// Pages set PlayerCard.configure({leagueId, onChange, onTrade}) once; onChange runs after a successful add/claim/drop so the
// page can refresh itself, onTrade({teamId, assetId}) starts a proposal (without it the card links to the league's Trades tab).
const PlayerCard = (() => {
  let cfg = {leagueId: null, onChange: null, onTrade: null};
  let el = null;
  let data = null;
  let tab = 'overview';
  let busy = false;
  let seq = 0;

  const esc = (t) => String(t ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const fmtDate = (s) => { if (!s) return ''; const d = new Date(/Z|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z'); return isNaN(d) ? '' : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'}); };
  const fmtWhen = (s) => { if (!s) return 'TBD'; const d = new Date(/Z|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z'); return isNaN(d) ? 'TBD' : d.toLocaleString(undefined, {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}); };
  const INJ = {DTD: 'Day-to-day', OUT: 'Out', IR: 'Injured reserve', LTIR: 'Long-term injured reserve'};
  const VIA = {draft: 'Draft', auction: 'Auction', recruiting: 'Recruiting', free_agency: 'Free agency', trade: 'Trade', commissioner: 'Commissioner'};

  function leagueId() {
    return cfg.leagueId || new URLSearchParams(location.search).get('league_id');
  }

  function configure(c) { cfg = Object.assign(cfg, c || {}); }

  // HTML for a player's name that opens the card. `inner` is already-escaped HTML (default: the escaped name).
  function link(assetId, name, inner) {
    if (assetId == null) return inner != null ? inner : esc(name);
    return `<a href="#" class="pc-link" data-pc="${assetId}">${inner != null ? inner : esc(name)}</a>`;
  }

  function ensure() {
    if (el) return;
    el = document.createElement('div');
    el.id = 'player-card-overlay';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,0.68);z-index:99998;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(3px);box-sizing:border-box;';
    el.addEventListener('click', (e) => { if (e.target === el) close(); });
    document.body.appendChild(el);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && el.style.display !== 'none') close(); });
  }

  function close() { if (el) el.style.display = 'none'; data = null; }

  function shell(inner) {
    el.innerHTML = `<div class="card elev-lg" style="max-width:640px;width:100%;max-height:90vh;overflow-y:auto;padding:var(--space-5);background:var(--color-bg);border:1px solid var(--color-divider);border-radius:var(--radius-md);box-shadow:0 16px 40px rgba(0,0,0,0.35);box-sizing:border-box;position:relative">${inner}</div>`;
    el.style.display = 'flex';
    const x = el.querySelector('[data-pc-close]');
    if (x) x.onclick = close;
  }

  async function open(assetId, opts) {
    ensure();
    const lid = (opts && opts.leagueId) || leagueId();
    const mine = ++seq;
    tab = 'overview';
    shell('<div style="padding:24px;text-align:center" class="muted">Loading player...</div><button type="button" class="btn btn-ghost" data-pc-close style="position:absolute;top:8px;right:8px;font-size:22px;line-height:1;padding:2px 8px" aria-label="Close">&times;</button>');
    try {
      const d = await FSC.api(`/leagues/${lid}/players/${assetId}/card`);
      if (mine !== seq) return;
      data = d;
      render();
    } catch (e) {
      if (mine !== seq) return;
      shell(`<p style="color:var(--color-danger)">${esc(e.message)}</p><button type="button" class="btn btn-secondary" data-pc-close>Close</button>`);
    }
  }

  const row = (k, v) => v == null || v === '' ? '' : `<div style="display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px solid var(--color-divider);font-size:13px"><span class="muted">${k}</span><span style="text-align:right">${v}</span></div>`;
  const section = (title, body) => `<div style="margin-top:var(--space-4)"><h4 style="margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.05em" class="muted">${title}</h4>${body}</div>`;
  const empty = (t) => `<p class="muted" style="margin:0;font-size:13px">${t}</p>`;
  const table = (head, rows) => `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>${head.map(h => `<th style="text-align:${h[1] || 'left'};padding:4px 6px;border-bottom:1px solid var(--color-divider);white-space:nowrap" class="muted">${h[0]}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map((c, i) => `<td style="padding:4px 6px;border-bottom:1px solid var(--color-divider);text-align:${head[i][1] || 'left'};white-space:nowrap">${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;

  function actionButtons(a) {
    const b = [];
    if (a.can_add) b.push(['add', 'Add', 'btn-primary']);
    if (a.can_claim) b.push(['claim', 'Claim', 'btn-primary']);
    if (a.can_cancel_claim) b.push(['cancel', 'Cancel claim', 'btn-secondary']);
    if (a.can_drop) b.push(['drop', 'Drop', 'btn-secondary']);
    if (a.can_trade) b.push(['trade', 'Trade', 'btn-primary']);
    const main = b.map(([k, label, cls]) => `<button type="button" class="btn ${cls}" data-pc-act="${k}" ${busy ? 'disabled' : ''}>${label}</button>`).join('')
      + (!b.length && a.note ? `<span class="muted" style="font-size:12px">${esc(a.note)}</span>` : '');
    // Watch sits apart from the roster moves: it only keeps the player on the team's watchlist (Transactions tab).
    const watch = a.can_watch || a.watching
      ? `<button type="button" class="btn btn-ghost" data-pc-act="${a.watching ? 'unwatch' : 'watch'}" ${busy ? 'disabled' : ''} title="${a.watching ? 'Remove from your watchlist' : 'Add to your watchlist, on the Transactions tab'}">${a.watching ? '&#9733; Watching' : '&#9734; Watch'}</button>` : '';
    return main + watch;
  }

  function overviewTab(d) {
    const o = d.ownership, b = d.bio, inj = d.injury, p = d.pct_owned;
    let own;
    if (o.status === 'rostered') own = `On <strong>${esc(o.team_name)}</strong>${o.owner_name && o.owner_name !== 'AI' ? ` (${esc(o.owner_name)})` : o.owner_name === 'AI' ? ' (AI)' : ''}`;
    else if (o.status === 'free_agent') own = 'Free agent';
    else if (o.status === 'waivers') own = `On waivers until ${esc(fmtWhen(o.waiver_until))}`;
    else own = 'Not in this league\'s player pool';
    const injHtml = inj.status ? `<span class="pill" style="background:var(--color-danger,#c0392b);color:#fff">${esc(inj.status)}</span> ${esc(INJ[inj.status] || '')}${inj.note ? ' - ' + esc(inj.note) : ''}${inj.updated_at ? ` <span class="muted">(${esc(fmtDate(inj.updated_at))})</span>` : ''}` : 'Healthy';
    const acquired = o.status === 'rostered' ? `${esc(o.acquired_text || VIA[o.acquired_via] || '')}${o.acquired_at ? ` <span class="muted">${esc(fmtDate(o.acquired_at))}</span>` : ''}` : null;
    const pctHtml = p.percent == null ? null : `${p.percent}% <span class="muted">(${p.leagues_owned} of ${p.leagues_total} leagues)</span>`;
    return section('Bio & status',
        row('Position', esc(b.position || '-')) + row('Team', esc(b.real_team || '-'))
        + row('Age', b.age != null ? b.age : null) + row('Born', b.birth_date ? esc(fmtDate(b.birth_date)) : null)
        + row('Status', b.is_rookie ? 'Rookie' : null)
        + row('Injury', injHtml)
        + row('In this league', own)
        + row('Acquired', acquired)
        + row('Trade block', o.status === 'rostered' ? (o.on_trade_block ? '<strong>On the trade block</strong>' : 'Not on the block') : null)
        + row('Owned across leagues', pctHtml))
      + (d.sim_stats ? section('Simulation stats', statsGrid(d.sim_stats, b.position === 'G')) : '')
      + section('Recent news', d.news.length
        ? `<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5">${d.news.slice(0, 5).map(n => `<li>${n.kind === 'injury' ? '<strong>Injury:</strong> ' : ''}${esc(n.text)} <span class="muted">${esc(fmtDate(n.at))}</span></li>`).join('')}</ul>`
        : empty('No news for this player yet.'));
  }

  function statsGrid(s, goalie) {
    const cells = goalie
      ? [['GP', s.games_played], ['SV', s.saves], ['SO', s.shutouts]]
      : [['GP', s.games_played], ['G', s.goals], ['A', s.assists], ['PTS', s.points], ['SOG', s.shots]];
    return `<div style="display:flex;gap:8px;flex-wrap:wrap">${cells.map(([k, v]) => `<div class="card elev-sm" style="padding:8px 14px;text-align:center;min-width:56px"><div style="font-size:20px;font-weight:700">${v}</div><div class="muted" style="font-size:11px">${k}</div></div>`).join('')}</div>`
      + `<p class="muted" style="font-size:11px;margin:6px 0 0">Counted from this league's simulated games, never real-life stats.</p>`;
  }

  function logTab(d) {
    const hockey = d.sport === 'hockey', goalie = d.bio.position === 'G';
    if (!d.game_log.length) return section('Fantasy game log', empty(hockey ? 'No finished simulated games for this player yet.' : 'No scored weeks for this player yet.')) + hiddenNote(d);
    const head = hockey
      ? [['Date'], ['Opponent'], ['Result'], ...(goalie ? [['SV', 'right'], ['SO', 'right']] : [['G', 'right'], ['A', 'right'], ['SOG', 'right']])]
      : [['Week'], ['Opponent'], ['Pts', 'right']];
    const rows = d.game_log.map(g => hockey
      ? [esc(g.label), esc(g.opponent || ''), esc(g.result || ''), ...(goalie ? [g.saves, g.shutout ? 1 : 0] : [g.goals, g.assists, g.shots])]
      : [esc(g.label), esc(g.opponent || ''), g.points != null ? g.points.toFixed(1) : '-']);
    return section('Fantasy game log', table(head, rows)) + hiddenNote(d);
  }

  const hiddenNote = (d) => d.log_hidden_games ? `<p class="muted" style="font-size:12px;margin:8px 0 0">${d.log_hidden_games} finished game${d.log_hidden_games === 1 ? ' is' : 's are'} hidden until you open ${d.log_hidden_games === 1 ? 'it' : 'them'} (no spoilers).</p>` : '';

  function scheduleTab(d) {
    if (!d.schedule.length) return section('Upcoming schedule', empty('No upcoming games on the schedule.'));
    return section('Upcoming schedule', table([['When'], ['Opponent'], ['']], d.schedule.map(g => [esc(fmtWhen(g.when)), esc((g.home ? 'vs ' : 'at ') + g.opponent), g.week != null ? `<span class="muted">Wk ${g.week}</span>` : ''])));
  }

  function activityTab(d) {
    return section('League transactions', d.transactions.length
      ? `<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5">${d.transactions.map(t => `<li>${esc(t.text)} <span class="muted">${esc(fmtDate(t.at))}</span></li>`).join('')}</ul>`
      : empty('No transactions involving this player in this league.'))
      + section('News & injury history', d.news.length
        ? `<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5">${d.news.map(n => `<li>${n.kind === 'injury' ? '<strong>Injury:</strong> ' : ''}${esc(n.text)} <span class="muted">${esc(fmtDate(n.at))}</span></li>`).join('')}</ul>`
        : empty('Nothing yet.'));
  }

  function render() {
    const d = data;
    if (!d) return;
    const tabs = [['overview', 'Overview'], ['log', 'Game log'], ['schedule', 'Schedule'], ['activity', 'Activity']];
    const body = tab === 'log' ? logTab(d) : tab === 'schedule' ? scheduleTab(d) : tab === 'activity' ? activityTab(d) : overviewTab(d);
    const inj = d.injury.status ? `<span class="pill" style="background:var(--color-danger,#c0392b);color:#fff;margin-left:6px;font-size:11px">${esc(d.injury.status)}</span>` : '';
    shell(`
      <button type="button" class="btn btn-ghost" data-pc-close aria-label="Close" style="position:absolute;top:8px;right:8px;font-size:22px;line-height:1;padding:2px 8px">&times;</button>
      <div class="muted" style="font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase">${esc(d.bio.position || '')}${d.bio.real_team ? ' · ' + esc(d.bio.real_team) : ''}</div>
      <h3 style="margin:2px 32px 0 0;font-size:22px">${esc(d.bio.display_name)}${inj}${d.ownership.on_trade_block ? ' <span class="pill" style="font-size:11px;margin-left:4px">Trade block</span>' : ''}</h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:var(--space-3)" id="pc-actions">${actionButtons(d.actions)}</div>
      <div id="pc-msg" style="font-size:13px;margin-top:6px"></div>
      <div style="display:flex;gap:4px;border-bottom:1px solid var(--color-divider);margin-top:var(--space-3)">
        ${tabs.map(([k, l]) => `<button type="button" class="btn btn-ghost" data-pc-tab="${k}" style="font-size:13px;padding:6px 12px;${k === tab ? 'font-weight:700;border-bottom:2px solid var(--color-accent)' : ''}">${l}</button>`).join('')}
      </div>
      ${body}`);
    el.querySelectorAll('[data-pc-tab]').forEach(b => b.onclick = () => { tab = b.dataset.pcTab; render(); });
    el.querySelectorAll('[data-pc-act]').forEach(b => b.onclick = () => act(b.dataset.pcAct));
  }

  async function act(kind) {
    const d = data, team = d.actions.my_team_id, id = d.draftable_asset_id;
    if (kind === 'trade') {
      const target = d.ownership.team_id;
      close();
      if (cfg.onTrade) cfg.onTrade({teamId: target, assetId: id});
      else location.href = `/static/league.html?league_id=${d.league_id}&tab=trades&trade_team=${target}&trade_asset=${id}`;
      return;
    }
    if (kind === 'drop' && !confirm(`Drop ${d.bio.display_name}? He goes on waivers for 24 hours, then to the free-agent pool.`)) return;
    const route = {watch: [`/teams/${team}/watchlist`, 'Added to your watchlist. Find it on the Transactions tab.'], add: [`/teams/${team}/free-agency/add`, 'Signed.'], drop: [`/teams/${team}/free-agency/drop`, 'Dropped. He is on waivers for 24 hours.'],
                   claim: [`/teams/${team}/waivers/claim`, 'Claim placed. It is filled when his waiver period ends.'], cancel: [`/teams/${team}/waivers/cancel`, 'Claim cancelled.'],
                   unwatch: [null, 'Removed from your watchlist.']}[kind];
    let body = {draftable_asset_id: id};
    if (kind === 'claim') {
      try {
        const wv = await FSC.api(`/leagues/${d.league_id}/waivers?team_id=${team}`);
        if (wv.acquisition_type === 'budget') {
          const ans = prompt(`Your bid in dollars (you have $${wv.budget_left} left). The highest bid wins; you pay your bid if you win and it is not refunded.`, '0');
          const bid = ans === null ? NaN : parseInt(ans, 10);
          if (Number.isNaN(bid) || bid < 0) return;
          body.bid = bid;
        }
      } catch (e) {}
    }
    busy = true;
    el.querySelectorAll('[data-pc-act]').forEach(b => b.disabled = true);
    try {
      if (kind === 'unwatch') await FSC.del(`/teams/${team}/watchlist/${id}`);
      else await FSC.post(route[0], body);
      busy = false;
      if (cfg.onChange) { try { await cfg.onChange(kind, id); } catch (e) {} }
      const fresh = await FSC.api(`/leagues/${d.league_id}/players/${id}/card`);
      data = fresh;
      render();
      const m = el.querySelector('#pc-msg');
      if (m) m.innerHTML = `<span style="color:var(--color-success,#1a7f37)">${route[1]}</span>`;
    } catch (e) {
      busy = false;
      el.querySelectorAll('[data-pc-act]').forEach(b => b.disabled = false);
      const m = el.querySelector('#pc-msg');
      if (m) m.innerHTML = `<span style="color:var(--color-danger)">${esc(e.message)}</span>`;
    }
  }

  // One listener serves every page: any element (or ancestor) with data-pc opens the card.
  document.addEventListener('click', (e) => {
    const t = e.target.closest && e.target.closest('[data-pc]');
    if (!t) return;
    e.preventDefault();
    e.stopPropagation();
    open(Number(t.dataset.pc));
  }, true);

  return {configure, open, close, link};
})();
