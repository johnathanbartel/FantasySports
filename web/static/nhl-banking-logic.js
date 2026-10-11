// Pure view logic for the NHL off-day banking UI (nhl-schedule.html). No DOM, no fetch, no scoring: every number
// and every recommendation comes from the FS-028 API (/teams/{id}/banked-credits, /banked-credits/{id}/...).
// It only decides how to describe them. Checked under Node by scripts/verify_hockey_banking_ui.py.
(function (root) {
  const DAY_MS = 86400000;

  // The API sends naive UTC timestamps ("2026-10-14T21:00:00"); read them as UTC.
  function parseUtc(s) { return Date.parse(/Z|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z'); }

  function fmtPts(v) { return (Math.round(v * 100) / 100).toFixed(2); }

  // The six credit situations the UI must tell apart. `capped` is orthogonal (a capped credit is also one of the others).
  function stateOf(c) {
    const capped = !!c.capped;
    if (c.status === 'ASSIGNED') {
      return c.assignment_mode === 'AUTO'
        ? {key: 'auto-assigned', label: 'Auto-assigned', tone: 'info', capped}
        : {key: 'assigned', label: 'Assigned', tone: 'good', capped};
    }
    if (c.status === 'EXPIRED') return {key: 'expired', label: 'Expired', tone: 'muted', capped};
    return {key: 'available', label: 'Available', tone: 'attention', capped};
  }

  // "Earned 1.80 · 1.50 applies" for a capped credit, "Earned 1.35" otherwise.
  function pointsText(c, cap) {
    const limit = cap == null ? 1.5 : cap;
    if (c.capped) return `Earned ${fmtPts(c.credit_points)} · ${fmtPts(c.applied_points != null ? c.applied_points : limit)} applies`;
    return `Earned ${fmtPts(c.credit_points)}`;
  }

  // Time until the automatic 24-hour assignment: {overdue, ms, text}.
  function fallbackCountdown(c, nowMs) {
    const ms = parseUtc(c.fallback_at) - nowMs;
    if (ms <= 0) return {overdue: true, ms: 0, text: 'Due to bank automatically now'};
    const h = Math.floor(ms / 3600000), m = Math.floor((ms % 3600000) / 60000);
    return {overdue: false, ms, text: h > 0 ? `Banks automatically in ${h}h ${m}m` : `Banks automatically in ${Math.max(1, m)}m`};
  }

  // The alert strip: how many credits are waiting and what is at stake. null when nothing is waiting.
  function alertSummary(credits, nowMs, cap) {
    const limit = cap == null ? 1.5 : cap;
    const waiting = (credits || []).filter(c => c.status === 'AVAILABLE');
    if (!waiting.length) return null;
    waiting.sort((a, b) => parseUtc(a.fallback_at) - parseUtc(b.fallback_at));
    const total = waiting.reduce((s, c) => s + Math.min(c.credit_points, limit), 0);
    const first = waiting[0];
    const cd = fallbackCountdown(first, nowMs);
    const who = waiting.length === 1 ? `${first.player_name} earned ${fmtPts(first.credit_points)} pts on an off day`
                                      : `${waiting.length} off-day credits are waiting to be banked`;
    return {count: waiting.length, firstId: first.id, urgent: cd.overdue || cd.ms < 6 * 3600000, text: `${who}. ${cd.text}.`, total};
  }

  // dailyGameId -> {creditId, points, auto}: what to badge on each series tab.
  function tabBadges(credits) {
    const out = {};
    (credits || []).forEach(c => {
      if (c.status === 'ASSIGNED' && c.assigned_daily_game_id != null) {
        out[c.assigned_daily_game_id] = {creditId: c.id, points: c.applied_points, auto: c.assignment_mode === 'AUTO', name: c.player_name};
      }
    });
    return out;
  }

  // dailyGameId -> {date, label, opponent, homeAway} from the team schedule payload (series[].games[]).
  function gameIndex(schedule, dayLabel) {
    const idx = {};
    ((schedule && schedule.series) || []).forEach(s => (s.games || []).forEach(g => {
      idx[g.daily_game_id] = {date: g.calendar_date, label: dayLabel(g.calendar_date), opponent: s.opponent_name, homeAway: g.home_away};
    }));
    return idx;
  }

  function gameText(g) { return g ? `${g.label} ${g.homeAway === 'home' ? 'vs' : '@'} ${g.opponent}` : 'a later game'; }

  // One line saying where an assigned/expired credit ended up.
  function outcomeText(c, index) {
    if (c.status === 'ASSIGNED') {
      const where = gameText(index[c.assigned_daily_game_id]);
      return c.assignment_mode === 'AUTO'
        ? `Banked automatically to ${where} after 24 hours without a choice.`
        : `Banked to ${where}.`;
    }
    if (c.status === 'EXPIRED') return 'Expired: no open game was left in the 7-day window.';
    return '';
  }

  // How sure the recommendation is, in words that do not overstate. Missing inputs are named, never filled in.
  const CONFIDENCE = {
    FULL: {label: 'Based on projections', note: 'Uses projected margin and win-probability swing.'},
    PARTIAL: {label: 'Partial basis', note: 'Based only on the inputs listed as available; it is not a win-probability estimate.'},
    NEUTRAL: {label: 'No projection data', note: 'Nothing was available to compare games, so this is simply the earliest open game, not a prediction.'},
    NONE: {label: 'No open game', note: 'There is no open game left in the window.'},
  };
  const INPUT_LABELS = {projected_margin: 'Projected margin', opponent_strength: 'Opponent strength', win_probability_swing: 'Win-probability swing'};

  function optionLabel(o, dayLabel) {
    return `${dayLabel(o.calendar_date)} ${o.home_away === 'home' ? 'vs' : '@'} ${o.opponent_name}`;
  }

  function strengthText(s) {
    return s ? `${Math.round(s.win_pct * 100)}% wins over ${s.games} games, ${s.goal_diff_per_game >= 0 ? '+' : ''}${s.goal_diff_per_game.toFixed(2)} goals/game` : null;
  }

  // Everything the recommendation card shows, from a BankRecommendationOut.
  function recommendationView(rec, dayLabel) {
    const conf = CONFIDENCE[rec.confidence] || CONFIDENCE.NONE;
    const options = rec.options.map(o => ({
      id: o.daily_game_id, rank: o.rank, label: optionLabel(o, dayLabel), recommended: o.daily_game_id === rec.recommended_game_id,
      strength: strengthText(o.opponent_strength), gameScore: o.game_score,
    }));
    const best = options.find(o => o.recommended) || null;
    return {
      hasRecommendation: !!best,
      headline: best ? `Recommended: ${best.label}` : 'No open game to bank into',
      recommendedId: best ? best.id : null,
      confidenceKey: rec.confidence, confidenceLabel: conf.label, confidenceNote: conf.note,
      inputs: Object.keys(INPUT_LABELS).map(k => ({
        key: k, label: INPUT_LABELS[k], available: !!(rec.inputs_available && rec.inputs_available[k]),
        text: rec.inputs_available && rec.inputs_available[k] ? 'Used' : 'Not available — not estimated',
      })),
      reasons: rec.reasons || [], options,
    };
  }

  function confirmText(c, option, cap) {
    const limit = cap == null ? 1.5 : cap;
    const note = c.capped ? ` (capped from ${fmtPts(c.credit_points)})` : '';
    return `Bank +${fmtPts(Math.min(c.credit_points, limit))}${note} from ${c.player_name} to ${option.label}? This cannot be changed afterwards.`;
  }

  // API error -> what to tell the user, and whether the card should reload its options.
  function bankError(err) {
    const code = err && err.code, status = err && err.status, msg = (err && err.message) || 'Something went wrong.';
    if (status === 401) return {title: 'Sign in again', text: 'Your session has ended.', refresh: false};
    if (status === 403) return {title: 'Not allowed', text: msg, refresh: false};
    if (code === 'ALREADY_ASSIGNED' || code === 'EXPIRED' || code === 'NOT_AVAILABLE')
      return {title: 'This credit was already settled', text: msg, refresh: true};
    if (code === 'GAME_HAS_CREDIT' || code === 'GAME_STARTED' || code === 'IN_THE_PAST')
      return {title: 'That game is no longer open', text: msg + ' Pick another game.', refresh: true};
    if (status === 0 || msg === 'Failed to fetch') return {title: "Can't reach the server", text: 'Nothing was changed. Check your connection and try again.', refresh: false};
    return {title: "Couldn't bank this credit", text: msg, refresh: true};
  }

  // Newest first by banked time (the list the API returns is already ordered; this keeps tests honest).
  function sortCredits(credits) {
    return (credits || []).slice().sort((a, b) => parseUtc(b.created_at) - parseUtc(a.created_at) || b.id - a.id);
  }

  const api = {DAY_MS, parseUtc, fmtPts, stateOf, pointsText, fallbackCountdown, alertSummary, tabBadges, gameIndex, gameText,
               outcomeText, CONFIDENCE, optionLabel, recommendationView, confirmText, bankError, sortCredits};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NhlBanking = api;
})(typeof window !== 'undefined' ? window : this);
