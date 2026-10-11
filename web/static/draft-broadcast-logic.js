// Pure logic for the draft broadcast room (draft-board.html). No DOM, no fetch, no speech: the page passes in what the
// server announced (GET /drafts/{id}/announcements) and this decides what is new, what is worth speaking and how to
// word it. Commentary text comes from the server; nothing here writes or estimates any statistic.
// Checked under Node by scripts/verify_draft_broadcast.py.
(function (root) {
  const SUPPORTED_MAJOR = 1;
  const MAX_SPOKEN_BACKLOG = 1;   // if several picks land at once, only the newest is spoken

  function versionSupported(v) {
    const major = Number(String(v || '').split('.')[0]);
    return Number.isInteger(major) && major === SUPPORTED_MAJOR;
  }

  // Merge announcements into the list we already hold. Duplicate event_ids (a repeated poll, a reconnect backfill)
  // are dropped; the result is in pick order. `fresh` is only what was not already known.
  function merge(existing, incoming) {
    const known = new Map((existing || []).map(a => [a.event_id, a]));
    const fresh = [];
    (incoming || []).forEach(a => {
      if (!a || !versionSupported(a.version) || known.has(a.event_id)) return;
      known.set(a.event_id, a);
      fresh.push(a);
    });
    const list = Array.from(known.values()).sort((a, b) => a.pick_number - b.pick_number);
    fresh.sort((a, b) => a.pick_number - b.pick_number);
    return {list, fresh, lastPick: list.length ? list[list.length - 1].pick_number : 0};
  }

  // What to speak after a poll. Nothing during the first load or a backfill, nothing when sound is off or the browser
  // has no speech support; otherwise the newest announcement(s).
  function toSpeak(fresh, opts) {
    if (!opts || !opts.soundOn || !opts.supported || opts.backfill) return [];
    return (fresh || []).slice(-MAX_SPOKEN_BACKLOG);
  }

  function headline(a) {
    return `Pick ${a.pick_number} · ${a.team.name} select ${a.player.name}${a.player.position ? ' (' + a.player.position + ')' : ''}`;
  }

  // Text for the screen-reader live region (the visible banner is not itself a live region, to avoid double reads).
  function announcementText(a) {
    return `${headline(a)}. ${a.commentary.text}`;
  }

  function toneOf(a) {
    return {value: 'good', reach: 'warn', fair: 'neutral', unknown: 'muted'}[a.commentary.value_label] || 'muted';
  }

  const TONE_LABEL = {value: 'Value', reach: 'Reach', fair: 'Fair', unknown: 'No value call'};
  function toneLabel(a) { return TONE_LABEL[a.commentary.value_label] || TONE_LABEL.unknown; }

  // The line shown when inputs were missing, so the viewer can see why there is no value call.
  function unavailableNote(a) {
    const u = (a.commentary.unavailable || []);
    return u.length ? `Not available for this pick: ${u.join(', ')}.` : '';
  }

  function soundLabel(soundOn, supported) {
    if (!supported) return {text: 'Sound unavailable in this browser', disabled: true, pressed: false};
    return {text: soundOn ? 'Sound on' : 'Sound off', disabled: false, pressed: !!soundOn};
  }

  // Backoff for polling: 5s normally, doubling to 60s while failing.
  function pollDelayMs(failures) { return failures ? Math.min(60000, 5000 * Math.pow(2, failures)) : 5000; }

  const api = {SUPPORTED_MAJOR, versionSupported, merge, toSpeak, headline, announcementText, toneOf, toneLabel,
               unavailableNote, soundLabel, pollDelayMs};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.DraftBroadcast = api;
})(typeof window !== 'undefined' ? window : this);
