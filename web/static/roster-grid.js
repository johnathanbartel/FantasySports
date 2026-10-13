// Pure view logic for the NHL roster grid (docs/hockey_spec.md section 4 and 9): positions down the left, Lines 1-3
// across, every cell a player who can be swapped with another player of the same position group. No DOM, no fetch;
// checked under Node by scripts/verify_roster_grid.py. Shapes match GET/PUT /teams/{id}/pecking-order.
(function (root) {
  const LINES = 3;
  // Rows top to bottom: the six starting spots of Line 1. perLine is how many players a position group puts on one line.
  const ROWS = [
    {label: 'C', group: 'C', slot: 0, perLine: 1},
    {label: 'W', group: 'W', slot: 0, perLine: 2},
    {label: 'W', group: 'W', slot: 1, perLine: 2},
    {label: 'D', group: 'D', slot: 0, perLine: 2},
    {label: 'D', group: 'D', slot: 1, perLine: 2},
    {label: 'G', group: 'G', slot: 0, perLine: 1},
  ];
  const GROUP_NAME = {C: 'centre', W: 'winger', D: 'defenceman', G: 'goalie'};

  const rankOf = (row, line) => (line - 1) * row.perLine + row.slot + 1;

  // groups: PeckingOrderOut.groups -> {rows: [{label, group, cells: [{line, rank, group, player|null}]}], reserves: {group: [player]}}
  function build(groups) {
    const by = {};
    (groups || []).forEach(g => { by[g.position_group] = g; });
    const rows = ROWS.map(row => ({
      label: row.label, group: row.group,
      cells: Array.from({length: LINES}, (_, i) => {
        const line = i + 1, rank = rankOf(row, line), g = by[row.group];
        return {line, rank, group: row.group, player: g && rank <= g.max_depth ? (g.ranked.find(p => p.priority === rank) || null) : null};
      }),
    }));
    const reserves = {};
    ['C', 'W', 'D', 'G'].forEach(code => { reserves[code] = by[code] ? by[code].unranked.slice() : []; });
    return {rows, reserves};
  }

  const orderedIds = g => g.ranked.slice().sort((a, b) => a.priority - b.priority).map(p => p.player_id);

  // A selection is {group, rank} (a filled grid cell) or {group, playerId} (a reserve). Returns {group, ids} - the new
  // ranking of that one group, for PUT /teams/{id}/pecking-order - or {error}.
  function swap(groups, a, b) {
    if (!a || !b) return {error: 'Pick two players.'};
    if (a.group !== b.group) return {error: `A ${GROUP_NAME[a.group] || a.group} can't play ${b.group}.`};
    const g = (groups || []).find(x => x.position_group === a.group);
    if (!g) return {error: 'Unknown position.'};
    const ids = orderedIds(g);
    const idAt = s => s.playerId != null ? s.playerId : ids[s.rank - 1];
    const pa = idAt(a), pb = idAt(b);
    if (pa == null) return {error: 'Nothing to move there.'};
    if (pa === pb) return {error: null, group: a.group, ids, noop: true};
    const out = ids.slice();
    if (pb == null) {                                   // moving onto an empty cell: to the end of the ranking
      if (a.playerId != null && out.length >= g.max_depth) return {error: `All ${g.max_depth} ${a.group} spots are full.`};
      if (a.playerId == null) out.splice(out.indexOf(pa), 1);
      out.push(pa);
      return {error: null, group: a.group, ids: out};
    }
    const ia = out.indexOf(pa), ib = out.indexOf(pb);
    if (ia >= 0 && ib >= 0) { out[ia] = pb; out[ib] = pa; }          // two ranked players trade places
    else if (ia < 0 && ib >= 0) out[ib] = pa;                        // a reserve takes a ranked player's place
    else if (ia >= 0 && ib < 0) out[ia] = pb;
    else return {error: 'Pick a player on a line to swap with.'};
    return {error: null, group: a.group, ids: out};
  }

  const api = {LINES, ROWS, build, swap};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.RosterGrid = api;
})(typeof window !== 'undefined' ? window : this);
