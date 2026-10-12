"""
Browser-independent verification for the NHL live simulator, replay and box-score page (FS-027).

Publishes a real hockey script (FS-026 adapter, synthetic credits) for one series daily game, drives the real
FS-025 reveal API through the FastAPI TestClient with an injected clock to produce every state the page must
handle - unpublished, scheduled, live (veiled and watched), final (veiled, replaying, skipped, replayed) - then
feeds those live payloads through the page's pure logic (web/static/nhl-game-logic.js) under Node. It also
checks the served page for structure, accessibility hooks and the absence of external assets or any outcome
maths. No browser, network or provider is used.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_simulator_ui.py
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

NODE_CHECK = r"""
const fs = require('fs');
const G = require(process.argv[2]);
const d = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const ok = (c, m) => { if (!c) { console.error('FAIL ' + m); process.exit(1); } console.log('ok  ' + m); };

// ---- screens: one per state the API can produce ----
const S = d.states;
ok(G.screenFor(S.unpublished) === 'unpublished', 'unpublished game -> unpublished screen');
ok(G.screenFor(S.scheduled) === 'scheduled', 'published but not started -> scheduled screen');
ok(G.screenFor(S.live_veiled) === 'live-veiled', 'live, not opened -> spoiler veil');
ok(G.screenFor(S.live_watching) === 'live', 'live, watching -> live screen');
ok(G.screenFor(S.final_veiled) === 'final-veiled', 'final, not opened -> final hidden behind the veil');
ok(G.screenFor(S.replay_mid) === 'replay', 'final, watching a replay -> replay screen');
ok(G.screenFor(S.replayed) === 'final', 'final, replay finished -> box score');
ok(G.screenFor(S.skipped) === 'final', 'final, skipped -> box score');
ok(G.screenFor(null) === 'unpublished', 'no state at all is treated as nothing published');

// ---- the veil: the server withholds, and the page never shows what it was not given ----
for (const k of ['unpublished', 'scheduled', 'live_veiled', 'final_veiled']) {
  ok(S[k].home_score === null || S[k].home_score === undefined, k + ': no score in the payload');
  ok(G.clockLabel(S[k]) === '—', k + ': clock reads a dash, not a guess');
}

// ---- controls: independent watch / skip, with reasons ----
const lv = G.controlsFor('live-veiled', S.live_veiled);
ok(lv.find(c => c.id === 'watch').enabled && !lv.find(c => c.id === 'skip').enabled && lv.find(c => c.id === 'skip').why,
   'live veil: watch enabled; skip disabled with a reason');
const fv = G.controlsFor('final-veiled', S.final_veiled);
ok(fv.find(c => c.id === 'watch').enabled && fv.find(c => c.id === 'skip').enabled, 'final veil: watch replay and skip both enabled');
ok(G.controlsFor('replay', S.replay_mid).some(c => c.id === 'skip' && c.enabled), 'replay can still skip to the box score');
ok(G.controlsFor('final', S.skipped).length === 0 && G.controlsFor('scheduled', S.scheduled).length === 0, 'no controls where there is nothing to choose');

// ---- clock ----
ok(G.clockLabel(S.live_watching).startsWith('Period '), 'live clock label: ' + G.clockLabel(S.live_watching));
ok(G.clockLabel({period: 2, seconds_remaining: 858, periods: 3, status: 'LIVE'}) === 'Period 2 — 14:18 left', 'spec example "Period 2 — 14:18 left"');
ok(G.clockLabel({period: 1, seconds_remaining: 0, periods: 3, status: 'LIVE'}) === 'End of period 1', 'end of a period');
ok(G.clockLabel(S.skipped) === 'Final', 'completed game reads Final');
const c = G.clockAt(1200, 3, 1200), c2 = G.clockAt(1201, 3, 1200), c3 = G.clockAt(0, 3, 1200);
ok(c.period === 1 && c.secondsRemaining === 0 && c2.period === 2 && c2.secondsRemaining === 1199 && c3.period === 1 && c3.secondsRemaining === 1200,
   'clock arithmetic matches the server at period boundaries');

// ---- the scoreboard comes from API events, never recomputed ----
const ev = d.events;
const shape = {periods: S.replay_mid.periods, period_seconds: S.replay_mid.period_seconds};
for (const e of ev) {
  const b = G.boardAt(ev, e.seq, shape);
  if (b.home_score !== e.home_score || b.away_score !== e.away_score || b.home_shots !== e.home_shots || b.away_shots !== e.away_shots) {
    console.error('FAIL board differs from event totals at seq ' + e.seq); process.exit(1);
  }
}
console.log('ok  boardAt reads the stored running totals at every event (' + ev.length + ' events)');
const mid = G.boardAt(ev, S.replay_mid.cursor_seq, shape);
ok(mid.home_score === S.replay_mid.home_score && mid.away_score === S.replay_mid.away_score,
   'replay scoreboard at the saved cursor equals the server-reported scoreboard');
const live = G.boardAt(d.live_events, S.live_watching.shown_seq, shape);
ok(live.home_score === S.live_watching.home_score && live.away_score === S.live_watching.away_score,
   'live scoreboard from polled events equals the server-reported scoreboard');
let prev = G.boardAt(ev, 0, shape), monotone = true;
for (const e of ev) { const b = G.boardAt(ev, e.seq, shape); monotone = monotone && b.home_score >= prev.home_score && b.away_score >= prev.away_score; prev = b; }
ok(monotone, 'displayed scores never decrease across the whole replay');
ok(G.boardAt(ev, 0, shape).home_score === 0 && G.boardAt(ev, 0, shape).secondsRemaining === shape.period_seconds, 'before the first event the board is 0-0 at the top of period 1');

// ---- feed merge: reconnect and duplicate safety ----
let m = G.mergeEvents([], ev.slice(0, 5));
ok(m.events.length === 5 && m.lastSeq === 5 && m.added === 5, 'first page merges in order');
m = G.mergeEvents(m.events, ev.slice(3, 9));
ok(m.events.length === 9 && m.added === 4 && m.lastSeq === 9, 'an overlapping page after a reconnect adds only the new events');
const again = G.mergeEvents(m.events, ev.slice(3, 9));
ok(again.added === 0 && again.events.length === 9, 'merging the same page twice changes nothing (idempotent)');
const shuffled = G.mergeEvents([], ev.slice().reverse());
ok(shuffled.events.every((e, i) => e.seq === i + 1), 'out-of-order input is sorted by seq');

// ---- replay timeline: about 75 seconds, ordered, skippable ----
const tl = G.replayTimeline(ev);
ok(tl.length === ev.length && tl[0].atMs === 0, 'timeline has one entry per event, starting at 0');
ok(tl.every((t, i) => i === 0 || t.atMs >= tl[i - 1].atMs), 'timeline never goes backwards');
ok(tl[tl.length - 1].atMs >= 74000 && tl[tl.length - 1].atMs <= 76000, 'a full game replays in about 75 seconds (' + tl[tl.length - 1].atMs + ' ms)');
const gaps = tl.map((t, i) => i ? t.atMs - tl[i - 1].atMs : 0).filter((g, i) => i && ev[i].game_second !== ev[i - 1].game_second);
ok(gaps.every(g => g >= G.MIN_GAP_MS), 'events at different times are never closer than the minimum gap');
ok(G.replayTimeline(ev, 0).every(t => t.atMs === 0), 'instant reveal collapses the timeline to zero');
const resumed = G.replayTimeline(ev.slice(10));
ok(resumed[0].atMs === 0 && resumed.length === ev.length - 10, 'resuming from a cursor replays only the rest');
ok(G.delayBefore(tl, 0) === 0 && G.delayBefore(tl, 3) === tl[3].atMs - tl[2].atMs, 'per-event delays come from the timeline');
ok(G.replayMode(true) === 'manual' && G.replayMode(false) === 'auto', 'reduced motion switches replay to manual stepping');
ok(ev.filter(G.isCheckpoint).every(e => ['score', 'period_end', 'game_end'].includes(e.kind)) && G.isCheckpoint(ev[ev.length - 1]),
   'progress is saved at goals, period ends and the final event');

// ---- disconnected state ----
ok([1, 2, 3, 4, 5, 6, 7].map(G.backoffMs).join() === '1000,2000,4000,8000,16000,30000,30000', 'reconnect backoff doubles up to 30 seconds');
ok(G.pollDelayMs(0) === 5000 && G.pollDelayMs(2) === 2000, 'healthy polling every 5s; failing polling backs off');
ok(G.connectionMessage(0, 0) === '' && /Connection lost/.test(G.connectionMessage(2, 2000)) && /2s/.test(G.connectionMessage(2, 2000)),
   'the connection banner is empty when healthy and says what is happening when not');

// ---- error states ----
ok(G.describeError({status: 401, message: 'x'}).action === 'signin', '401 -> sign in');
ok(G.describeError({status: 404, message: 'x'}).action === 'back', '404 -> back');
ok(G.describeError({status: 409, code: 'NOT_FINAL', message: 'not yet'}).title === 'Not final yet', 'NOT_FINAL has its own message');
ok(G.describeError({status: 403, code: 'REVEAL_REQUIRED', message: 'open it'}).title === 'Open the game first', 'REVEAL_REQUIRED has its own message');
ok(G.describeError(new Error('Failed to fetch')).action === 'retry', 'a network failure offers retry');
ok(G.describeError({status: 500, message: 'boom'}).action === 'retry' && G.describeError({}).detail.length > 0, 'unknown errors offer retry with text');

// ---- attribution box score + provenance ----
const prov = d.final.provenance;
for (const side of ['home', 'away']) {
  const b = G.boxScoreRows(prov, side);
  ok(b.rows.length > 0 && b.tiesOut, side + ' box score has players and ties out to the stored team total');
  ok(Math.abs(b.total - prov.raw[side]) < 1e-4, side + ' box score total equals raw credit ' + prov.raw[side]);
  ok(b.rows.every((r, i) => i === 0 || r.total <= b.rows[i - 1].total), side + ' rows are best first');
}
ok(G.boxScoreRows(null, 'home').rows.length === 0 && G.boxScoreRows({}, 'away').tiesOut, 'missing provenance gives an empty, honest box score');
ok(G.fmtPoints(0.5) === '+0.50' && G.fmtPoints(-0.8) === '-0.80' && G.fmtPoints(-0.001) === '0.00', 'points format with sign and two decimals');
const lines = G.provenanceLines(prov);
ok(lines[0].includes('hockey-xg') && lines.some(l => l.includes('Raw credit')) && lines.some(l => l.includes('Expected goal margin')), 'provenance lines state adapter, raw credit and normalization');
ok(G.provenanceLines(null)[0].includes('No provenance'), 'absent provenance is stated, not invented');
"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    import audit_hockey_simulator_scoring as audit  # reuse the synthetic credit builders

    with tempfile.TemporaryDirectory(prefix="fs027-ui-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'ui').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import series_schedule
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            League, LeagueUniverse, SeriesDailyGame, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.platform.simulator import service
        from fantasy_sports.sports.hockey.simulator import HockeySimulatorAdapter

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)
        start = datetime.date(2026, 10, 14)

        with SessionLocal() as db:
            commissioner = User(email="comm@example.com", display_name="Commissioner")
            fan, fan2 = User(email="fan@example.com", display_name="Fan"), User(email="fan2@example.com", display_name="Fan2")
            db.add_all([commissioner, fan, fan2])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            league = League(name="UI League", commissioner_user_id=commissioner.id, size=4, series_length_days=5)
            db.add(league)
            db.flush()
            db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
            for name in ("Harbor Hawks", "Lake Lynx", "Ridge Rams", "Delta Dogs"):
                db.add(Team(league_id=league.id, name=name, is_ai_owned=True))
            db.flush()
            series_schedule.generate_schedule(db, league, start)
            db.commit()
            games = db.query(SeriesDailyGame).order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id).all()
            ids = [g.id for g in games[:7]]
            tokens = {n: create_session(db, u.id).token for n, u in dict(commissioner=commissioner, fan=fan, fan2=fan2).items()}
            league_id = league.id
        auth = {n: {"Authorization": f"Bearer {t}"} for n, t in tokens.items()}
        (g_unpub, g_sched, g_live, g_final, g_replay, g_skip, g_replayed) = ids

        home = audit.offense_lines("H", 100, 3.4) + [audit.line("hb", "hb", 110, "H Blocker", "D", "blocker", 0.05),
                                                     audit.line("hs", "hs", 120, "H Goalie", "G", "goalie", 0.30),
                                                     audit.line("hga", "hga", 120, "H Goalie", "G", "goalie", -0.80)]
        away = audit.offense_lines("A", 200, 2.0) + [audit.line("ags", "ags", 220, "A Goalie", "G", "goalie", -0.45)]
        from fantasy_sports.sports.hockey.simulator import ShotAttempt
        home += [audit.line(f"hsv{i}", f"hsv{i}", 120, "H Goalie", "G", "goalie", 0.04 + i / 100, sec=400 * i + 50) for i in range(1, 7)]
        away += [audit.line(f"asv{i}", f"asv{i}", 220, "A Goalie", "G", "goalie", 0.05 + i / 100, sec=450 * i + 20) for i in range(1, 6)]
        home_attempts = [ShotAttempt(f"ha{i}", 100 + i, f"H Skater{i}", 300 * i, 0.05 * i) for i in range(1, 8)]
        away_attempts = [ShotAttempt(f"aa{i}", 200 + i, f"A Skater{i}", 280 * i, 0.04 * i) for i in range(1, 8)]
        adapter = HockeySimulatorAdapter()
        with SessionLocal() as db:
            for gid in (g_sched, g_live, g_final, g_replay, g_skip, g_replayed):
                inputs = audit.make_inputs(home, away, home_attempts, away_attempts, game_id=gid)
                service.publish_script(db, db.get(SeriesDailyGame, gid), adapter.build_script(inputs, gid))
            db.commit()

        t0 = datetime.datetime(2026, 10, 14, 19, 0, 0)
        clock = {"now": t0}
        service.utcnow = lambda: clock["now"]
        window = 3 * 3600

        def at(seconds):
            clock["now"] = t0 + datetime.timedelta(seconds=seconds)

        def state(gid, who="fan"):
            return client.get(f"/simulator/games/{gid}/state", headers=auth[who]).json()

        def post(gid, action, who="fan", body=None):
            r = client.post(f"/simulator/games/{gid}/{action}", json=body, headers=auth[who])
            return r

        # Games that run live now, finish later.
        at(0)
        for gid in (g_live, g_final, g_replay, g_skip, g_replayed):
            check(post(gid, "start", "commissioner").status_code == 200, f"game {gid} started")
        at(window * 0.45)
        states: dict = {}
        states["unpublished"] = state(g_unpub)
        states["scheduled"] = state(g_sched)
        states["live_veiled"] = state(g_live)
        check(post(g_live, "watch").status_code == 200, "fan opens the live game")
        states["live_watching"] = state(g_live)
        live_events = client.get(f"/simulator/games/{g_live}/events?limit=500", headers=auth["fan"]).json()["events"]

        at(window + 5)
        states["final_veiled"] = state(g_final)
        check(states["final_veiled"]["status"] == "FINAL" and states["final_veiled"]["veiled"], "a finished game stays veiled until opened")

        post(g_replay, "watch")
        events = client.get(f"/simulator/games/{g_replay}/events?limit=500", headers=auth["fan"]).json()["events"]
        check(len(events) > 20, f"the published hockey game has a full feed ({len(events)} events)")
        cursor = events[len(events) // 2]["seq"]
        post(g_replay, "progress", body={"cursor_seq": cursor})
        states["replay_mid"] = state(g_replay)
        post(g_skip, "skip")
        states["skipped"] = state(g_skip)
        post(g_replayed, "watch")
        replayed_events = client.get(f"/simulator/games/{g_replayed}/events?limit=500", headers=auth["fan"]).json()["events"]
        post(g_replayed, "progress", body={"cursor_seq": replayed_events[-1]["seq"]})
        states["replayed"] = state(g_replayed)
        final = client.get(f"/simulator/games/{g_skip}/final", headers=auth["fan"]).json()
        check(client.get(f"/simulator/games/{g_replay}/final", headers=auth["fan"]).status_code == 403,
              "a half-watched replay cannot read the final (reveal required)")
        check(states["live_veiled"]["home_score"] is None and states["final_veiled"]["home_score"] is None,
              "veiled states carry no score")
        check(states["live_veiled"]["home_team"]["name"] and states["live_veiled"]["calendar_date"],
              "teams and date are present on every state (not spoilers)")

        # Another viewer is unaffected by fan's choices (independent reveals).
        check(state(g_skip, "fan2")["veiled"], "a second viewer is still veiled after fan skipped")
        check(final["provenance"]["adapter"] == "hockey-xg" and final["provenance"]["box_score"]["home"],
              "the final snapshot carries provenance and the attribution box score")

        # ---- the served page ----------------------------------------------------------------------------
        r = client.get("/static/nhl-game.html")
        html = r.text
        check(r.status_code == 200, "nhl-game.html is served")
        for marker, why in [
            ('id="screen"', "screen host"), ('id="board-h"', "scoreboard heading"), ('role="group" aria-label="Scoreboard"', "scoreboard group"),
            ('id="sc-home"', "home score"), ('id="sc-away"', "away score"), ('id="clock"', "period clock"),
            ('id="sh-home"', "home shots"), ('id="sh-away"', "away shots"), ('id="ticker-host"', "event ticker"),
            ('aria-label="Play-by-play, newest first"', "ticker label"), ('id="announce"', "screen-reader announcer"),
            ('aria-live="polite"', "polite live regions"), ('role="alert"', "errors announced"), ('role="progressbar"', "replay progress"),
            ('data-act="skip"', "skip control"), ("name === 'watch'", "watch control"), ('data-act="pause"', "pause control"),
            ('data-act="step"', "step control"), ('data-act="reveal"', "instant reveal control"),
            ('Step manually (no animation)', "reduced-motion toggle"), ('prefers-reduced-motion', "reduced-motion CSS and detection"),
            ('Attribution box score', "attribution box score"), ('Data provenance', "provenance panel"),
            ('This game has not been simulated', "unpublished state copy"), ('The result is hidden', "final-hidden veil copy"),
            ('@media (max-width:720px)', "narrow-screen layout"), ('focus-visible', "visible keyboard focus"),
            ('aria-disabled', "disabled controls say so"), ('showConn(G.connectionMessage', "disconnected banner wiring"),
            ('"online"', "reconnects when the browser is back online"),
        ]:
            check(marker in html or marker.replace('"', "'") in html, f"page has {why}")
        for endpoint in ("/watch", "/skip", "/progress", "/events?after_seq=", "/final", "/state"):
            check(endpoint in html, f"page calls {endpoint}")
        check(not re.search(r'(src|href)=["\']https?://', html) and "@import" not in html, "no external assets")
        check(not re.search(r"Math\.random|EventSource|WebSocket|new Audio|<video|<audio", html), "no randomness, sockets or media")
        check(not re.search(r"home_score\s*\+|away_score\s*\+|home_score\s*=[^=]|away_score\s*=[^=]", html),
              "the page never computes or assigns a score")
        check(client.get("/static/nhl-game-logic.js").status_code == 200, "logic module is served")
        check("nhl-game.html?daily_game_id=" in client.get("/static/league.html").text,
              "the league viewer links each game day to the game page")
        check(client.get("/static/session.js").text.count("err.code") >= 1, "session.js passes structured error codes to pages")

        # ---- logic over the live payloads under Node --------------------------------------------------------
        node = shutil.which("node")
        check(node is not None, "node is available to run the page logic checks")
        fixtures = Path(temp_dir) / "payloads.json"
        script = Path(temp_dir) / "check.js"
        fixtures.write_text(json.dumps({"states": states, "events": events, "live_events": live_events, "final": final}), encoding="utf-8")
        script.write_text(NODE_CHECK, encoding="utf-8")
        proc = subprocess.run([node, str(script), str(ROOT / "web" / "static" / "nhl-game-logic.js"), str(fixtures)],
                              capture_output=True, text=True)
        print(proc.stdout, end="")
        check(proc.returncode == 0, "page logic checks pass under Node" + (f": {proc.stderr.strip()}" if proc.returncode else ""))

        inline = re.findall(r"<script>(.*?)</script>", html, re.S)
        (Path(temp_dir) / "inline.js").write_text(inline[-1], encoding="utf-8")
        proc = subprocess.run([node, "--check", str(Path(temp_dir) / "inline.js")], capture_output=True, text=True)
        check(proc.returncode == 0, "page script is syntactically valid" + (f": {proc.stderr.strip()}" if proc.returncode else ""))
        engine.dispose()

    print("\nAll FS-027 simulator UI checks passed.")


if __name__ == "__main__":
    main()
