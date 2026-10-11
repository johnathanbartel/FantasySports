// Pure view logic for the draft board's pick clock (draft-board.html). No DOM, no fetch. The server owns the
// clock: every deadline, pause and overnight freeze comes from GET /drafts/{id}/clock; this only formats it and
// ticks the display down between polls. Checked under Node by scripts/verify_async_snake_draft.py.
(function (root) {
  const MIN_PICK_SECONDS = 10;
  const MAX_PICK_SECONDS = 7 * 86400;

  function pad2(n) { return String(n).padStart(2, '0'); }

  // 45 -> "0:45", 754 -> "12:34", 4500 -> "1h 15m", 190000 -> "2d 4h"
  function formatRemaining(seconds) {
    if (seconds == null) return '—';
    const s = Math.max(0, Math.floor(seconds));
    if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
    if (s >= 3600) return `${Math.floor(s / 3600)}h ${pad2(Math.floor((s % 3600) / 60))}m`;
    return `${Math.floor(s / 60)}:${pad2(s % 60)}`;
  }

  // Seconds left to show right now. Only a running clock counts down; paused and overnight clocks stay frozen at
  // the server's figure (the server already excludes overnight time from remaining_seconds).
  function liveRemaining(clock, nowMs, fetchedAtMs) {
    if (!clock || clock.remaining_seconds == null) return null;
    if (clock.clock_status !== 'running') return clock.remaining_seconds;
    return Math.max(0, clock.remaining_seconds - Math.floor((nowMs - fetchedAtMs) / 1000));
  }

  function offsetLabel(minutes) {
    const m = minutes || 0, sign = m < 0 ? '-' : '+', a = Math.abs(m);
    return `UTC${sign}${pad2(Math.floor(a / 60))}:${pad2(a % 60)}`;
  }

  // The one-line status under the countdown. The overnight times are the commissioner's configured local times.
  function statusLine(clock) {
    if (!clock) return '';
    switch (clock.clock_status) {
      case 'untimed': return 'No pick clock';
      case 'paused': return 'Paused by the commissioner — the time left is frozen';
      case 'overnight': {
        const o = clock.overnight;
        return `Overnight pause — the clock resumes at ${o ? o.end : '?'} (${o ? offsetLabel(o.utc_offset_minutes) : ''}); the time left is frozen`;
      }
      case 'complete': return 'Draft complete';
      default: return clock.mode === 'async' ? 'Slow draft — the clock is running' : 'Live draft — the clock is running';
    }
  }

  function overnightNote(clock) {
    const o = clock && clock.overnight;
    return o ? `Overnight pause ${o.start}–${o.end} (${offsetLabel(o.utc_offset_minutes)}): time does not count down.` : '';
  }

  // Seconds until the AI team on the clock makes its pick (null when a person is on the clock).
  function aiRemaining(clock, nowMs, fetchedAtMs) {
    if (!clock || clock.ai_in_seconds == null) return null;
    if (clock.clock_status === 'paused' || clock.clock_status === 'complete') return null;
    return Math.max(0, clock.ai_in_seconds - (nowMs - fetchedAtMs) / 1000);
  }

  // Whether the page should ask the server to apply a pick now: an expired clock, or an AI team's thinking time is up.
  function shouldProcess(clock, nowMs, fetchedAtMs) {
    if (!clock) return false;
    const ai = aiRemaining(clock, nowMs, fetchedAtMs);
    if (ai != null && ai <= 0) return true;
    if (clock.clock_status !== 'running') return false;
    const left = liveRemaining(clock, nowMs, fetchedAtMs);
    return left != null && left <= 0;
  }

  // How often to poll the clock: quickly for live drafts (and while an AI team is on the clock), slowly for slow ones,
  // never for finished drafts or untimed drafts with only people on the clock.
  function pollDelayMs(clock) {
    if (!clock || clock.clock_status === 'complete') return 0;
    if (clock.ai_in_seconds != null) return 3000;
    if (clock.clock_status === 'untimed') return 0;
    return clock.mode === 'async' ? 30000 : 5000;
  }

  // Form -> POST /drafts/start body. form: {mode: 'none'|'live'|'slow', value, overnight, from, to, offsetMinutes}.
  function startBody(form) {
    if (!form || form.mode === 'none') return {body: {}, error: null};
    const n = Number(form.value);
    if (!Number.isFinite(n) || n <= 0) return {body: null, error: 'Enter how long each pick lasts.'};
    const seconds = Math.round(form.mode === 'slow' ? n * 3600 : n);
    if (seconds < MIN_PICK_SECONDS) return {body: null, error: `A pick must last at least ${MIN_PICK_SECONDS} seconds.`};
    if (seconds > MAX_PICK_SECONDS) return {body: null, error: 'A pick can last at most 7 days.'};
    const body = {pick_seconds: seconds};
    if (form.overnight) {
      if (!form.from || !form.to) return {body: null, error: 'Choose when the overnight pause starts and ends.'};
      if (form.from === form.to) return {body: null, error: 'The overnight pause must start and end at different times.'};
      body.overnight_start = form.from;
      body.overnight_end = form.to;
      body.utc_offset_minutes = form.offsetMinutes || 0;
    }
    return {body, error: null};
  }

  const api = {MIN_PICK_SECONDS, MAX_PICK_SECONDS, formatRemaining, liveRemaining, offsetLabel, statusLine, overnightNote,
               aiRemaining, shouldProcess, pollDelayMs, startBody};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.DraftClock = api;
})(typeof window !== 'undefined' ? window : this);
