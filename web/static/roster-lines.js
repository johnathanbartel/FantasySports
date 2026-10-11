// Renders GET /teams/{id}/lines (a roster laid out by lineup slot) as HTML. Pure: no DOM, no fetch. Used by the
// draft board's Roster tab and the standalone roster page.
(function (root) {
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const pts = v => v == null ? '-' : Number(v).toFixed(1);

  function row(code, player) {
    const chip = `<span class="tag tag-neutral" style="min-width:44px;justify-content:center">${esc(code)}</span>`;
    if (!player) {
      return `<div class="row" style="display:flex;gap:var(--space-3);align-items:center;padding:6px 0;border-bottom:1px solid var(--color-divider)">
        ${chip}<span class="text-muted" style="flex:1;font-style:italic">Open</span><span class="text-muted">-</span></div>`;
    }
    return `<div class="row" style="display:flex;gap:var(--space-3);align-items:center;padding:6px 0;border-bottom:1px solid var(--color-divider)">
      ${chip}<span style="flex:1"><strong>${esc(player.display_name)}</strong>
      <span class="text-muted" style="font-size:12px">${esc(player.position || '')}${player.real_team ? ' · ' + esc(player.real_team) : ''}</span></span>
      <span style="font-variant-numeric:tabular-nums" title="Last season's points">${pts(player.points)}</span></div>`;
  }

  function section(title, slots) {
    const rows = slots.flatMap(s => Array.from({length: s.capacity}, (_, i) => row(s.slot_code, s.players[i])));
    return rows.length ? `<h5 style="margin:var(--space-3) 0 var(--space-1)">${esc(title)}</h5>${rows.join('')}` : '';
  }

  function html(lines) {
    if (!lines) return '';
    const starters = lines.slots.filter(s => s.is_starter), bench = lines.slots.filter(s => !s.is_starter);
    const benchFilled = bench.reduce((n, s) => n + s.players.length, 0), benchTotal = bench.reduce((n, s) => n + s.capacity, 0);
    return `<div class="text-muted" style="font-size:13px">${lines.starters_filled} of ${lines.starters_total} starting spots filled</div>
      ${section('Starting lineup', starters)}
      ${bench.length ? section(`Bench (${benchFilled} of ${benchTotal})`, bench) : ''}
      ${lines.unslotted.length ? `<h5 style="margin:var(--space-3) 0 var(--space-1)">Extra</h5>${lines.unslotted.map(p => row(p.position || '-', p)).join('')}` : ''}`;
  }

  const api = {html};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.RosterLines = api;
})(typeof window !== 'undefined' ? window : this);
