// Pure view logic for the NHL schedule + lineup viewer (nhl-schedule.html). No DOM, no fetch, so it
// can be checked under Node by scripts/verify_hockey_schedule_viewer.py. Shapes match the FS-021
// (series-schedule) and FS-022 (pecking-order) API responses.
(function (root) {
  const LINES = 4;
  // Players of each position group a line holds: line n of C = rank n; of W/D = ranks 2n-1..2n; of G = rank n.
  const PER_LINE = {C: 1, W: 2, D: 2, G: 1};
  const SLOT_ORDER = ['C', 'W', 'D', 'G'];

  // groups: PeckingOrderOut.groups. Returns 4 lines, each {line, slots: [{group, rank, player|null}]}.
  function buildLines(groups) {
    const byGroup = {};
    (groups || []).forEach(g => { byGroup[g.position_group] = g; });
    const lines = [];
    for (let n = 1; n <= LINES; n++) {
      const slots = [];
      SLOT_ORDER.forEach(code => {
        const g = byGroup[code];
        for (let i = 0; i < PER_LINE[code]; i++) {
          const rank = (n - 1) * PER_LINE[code] + i + 1;
          const player = g && rank <= g.max_depth ? (g.ranked.find(p => p.priority === rank) || null) : null;
          slots.push({group: code, rank, player, beyondDepth: !g || rank > g.max_depth});
        }
      });
      lines.push({line: n, slots});
    }
    return lines;
  }

  // Ranked player ids of a group in priority order, after moving the player at 1-based `rank` by delta (-1 up, +1 down).
  // Returns null when the move is not possible.
  function reorder(group, rank, delta) {
    const ids = group.ranked.slice().sort((a, b) => a.priority - b.priority).map(p => p.player_id);
    const from = rank - 1, to = from + delta;
    if (from < 0 || from >= ids.length || to < 0 || to >= ids.length) return null;
    const [moved] = ids.splice(from, 1);
    ids.splice(to, 0, moved);
    return ids;
  }

  // Ids with `playerId` appended to the ranking (null when the group is already at max depth).
  function addToDepth(group, playerId) {
    if (group.ranked.length >= group.max_depth) return null;
    return group.ranked.slice().sort((a, b) => a.priority - b.priority).map(p => p.player_id).concat([playerId]);
  }

  function removeFromDepth(group, playerId) {
    return group.ranked.slice().sort((a, b) => a.priority - b.priority).map(p => p.player_id).filter(id => id !== playerId);
  }

  // Per-player state for a day. resolved = PeckingOrderResolvedOut | null. Without a resolve result the
  // state is 'unknown' (never guessed). A resolved starter is 'active'; anyone else ranked is 'idle'.
  // Goalies in confirmedGoalieIds additionally carry confirmed=true.
  function playerState(player, resolved, confirmedGoalieIds) {
    const confirmed = !!player && player.position === 'G' && (confirmedGoalieIds || []).indexOf(player.player_id) >= 0;
    if (!resolved) return {state: 'unknown', confirmed};
    const active = Object.keys(resolved.starters || {}).some(
      g => resolved.starters[g].some(p => p.player_id === player.player_id));
    return {state: active ? 'active' : 'idle', confirmed};
  }

  function initials(name) {
    const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return '?';
    return (parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  // Parse YYYY-MM-DD as a local calendar date (no timezone shift).
  function parseDate(s) {
    const [y, m, d] = String(s).split('-').map(Number);
    return new Date(y, m - 1, d);
  }

  const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  function dayLabel(s) {
    const d = parseDate(s);
    return `${DOW[d.getDay()]} ${d.getMonth() + 1}/${d.getDate()}`;
  }

  // The series to show first: the one containing today, else the next to start, else the last played.
  function pickSeries(seriesList, todayIso) {
    if (!seriesList || !seriesList.length) return -1;
    const idx = seriesList.findIndex(s => s.start_date <= todayIso && todayIso <= s.end_date);
    if (idx >= 0) return idx;
    const next = seriesList.findIndex(s => s.start_date > todayIso);
    return next >= 0 ? next : seriesList.length - 1;
  }

  // One tab per calendar day of the series: {date, label, off, opponent, homeAway, status, gameScore}.
  function seriesTabs(series) {
    return series.games.map(g => ({
      date: g.calendar_date,
      label: dayLabel(g.calendar_date),
      off: !g.is_matchup_day,
      opponent: series.opponent_name,
      homeAway: g.home_away,
      status: g.status,
      gameScore: null,   // The Game Score (spec 8.2) is not built yet: always a placeholder
    }));
  }

  // Spec 9.1 ramp: 0-40 charcoal, 41-70 amber, 71-100 vivid red. null (no score yet) is a muted placeholder.
  function gameScoreClass(score) {
    if (score == null) return 'gs-none';
    if (score <= 40) return 'gs-low';
    if (score <= 70) return 'gs-mid';
    return 'gs-high';
  }

  const api = {LINES, PER_LINE, buildLines, reorder, addToDepth, removeFromDepth, playerState, initials,
               dayLabel, pickSeries, seriesTabs, gameScoreClass};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NhlViewer = api;
})(typeof window !== 'undefined' ? window : this);
