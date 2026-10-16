// Pure view logic for the NHL game page (nhl-game.html): which screen a simulator state maps to, what the
// viewer may do, how the event feed merges and replays, and how the box score is laid out. No DOM, no fetch,
// no randomness, and no scoring: every number shown comes from the API (event totals, final snapshot,
// provenance). Checked under Node by scripts/verify_hockey_simulator_ui.py.
(function (root) {
  const REPLAY_MS = 75000;      // a full game replays in about 75 seconds
  const MIN_GAP_MS = 120;       // events never land on top of each other
  const MAX_BACKOFF_MS = 30000;

  // The screen for an API state (SimStateOut). The veil is the server's call (state.veiled); we only mirror it.
  function screenFor(state) {
    if (!state || !state.published) return 'unpublished';
    if (state.status === 'SCHEDULED') return 'scheduled';
    // Games published period by period (the NHL windows) have no veil: a game centre until the viewer has watched it all.
    if (state.progressive) return state.is_complete || (state.status === 'FINAL' && state.reveal_mode === 'SKIPPED') ? 'final' : 'hub';
    if (state.veiled) return state.status === 'LIVE' ? 'live-veiled' : 'final-veiled';
    if (state.reveal_mode === 'SKIPPED') return 'final';
    if (state.status === 'LIVE') return 'live';
    return state.is_complete ? 'final' : 'replay';
  }

  // Buttons a screen offers: {id, label, enabled, why}. Disabled buttons say why instead of disappearing.
  function controlsFor(screen, state) {
    const skipWhy = 'Available once the game has ended.';
    switch (screen) {
      case 'live-veiled':
        return [{id: 'watch', label: 'Watch live', enabled: true},
                {id: 'skip', label: 'Skip to box score', enabled: false, why: skipWhy}];
      case 'final-veiled':
        return [{id: 'watch', label: 'Watch replay (about 75 seconds)', enabled: true},
                {id: 'skip', label: 'Skip to box score', enabled: true}];
      case 'replay':
        return [{id: 'skip', label: 'Skip to box score', enabled: true}];
      case 'live':
        return [{id: 'skip', label: 'Skip to box score', enabled: false, why: skipWhy}];
      default:
        return [];
    }
  }

  function pad2(n) { return String(n).padStart(2, '0'); }

  // "Period 2 — 14:18 left", "End of period 2", "Final", or "—" when the clock is hidden by the veil.
  function clockLabel(state) {
    if (!state || state.period == null || state.seconds_remaining == null) return '—';
    if (state.status === 'FINAL' && state.is_complete) return 'Final';
    if (state.seconds_remaining === 0) return state.period >= (state.periods || 3) ? 'Final' : `End of period ${state.period}`;
    const m = Math.floor(state.seconds_remaining / 60), s = state.seconds_remaining % 60;
    return `Period ${state.period} — ${m}:${pad2(s)} left`;
  }

  // Game seconds -> {period, secondsRemaining}, the same arithmetic as the server (clock_at).
  function clockAt(gameSecond, periods, periodSeconds) {
    const total = periods * periodSeconds;
    const sec = Math.max(0, Math.min(gameSecond, total));
    const period = Math.max(1, Math.ceil(sec / periodSeconds));
    return {period, secondsRemaining: period * periodSeconds - sec};
  }

  // Merge a polled page into what we already show. Idempotent and order-safe: duplicates (a reconnect that
  // re-sends events) are dropped, and the list stays in seq order. Returns {events, lastSeq, added}.
  function mergeEvents(existing, incoming) {
    const bySeq = new Map();
    (existing || []).forEach(e => bySeq.set(e.seq, e));
    let added = 0;
    (incoming || []).forEach(e => { if (!bySeq.has(e.seq)) { bySeq.set(e.seq, e); added++; } });
    const events = Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
    return {events, lastSeq: events.length ? events[events.length - 1].seq : 0, added};
  }

  // The scoreboard after event `seq`, read straight off the stored running totals (never recomputed).
  function boardAt(events, seq, shape) {
    const e = events.filter(x => x.seq <= seq).pop();
    if (!e) return {home_score: 0, away_score: 0, home_shots: 0, away_shots: 0, period: 1,
                    secondsRemaining: shape ? shape.period_seconds : 1200, seq: 0, headline: null};
    const clock = shape ? clockAt(e.game_second, shape.periods, shape.period_seconds)
                        : {period: e.period, secondsRemaining: 0};
    return {home_score: e.home_score, away_score: e.away_score, home_shots: e.home_shots, away_shots: e.away_shots,
            period: clock.period, secondsRemaining: clock.secondsRemaining, seq: e.seq, headline: e.headline};
  }

  // Times (ms from the start of playback) at which to reveal each event so the game plays in about durationMs.
  // Proportional to game time with a minimum gap, non-decreasing, and the final event lands at the end.
  function replayTimeline(events, durationMs, minGapMs) {
    const dur = durationMs == null ? REPLAY_MS : durationMs;
    const gap = minGapMs == null ? MIN_GAP_MS : minGapMs;
    if (!events.length) return [];
    if (dur <= 0) return events.map(e => ({seq: e.seq, atMs: 0}));
    const first = events[0].game_second, last = events[events.length - 1].game_second;
    const span = Math.max(1, last - first);
    let prev = -Infinity;
    return events.map((e, i) => {
      let at = Math.round(((e.game_second - first) / span) * dur);
      if (i > 0 && at < prev + (e.game_second === events[i - 1].game_second ? 0 : gap)) at = prev + (e.game_second === events[i - 1].game_second ? 0 : gap);
      prev = at;
      return {seq: e.seq, atMs: at};
    });
  }

  // Delay before event i given a timeline built from `startIndex`.
  function delayBefore(timeline, i) { return i <= 0 ? 0 : Math.max(0, timeline[i].atMs - timeline[i - 1].atMs); }

  // Replay mode: reduced motion never auto-plays (the viewer steps or reveals everything at once).
  function replayMode(reducedMotion) { return reducedMotion ? 'manual' : 'auto'; }

  // Sequence numbers worth saving as progress while replaying: every goal, period end, and the last event.
  function isCheckpoint(e) { return e.kind === 'score' || e.kind === 'period_end' || e.kind === 'game_end'; }

  // Reconnect policy for polling: 1s, 2s, 4s ... capped at 30s.
  function backoffMs(failures) { return Math.min(MAX_BACKOFF_MS, 1000 * Math.pow(2, Math.max(0, failures - 1))); }

  // Poll cadence while watching live: quicker near the start of a request cycle is unnecessary; 5s is plenty
  // for a game that plays out over hours.
  function pollDelayMs(failures) { return failures > 0 ? backoffMs(failures) : 5000; }

  // The connection banner: '' when fine, otherwise what is happening.
  function connectionMessage(failures, retryInMs) {
    if (!failures) return '';
    return `Connection lost — showing the last update. Retrying in ${Math.max(1, Math.round(retryInMs / 1000))}s.`;
  }

  // Error -> {title, detail, action} for the page's error panel. `err` is what FSC.api throws (message, status, code).
  function describeError(err) {
    const status = err && err.status, code = err && err.code, msg = (err && err.message) || 'Something went wrong.';
    if (status === 401) return {title: 'Sign in to see this game', detail: msg, action: 'signin'};
    if (status === 404) return {title: 'Game not found', detail: msg, action: 'back'};
    if (code === 'NOT_FINAL') return {title: 'Not final yet', detail: msg, action: 'none'};
    if (code === 'NOT_STARTED') return {title: 'Not started yet', detail: msg, action: 'none'};
    if (code === 'REVEAL_REQUIRED') return {title: 'Open the game first', detail: msg, action: 'none'};
    if (status === 0 || msg === 'Failed to fetch') return {title: "Can't reach the server", detail: 'Check your connection.', action: 'retry'};
    return {title: "Couldn't load this game", detail: msg, action: 'retry'};
  }

  const ROLE_COLUMNS = [
    ['shooter', 'Shots'], ['primary_assist', 'A1'], ['secondary_assist', 'A2'], ['blocker', 'Blocks'], ['hitter', 'Hits'], ['takeaway', 'Take'], ['goalie', 'Saves'],
  ];

  function fmtPoints(v) {
    const n = Math.round(v * 100) / 100;
    return (n > 0 ? '+' : '') + (Object.is(n, -0) ? '0.00' : n.toFixed(2));
  }

  // Attribution box score per side from provenance.box_score. Totals are sums of the stored per-player points;
  // `tiesOut` is true when they match provenance.raw to the rounding the API uses.
  function boxScoreRows(provenance, side) {
    const players = ((provenance && provenance.box_score && provenance.box_score[side]) || []);
    const rows = players.map(p => ({
      name: p.name, position: p.position, total: p.points,
      cells: ROLE_COLUMNS.map(([role]) => (p.by_role && p.by_role[role] != null ? p.by_role[role] : null)),
    }));
    const total = players.reduce((a, p) => a + p.points, 0);
    const claimed = provenance && provenance.raw ? provenance.raw[side] : null;
    return {rows, total, tiesOut: claimed == null || Math.abs(total - claimed) < 1e-4};
  }

  // "2026-10-14T19:00:00" -> "7:00 PM". Nominal start times are stored as naive Eastern, so no timezone maths here.
  function fmtNominal(iso) {
    const m = /T(\d{2}):(\d{2})/.exec(String(iso || ''));
    if (!m) return '';
    const h = Number(m[1]);
    return `${h % 12 || 12}:${m[2]} ${h < 12 ? 'AM' : 'PM'}`;
  }

  // The chronological division table from provenance.division (docs/hockey_spec.md 6.1 and 9): one group per fantasy
  // period listing every real period assigned to it.
  function divisionPeriods(provenance) {
    const d = provenance && provenance.division;
    if (!d || !Array.isArray(d.periods)) return [];
    return d.periods.map(p => ({
      period: p.fantasy_period, count: p.real_periods,
      rows: p.rows.map(r => ({
        player: r.player, team: r.fantasy_team, game: `${r.opponent} · ${r.game_date}`,
        realPeriod: r.real_period, start: fmtNominal(r.nominal_start), ot: !!r.ot, points: r.points,
      })),
    }));
  }

  // Human lines for the provenance panel. Only states what the stored record says.
  function provenanceLines(p) {
    if (!p) return ['No provenance was stored for this game.'];
    const n = p.normalization || {};
    const lines = [`Built by ${p.adapter || 'unknown adapter'} v${p.adapter_version || '?'} with seed ${p.seed == null ? '?' : p.seed}.`];
    if (p.raw) lines.push(`Raw credit: home ${fmtPoints(p.raw.home)}, away ${fmtPoints(p.raw.away)} (margin ${fmtPoints(p.raw.margin)}).`);
    if (p.value) {
      const v = p.value;
      lines.push(`Value created: home ${v.created.home.toFixed(2)}, away ${v.created.away.toFixed(2)}. Saved by goalies: home ${v.saved.home.toFixed(2)}, away ${v.saved.away.toFixed(2)}.`);
      lines.push(`Net value after saves (any excess a goalie saved goes to his own team): home ${v.net.home.toFixed(2)}, away ${v.net.away.toFixed(2)} → expected goals home ${v.expected_goals.home.toFixed(2)}, away ${v.expected_goals.away.toFixed(2)}.`);
    }
    if (p.offense && !p.value) lines.push(`After turning opponent errors into chances: home ${p.offense.home.toFixed(2)}, away ${p.offense.away.toFixed(2)}.`);
    if (n.expected_margin != null) lines.push(`Expected goal margin ${n.expected_margin.toFixed(2)}; bounded variation ${fmtPoints(n.noise)} goals (limit ±${n.cap}).`);
    if (n.upset) lines.push('This result was an upset: the scoreboard winner trailed on raw credit, within the allowed window.');
    if (n.tiebreak_side) lines.push(`The rounded score was level; a deciding goal went to ${n.tiebreak_side}.`);
    if (p.inputs_digest) lines.push(`Input fingerprint ${String(p.inputs_digest).slice(0, 12)}…`);
    return lines;
  }

  // ---- game centre (period-by-period games) ----------------------------------------------------------------------------
  // phases: upcoming / in_progress / simulating / ready / watched (see platform/simulator/service.period_phases).
  function mmss(seconds) { const s = Math.max(0, Math.round(seconds)); return `${Math.floor(s / 60)}:${pad2(s % 60)}`; }

  // The live clock line. It is public: it never hints at what happened.
  function liveClockLabel(state, elapsedSinceReadSeconds) {
    if (!state || state.period == null || state.seconds_remaining == null) return '—';
    if (state.status === 'FINAL') return 'Final';
    const left = Math.max(0, state.seconds_remaining - (elapsedSinceReadSeconds || 0));
    return left === 0 ? `End of period ${state.period}` : `Period ${state.period} — ${mmss(left)} left`;
  }

  // What one period's tile says: {title, detail, tone, action}. `action` is 'watch' | 'replay' | null.
  function phaseTile(p, firstReady, elapsedSinceReadSeconds) {
    const n = p.period;
    switch (p.phase) {
      case 'upcoming': return {title: `Period ${n}`, detail: 'Not started', tone: 'idle', action: null};
      case 'in_progress': return {title: `Period ${n} in progress`, detail: `${mmss(Math.max(0, p.seconds_remaining - (elapsedSinceReadSeconds || 0)))} left`, tone: 'live', action: null};
      case 'simulating': return {title: `Intermission`, tone: 'sim', action: null, percent: Math.max(0, Math.min(100, p.percent || 0)),
        detail: p.waiting_for ? `Waiting for period ${p.waiting_for} to finish simulating.` : 'The Zamboni is currently on the simulator.'};
      case 'ready': return {title: firstReady === n ? `Watch period ${n} now!` : `Period ${n} ready`, detail: firstReady === n ? 'Highlights are ready. Watching updates the score.' : `Watch period ${firstReady} first.`, tone: firstReady === n ? 'ready' : 'idle', action: firstReady === n ? 'watch' : null};
      default: return {title: `Period ${n}`, detail: 'Watched', tone: 'done', action: 'replay'};
    }
  }

  function firstReadyPeriod(phases) { const p = (phases || []).find(x => x.phase === 'ready'); return p ? p.period : null; }

  // Everything the hub needs to decide whether to redraw (not just refresh the clock).
  function phaseSignature(state) { return (state.period_phases || []).map(p => p.phase + (p.phase === 'simulating' ? ':' + (p.percent || 0) : '')).join('|') + '#' + (state.shown_seq || 0) + '#' + state.status; }

  // The events of one period (its period_start through its period_end / game_end).
  function periodEvents(events, period) { return (events || []).filter(e => e.period === period); }

  const api = {mmss, liveClockLabel, phaseTile, firstReadyPeriod, phaseSignature, periodEvents, REPLAY_MS, MIN_GAP_MS, screenFor, controlsFor, clockLabel, clockAt, mergeEvents, boardAt, replayTimeline,
               delayBefore, replayMode, isCheckpoint, backoffMs, pollDelayMs, connectionMessage, describeError,
               ROLE_COLUMNS, fmtPoints, boxScoreRows, provenanceLines, fmtNominal, divisionPeriods};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NhlGame = api;
})(typeof window !== 'undefined' ? window : this);
