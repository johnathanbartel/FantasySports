/* The Commissioner tab of the league page (league.html), shown only to the commissioner and co-commissioners.
   CommissionerTab.render(ctx) draws it into ctx.content. ctx supplies what league.html already has:
     leagueId, api (FSC.api), esc, content (element), showError, showResult, teamsById, me,
     reload() - re-draw this tab, editLines(teamId) - open that team's lines / lineup editor. */
const CommissionerTab = (() => {
  const ACTIONS = {
    co_commissioner_added: 'added a co-commissioner', co_commissioner_removed: 'removed a co-commissioner',
    transactions_set: 'set the transaction count', faab_set: 'set the FAAB balance', waiver_order_set: 'set the waiver order',
    roster_add: 'added a player', roster_drop: 'dropped a player', roster_move: 'moved a player', lineup_edit: 'edited the lineup',
    lineup_auto: 'auto-set the lineup', lines_edit: 'edited the lines', injured_list_edit: 'changed the injured list',
    season_edited: 'edited a season in the history', season_added: 'added a season to the history',
  };
  const json = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const when = (s) => s ? new Date(s + (/[zZ]|[+-]\d\d:\d\d$/.test(s) ? '' : 'Z')).toLocaleString() : '-';

  async function render(ctx) {
    const {leagueId: L, api, esc, content} = ctx;
    const [ov, seasons] = await Promise.all([api(`/leagues/${L}/commissioner`), api(`/leagues/${L}/seasons`)]);
    const money = (n) => n == null ? '-' : '$' + Math.round(n).toLocaleString();
    let waiverOrder = ov.teams.map(t => t.team_id);          // the order as edited here; saved with the button
    const byId = Object.fromEntries(ov.teams.map(t => [t.team_id, t]));

    const run = async (fn, done) => {
      ctx.showError(''); ctx.showResult('');
      try { await fn(); await ctx.reload(); if (done) ctx.showResult(done); } catch (e) { ctx.showError(e.message); }
    };

    // ---- commissioners -------------------------------------------------------------------------------------------
    const people = ov.commissioners.map(p => {
      const canRemove = p.role === 'co_commissioner' && (ov.is_primary || p.user_id === ctx.me);
      return `<div class="row" style="display:flex;gap:var(--space-3);align-items:center;margin:4px 0"><strong>${esc(p.display_name)}</strong>
        <span class="pill${p.role === 'commissioner' ? ' good' : ''}">${p.role === 'commissioner' ? 'Commissioner' : 'Co-commissioner'}</span>
        ${canRemove ? `<button type="button" class="btn btn-ghost" data-co-remove="${p.user_id}">${p.user_id === ctx.me && !ov.is_primary ? 'Step down' : 'Remove'}</button>` : ''}</div>`;
    }).join('');
    const addCo = ov.is_primary ? (ov.eligible_co_commissioners.length
      ? `<div style="display:flex;gap:var(--space-2);align-items:center;flex-wrap:wrap;margin-top:var(--space-3)"><select id="co-pick" class="inp">${ov.eligible_co_commissioners.map(p => `<option value="${p.user_id}">${esc(p.display_name)}</option>`).join('')}</select>
         <button type="button" class="btn btn-secondary" id="co-add">Add co-commissioner</button></div>`
      : `<p class="muted" style="margin:var(--space-3) 0 0;font-size:13px">Every manager in the league is already a commissioner, or there are no other managers yet.</p>`)
      : `<p class="muted" style="margin:var(--space-3) 0 0;font-size:13px">Only the league's own commissioner can add or remove co-commissioners.</p>`;
    const commissionersCard = `<div class="card"><h3>Commissioners</h3>
      <p class="muted" style="margin-top:0;font-size:13px">A co-commissioner has every commissioner power except adding or removing co-commissioners and deleting the league. They must manage a team here, and everything they do for someone else is logged below.</p>
      ${people}${addCo}</div>`;

    // ---- teams: waiver order, transactions, FAAB -------------------------------------------------------------------
    const teamRows = () => waiverOrder.map((id, i) => {
      const t = byId[id], x = t.transactions;
      return `<tr data-team="${id}"><td style="white-space:nowrap"><strong>${i + 1}</strong>
          <button type="button" class="btn btn-ghost" data-up="${id}" ${i === 0 ? 'disabled' : ''} aria-label="Move up">&uarr;</button><button type="button" class="btn btn-ghost" data-down="${id}" ${i === waiverOrder.length - 1 ? 'disabled' : ''} aria-label="Move down">&darr;</button></td>
        <td>${esc(t.name)} ${t.is_ai_owned ? '<span class="pill ai">AI</span>' : ''}<div class="muted" style="font-size:12px">${esc(t.owner_name || (t.is_ai_owned ? 'AI-owned' : 'unassigned'))}</div></td>
        <td><input class="inp tx-in" data-team="${id}" type="number" min="0" max="100000" value="${x.total}" style="width:5.5em" aria-label="Transactions for ${esc(t.name)}">
          <div class="muted" style="font-size:12px">${x.signings} signings + ${x.trades} trades${x.adjustment ? ` ${x.adjustment > 0 ? '+' : '-'} ${Math.abs(x.adjustment)} adj.` : ''}</div></td>
        ${ov.has_budget ? `<td><input class="inp faab-in" data-team="${id}" type="number" min="0" max="10000000" value="${t.faab_balance ?? 0}" style="width:7em" aria-label="FAAB balance for ${esc(t.name)}">
          <div class="muted" style="font-size:12px">${t.faab_adjustment ? `incl. ${t.faab_adjustment > 0 ? '+' : '-'}${money(Math.abs(t.faab_adjustment))} adj.` : 'no adjustment'}</div></td>` : ''}
        <td style="white-space:nowrap"><button type="button" class="btn btn-secondary" data-save-team="${id}">Save</button>
          <button type="button" class="btn btn-ghost" data-edit-lines="${id}">Lines / lineup</button></td></tr>`;
    }).join('');
    const teamsCard = `<div class="card"><h3>Waiver order, transactions${ov.has_budget ? ' and FAAB' : ''}</h3>
      <p class="muted" style="margin-top:0;font-size:13px">Waiver order is ${ov.waiver_basis === 'commissioner' ? 'set by hand right now' : 'worked out from the standings'}; set it here and it holds until the weekly reset (midnight Pacific, Saturday night), when it is rebuilt from the standings.
        A transaction count is the team's own signings and completed trades; changing it stores the difference as an adjustment and never touches the transaction log.${ov.has_budget ? ' The FAAB balance is the team\'s free-agency money.' : ' This league has no free-agency budget, so there is no FAAB balance.'}</p>
      <div style="overflow-x:auto"><table class="table"><thead><tr><th>Waiver #</th><th>Team</th><th>Transactions</th>${ov.has_budget ? '<th>FAAB balance</th>' : ''}<th></th></tr></thead><tbody id="team-rows">${teamRows()}</tbody></table></div>
      <p style="margin-bottom:0"><button type="button" class="btn btn-primary" id="waiver-save">Save waiver order</button>
        <span class="muted" style="font-size:12px"> Use the arrows to reorder, then save.</span></p></div>`;

    // ---- roster editor -------------------------------------------------------------------------------------------
    const rosterCard = `<div class="card"><h3>Edit a roster</h3>
      <p class="muted" style="margin-top:0;font-size:13px">Add, drop or move players for any team. Edits are free, skip the usual price and roster rules you choose to skip, and aren't counted as the team's transactions.</p>
      <div style="display:flex;gap:var(--space-2);align-items:center;flex-wrap:wrap"><select id="ro-team" class="inp">${ov.teams.slice().sort((a, b) => a.team_id - b.team_id).map(t => `<option value="${t.team_id}">${esc(t.name)}${t.is_ai_owned ? ' (AI)' : ''}</option>`).join('')}</select>
        <label style="font-size:13px"><input type="checkbox" id="ro-over"> allow going over the roster limit</label></div>
      <div id="ro-list" style="margin-top:var(--space-3)"></div>
      <div style="margin-top:var(--space-3)"><div class="card-kicker">Add a player</div><div style="display:flex;gap:var(--space-2);flex-wrap:wrap"><input id="ro-q" class="inp" placeholder="Search by name (2+ letters)..." style="flex:1;min-width:200px"><button type="button" class="btn btn-secondary" id="ro-search">Search</button></div>
        <div id="ro-hits" style="margin-top:var(--space-2)"></div></div></div>`;

    // ---- league history ------------------------------------------------------------------------------------------
    const teamOpts = (sel) => `<option value="">- none -</option>` + ov.teams.map(t => `<option value="${t.team_id}"${t.team_id === sel ? ' selected' : ''}>${esc(t.name)}</option>`).join('');
    const seasonName = (s) => s.label ? `${esc(s.label)} (season ${s.season_number})` : `Season ${s.season_number}`;
    const historyCard = `<div class="card"><h3>Edit league history</h3>
      <p class="muted" style="margin-top:0;font-size:13px">Fix an archived season's champion, final standings, label and note, or add a season played before the league joined the platform.</p>
      ${seasons.length ? seasons.map(s => `<details data-season="${s.season_number}"><summary>${seasonName(s)}${s.edited_at ? ' <span class="pill">edited</span>' : ''}</summary><div class="season-form" data-season="${s.season_number}"></div></details>`).join('') : '<p class="muted">No archived seasons yet.</p>'}
      <details id="add-season"><summary><strong>Add a past season</strong></summary><div class="season-form" data-season="new"></div></details></div>`;

    // ---- activity ------------------------------------------------------------------------------------------------
    const actions = ov.recent_actions.length ? `<div style="overflow-x:auto"><table class="table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Team</th></tr></thead><tbody>${ov.recent_actions.map(a => `<tr><td style="white-space:nowrap">${esc(when(a.at))}</td><td>${esc(a.user_name || a.user_id)}</td><td>${esc(ACTIONS[a.action] || a.action)}</td><td>${esc(a.team_name || '')}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="muted">Nothing yet.</p>';
    const emails = ov.recent_emails.length ? `<div style="overflow-x:auto"><table class="table"><thead><tr><th>When</th><th>To</th><th>Subject</th><th>Status</th></tr></thead><tbody>${ov.recent_emails.map(e => `<tr><td style="white-space:nowrap">${esc(when(e.at))}</td><td>${esc(e.to_email)}</td><td>${esc(e.subject)}</td><td>${esc(e.status === 'not_configured' ? 'recorded, not sent' : e.status)}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="muted">No emails yet.</p>';
    const activityCard = `<div class="card"><h3>Commissioner activity</h3>${actions}
      <h3 style="margin-top:var(--space-4)">League emails</h3>
      <p class="muted" style="margin-top:0;font-size:13px">Everyone in the league is emailed when a league setting changes, a vote on one opens, or a co-commissioner is added.${ov.smtp_configured ? '' : ' No mail server is set up on this site yet, so emails are recorded here but not sent.'}</p>${emails}</div>`;

    content.innerHTML = `<div style="display:grid;gap:var(--space-4)">${commissionersCard}${teamsCard}${rosterCard}${historyCard}${activityCard}</div>`;
    const $q = (sel) => content.querySelector(sel);
    const $$ = (sel) => [...content.querySelectorAll(sel)];

    // ---- wire: commissioners
    const addBtn = $q('#co-add');
    if (addBtn) addBtn.onclick = () => run(() => api(`/leagues/${L}/co-commissioners`, json('POST', {user_id: Number($q('#co-pick').value)})), 'Co-commissioner added. The league has been emailed.');
    $$('[data-co-remove]').forEach(b => b.onclick = () => { if (confirm('Remove this co-commissioner?')) run(() => api(`/leagues/${L}/co-commissioners/${b.dataset.coRemove}`, {method: 'DELETE'}), 'Removed.'); });

    // ---- wire: teams table
    const redrawTeams = () => { $q('#team-rows').innerHTML = teamRows(); wireTeamRows(); };
    const wireTeamRows = () => {
      $$('[data-up]').forEach(b => b.onclick = () => { const i = waiverOrder.indexOf(Number(b.dataset.up)); [waiverOrder[i - 1], waiverOrder[i]] = [waiverOrder[i], waiverOrder[i - 1]]; redrawTeams(); });
      $$('[data-down]').forEach(b => b.onclick = () => { const i = waiverOrder.indexOf(Number(b.dataset.down)); [waiverOrder[i + 1], waiverOrder[i]] = [waiverOrder[i], waiverOrder[i + 1]]; redrawTeams(); });
      $$('[data-save-team]').forEach(b => b.onclick = () => {
        const id = Number(b.dataset.saveTeam), t = byId[id];
        const tx = Number($q(`.tx-in[data-team="${id}"]`).value), faabEl = $q(`.faab-in[data-team="${id}"]`);
        run(async () => {
          if (tx !== t.transactions.total) await api(`/leagues/${L}/teams/${id}/transactions`, json('PUT', {total: tx}));
          if (faabEl && Number(faabEl.value) !== t.faab_balance) await api(`/leagues/${L}/teams/${id}/faab`, json('PUT', {balance: Number(faabEl.value)}));
        }, `${t.name} saved.`);
      });
      $$('[data-edit-lines]').forEach(b => b.onclick = () => ctx.editLines(Number(b.dataset.editLines)));
    };
    wireTeamRows();
    $q('#waiver-save').onclick = () => run(() => api(`/leagues/${L}/waiver-order`, json('PUT', {team_ids: waiverOrder})), 'Waiver order saved. It holds until the next weekly reset.');

    // ---- wire: roster editor
    const teamId = () => Number($q('#ro-team').value);
    const drawRoster = async () => {
      const box = $q('#ro-list');
      box.innerHTML = '<p class="muted">Loading...</p>';
      const list = await api(`/teams/${teamId()}/roster`);
      const others = ov.teams.filter(t => t.team_id !== teamId());
      box.innerHTML = list.length ? `<div style="overflow-x:auto"><table class="table"><thead><tr><th>Player</th><th>Pos</th><th></th></tr></thead><tbody>${list.map(p => `<tr><td>${esc(p.display_name)}</td><td>${esc(p.position || '')}</td>
          <td style="white-space:nowrap"><button type="button" class="btn btn-ghost" data-ro-drop="${p.draftable_asset_id}">Drop</button>
          <label style="font-size:12px"><input type="checkbox" data-ro-waivers="${p.draftable_asset_id}"> waivers</label>
          <select class="inp" data-ro-to="${p.draftable_asset_id}" style="font-size:12px"><option value="">Move to...</option>${others.map(t => `<option value="${t.team_id}">${esc(t.name)}</option>`).join('')}</select></td></tr>`).join('')}</tbody></table></div>`
        : '<p class="muted">This team has no players.</p>';
      const edit = (body, done) => run(async () => { await api(`/leagues/${L}/commissioner/roster`, json('POST', Object.assign({ignore_roster_limit: $q('#ro-over').checked}, body))); }, done);
      $$('[data-ro-drop]').forEach(b => b.onclick = () => edit({action: 'drop', team_id: teamId(), draftable_asset_id: Number(b.dataset.roDrop), put_on_waivers: $q(`[data-ro-waivers="${b.dataset.roDrop}"]`).checked}, 'Player dropped.'));
      $$('[data-ro-to]').forEach(s => s.onchange = () => { if (s.value) edit({action: 'move', from_team_id: teamId(), team_id: Number(s.value), draftable_asset_id: Number(s.dataset.roTo)}, 'Player moved.'); });
    };
    $q('#ro-team').onchange = () => drawRoster().catch(e => ctx.showError(e.message));
    $q('#ro-search').onclick = async () => {
      ctx.showError('');
      try {
        const hits = await api(`/leagues/${L}/commissioner/players?q=${encodeURIComponent($q('#ro-q').value)}`);
        $q('#ro-hits').innerHTML = hits.length ? hits.map(h => `<div class="row" style="display:flex;gap:var(--space-3);align-items:center;margin:3px 0"><span style="flex:1">${esc(h.display_name)} <span class="muted">${esc(h.position || '')}</span></span>
          ${h.team_id == null ? `<button type="button" class="btn btn-secondary" data-ro-add="${h.draftable_asset_id}">Add to this team</button>` : `<span class="muted" style="font-size:12px">on ${esc(byId[h.team_id]?.name || 'a team')}</span>`}</div>`).join('') : '<p class="muted">No players found.</p>';
        $$('[data-ro-add]').forEach(b => b.onclick = () => run(() => api(`/leagues/${L}/commissioner/roster`, json('POST', {action: 'add', team_id: teamId(), draftable_asset_id: Number(b.dataset.roAdd), ignore_roster_limit: $q('#ro-over').checked})), 'Player added.'));
      } catch (e) { ctx.showError(e.message); }
    };
    $q('#ro-q').onkeydown = (e) => { if (e.key === 'Enter') $q('#ro-search').click(); };
    drawRoster().catch(e => ctx.showError(e.message));

    // ---- wire: history forms (drawn once a section is opened)
    const seasonForm = (host, s) => {
      const isNew = s == null;
      let rows = (s ? s.standings : [{team_name: '', wins: 0, losses: 0, ties: 0}]).map(r => ({team_id: r.team_id ?? null, team_name: r.team_name || '', wins: r.wins ?? 0, losses: r.losses ?? 0, ties: r.ties ?? 0}));
      const lowest = seasons.reduce((m, x) => Math.min(m, x.season_number), 1) - 1;
      const draw = () => {
        host.innerHTML = `<div style="display:flex;gap:var(--space-3);flex-wrap:wrap;margin:var(--space-2) 0">
          ${isNew ? `<label>Season number <input class="inp sf-num" type="number" max="0" min="-99" value="${Math.min(0, lowest)}" style="width:5em"></label>` : ''}
          <label>Label <input class="inp sf-label" maxlength="40" placeholder="e.g. 2023" value="${esc(s?.label || '')}" style="width:9em"></label>
          <label>Champion <select class="inp sf-champ">${teamOpts(s?.champion_team_id ?? null)}</select></label></div>
          <label style="display:block;margin-bottom:var(--space-2)">Note <input class="inp sf-note" maxlength="500" value="${esc(s?.note || '')}" style="width:min(100%,520px)"></label>
          <div style="overflow-x:auto"><table class="table"><thead><tr><th>#</th><th>Team</th><th>W</th><th>L</th><th>T</th><th></th></tr></thead><tbody>${rows.map((r, i) => `<tr>
            <td>${i + 1}</td><td><input class="inp r-name" data-i="${i}" maxlength="80" value="${esc(r.team_name)}" ${r.team_id ? 'readonly title="A team in this league"' : ''} style="min-width:11em"></td>
            ${['wins', 'losses', 'ties'].map(k => `<td><input class="inp r-${k}" data-i="${i}" type="number" min="0" max="10000" value="${r[k]}" style="width:4.5em"></td>`).join('')}
            <td style="white-space:nowrap"><button type="button" class="btn btn-ghost r-up" data-i="${i}" ${i === 0 ? 'disabled' : ''}>&uarr;</button><button type="button" class="btn btn-ghost r-down" data-i="${i}" ${i === rows.length - 1 ? 'disabled' : ''}>&darr;</button><button type="button" class="btn btn-ghost r-del" data-i="${i}">Remove</button></td></tr>`).join('')}</tbody></table></div>
          <p style="margin:var(--space-2) 0 0"><button type="button" class="btn btn-secondary sf-add">Add a row</button>
            <button type="button" class="btn btn-primary sf-save">${isNew ? 'Add season' : 'Save season'}</button></p>`;
        const sync = () => host.querySelectorAll('.r-name').forEach(el => {
          const i = Number(el.dataset.i);
          rows[i].team_name = el.value;
          ['wins', 'losses', 'ties'].forEach(k => { rows[i][k] = Number(host.querySelector(`.r-${k}[data-i="${i}"]`).value) || 0; });
        });
        host.querySelectorAll('.r-up').forEach(b => b.onclick = () => { sync(); const i = Number(b.dataset.i); [rows[i - 1], rows[i]] = [rows[i], rows[i - 1]]; draw(); });
        host.querySelectorAll('.r-down').forEach(b => b.onclick = () => { sync(); const i = Number(b.dataset.i); [rows[i + 1], rows[i]] = [rows[i], rows[i + 1]]; draw(); });
        host.querySelectorAll('.r-del').forEach(b => b.onclick = () => { sync(); rows.splice(Number(b.dataset.i), 1); draw(); });
        host.querySelector('.sf-add').onclick = () => { sync(); rows.push({team_id: null, team_name: '', wins: 0, losses: 0, ties: 0}); draw(); };
        host.querySelector('.sf-save').onclick = () => {
          sync();
          const champ = host.querySelector('.sf-champ').value;
          const body = {label: host.querySelector('.sf-label').value.trim() || null, note: host.querySelector('.sf-note').value.trim() || null,
            champion_team_id: champ ? Number(champ) : null, standings: rows};
          if (isNew) run(() => api(`/leagues/${L}/seasons`, json('POST', Object.assign({season_number: Number(host.querySelector('.sf-num').value)}, body))), 'Season added to the history.');
          else run(() => api(`/leagues/${L}/seasons/${s.season_number}`, json('PATCH', body)), 'Season saved.');
        };
      };
      draw();
    };
    $$('.season-form').forEach(host => {
      const key = host.dataset.season, details = host.parentElement;
      details.addEventListener('toggle', () => { if (details.open && !host.dataset.ready) { host.dataset.ready = '1'; seasonForm(host, key === 'new' ? null : seasons.find(x => String(x.season_number) === key)); } });
    });
  }

  return {render};
})();
