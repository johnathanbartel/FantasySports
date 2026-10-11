"""
Browser-independent verification for the NHL off-day banking UI (FS-029).

Builds a throwaway NHL league, produces every credit state through the real FS-028 API with an injected clock
(available, capped, assigned, auto-assigned, expired) and every recommendation shape (neutral, partial, full, none),
then feeds those payloads through the page's pure logic (web/static/nhl-banking-logic.js) under Node and checks the
served page for structure, accessibility hooks, double-submit protection and the absence of any scoring or
notification code. No browser, network or provider is used.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_banking_ui.py
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

NODE_CHECK = r"""
const fs = require('fs');
const B = require(process.argv[2]);
const d = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const ok = (c, m) => { if (!c) { console.error('FAIL ' + m); process.exit(1); } console.log('ok  ' + m); };
const dayLabel = (s) => s;   // the page passes V.dayLabel; labels are not under test here
const now = B.parseUtc(d.now);

// ---- the six credit situations ----
const C = d.credits;
ok(B.stateOf(C.available).key === 'available' && B.stateOf(C.available).tone === 'attention', 'AVAILABLE -> available');
ok(B.stateOf(C.assigned).key === 'assigned', 'ASSIGNED + MANUAL -> assigned');
ok(B.stateOf(C.auto).key === 'auto-assigned', 'ASSIGNED + AUTO -> auto-assigned');
ok(B.stateOf(C.expired).key === 'expired', 'EXPIRED -> expired');
ok(B.stateOf(C.capped).capped === true && B.stateOf(C.capped).key === 'available', 'a capped credit is also available (capped is its own flag)');
ok(B.stateOf(C.assigned_capped).capped === true && B.stateOf(C.assigned_capped).key === 'assigned', 'a capped credit stays flagged once assigned');
ok(B.stateOf(C.available).capped === false, 'an uncapped credit is not flagged');
ok(B.pointsText(C.available, 1.5) === 'Earned 1.35', 'earned points are shown as stored');
ok(B.pointsText(C.capped, 1.5) === 'Earned 1.80 · 1.50 applies', 'a capped credit shows earned and applied values');
ok(B.pointsText(C.assigned_capped, 1.5) === 'Earned 1.70 · 1.50 applies', 'an assigned capped credit shows the applied value from the API');

// ---- countdown + alert ----
const cd = B.fallbackCountdown(C.available, now);
ok(!cd.overdue && /^Banks automatically in \d+h \d+m$/.test(cd.text), 'countdown in hours: ' + cd.text);
ok(B.fallbackCountdown(C.available, B.parseUtc(C.available.fallback_at) - 30 * 60000).text === 'Banks automatically in 30m', 'countdown in minutes');
ok(B.fallbackCountdown(C.available, B.parseUtc(C.available.fallback_at) + 3600000).overdue === true, 'past the fallback time it says it is due');
const a1 = B.alertSummary([C.available, C.assigned, C.expired], now);
ok(a1 && a1.count === 1 && a1.text.includes(C.available.player_name) && a1.text.includes('1.35'), 'single waiting credit: names the player and the points');
ok(a1.firstId === C.available.id && a1.urgent === false, 'the alert points at the credit and is not urgent a day out');
const a2 = B.alertSummary([C.available, C.capped], now, 1.5);
ok(a2.count === 2 && /2 off-day credits/.test(a2.text) && Math.abs(a2.total - (1.35 + 1.5)) < 1e-9, 'several waiting credits: count, and the total at the cap');
ok(B.alertSummary([C.available], B.parseUtc(C.available.fallback_at) - 5 * 3600000).urgent === true, 'under six hours to go the alert is urgent');
ok(B.alertSummary([C.assigned, C.auto, C.expired], now) === null && B.alertSummary([], now) === null, 'no waiting credits -> no alert');

// ---- tab badges + where credits ended up ----
const badges = B.tabBadges([C.assigned, C.auto, C.available, C.expired]);
ok(badges[C.assigned.assigned_daily_game_id].points === C.assigned.applied_points && badges[C.assigned.assigned_daily_game_id].auto === false, 'a manual assignment badges its game tab');
ok(badges[C.auto.assigned_daily_game_id].auto === true, 'an automatic assignment badges its tab as auto');
ok(Object.keys(badges).length === 2, 'available and expired credits badge nothing');
const idx = B.gameIndex(d.schedule, dayLabel);
ok(idx[C.assigned.assigned_daily_game_id] && idx[C.assigned.assigned_daily_game_id].opponent.length > 0, 'the schedule payload indexes games by daily game id');
ok(/^Banked to .+ (vs|@) .+\.$/.test(B.outcomeText(C.assigned, idx)), 'assigned outcome: ' + B.outcomeText(C.assigned, idx));
ok(/automatically .* after 24 hours/.test(B.outcomeText(C.auto, idx)), 'auto-assigned outcome says it was automatic');
ok(/Expired: no open game/.test(B.outcomeText(C.expired, idx)), 'expired outcome says why');
ok(B.outcomeText(C.available, idx) === '', 'available credits have no outcome line');

// ---- recommendation: never overstate ----
const R = d.recs;
const neutral = B.recommendationView(R.neutral, dayLabel);
ok(neutral.confidenceKey === 'NEUTRAL' && /not a prediction/.test(neutral.confidenceNote), 'neutral: says it is not a prediction');
ok(neutral.inputs.every(i => !i.available && /not estimated/i.test(i.text)), 'neutral: every missing input is labelled "not estimated"');
ok(neutral.hasRecommendation && neutral.recommendedId === R.neutral.recommended_game_id && neutral.options[0].recommended && neutral.options[0].rank === 1, 'neutral: recommends the earliest game and ranks the options');
ok(neutral.options.length === R.neutral.options.length && neutral.headline.startsWith('Recommended: '), 'neutral: every option is offered as an alternative');
const partial = B.recommendationView(R.partial, dayLabel);
ok(partial.confidenceKey === 'PARTIAL' && /not a win-probability estimate/.test(partial.confidenceNote), 'partial: says it is not a win-probability estimate');
ok(partial.inputs.find(i => i.key === 'opponent_strength').available && !partial.inputs.find(i => i.key === 'win_probability_swing').available, 'partial: only the available input is marked used');
ok(partial.options.some(o => o.strength && /% wins over \d+ games/.test(o.strength)), 'partial: opponent strength is shown with its sample size');
const full = B.recommendationView(R.full, dayLabel);
ok(full.confidenceKey === 'FULL' && full.inputs.every(i => i.available), 'full: all three inputs marked used');
const none = B.recommendationView(R.none, dayLabel);
ok(!none.hasRecommendation && none.recommendedId === null && /No open game/.test(none.headline) && none.options.length === 0, 'none: no recommendation and no options');
ok(partial.reasons.length > 0 && partial.reasons.some(r => /Not available, so not used/.test(r)), 'the API reasons are passed through, including what was not used');

// ---- confirmation text ----
const opt = partial.options[0];
ok(B.confirmText(C.available, opt).includes('+1.35') && /cannot be changed/.test(B.confirmText(C.available, opt)), 'confirmation states the points and that it is final');
ok(/capped from 1.80/.test(B.confirmText(C.capped, opt)) && B.confirmText(C.capped, opt).includes('+1.50'), 'a capped confirmation states the cap');

// ---- error states ----
ok(B.bankError({status: 409, code: 'ALREADY_ASSIGNED', message: 'm'}).refresh === true, 'already assigned -> reload');
ok(B.bankError({status: 409, code: 'GAME_HAS_CREDIT', message: 'm'}).text.includes('Pick another game'), 'game taken -> pick another');
ok(B.bankError({status: 409, code: 'EXPIRED', message: 'm'}).title === 'This credit was already settled', 'expired -> settled message');
ok(B.bankError({status: 403, message: 'no'}).refresh === false && B.bankError({status: 401, message: 'x'}).title === 'Sign in again', '403 and 401 do not reload');
ok(B.bankError(new Error('Failed to fetch')).text.includes('Nothing was changed'), 'a network failure says nothing was changed');
ok(B.bankError({status: 500, message: 'boom'}).text === 'boom', 'unknown errors pass the server message through');
ok(B.sortCredits(d.list).every((c, i, arr) => i === 0 || B.parseUtc(arr[i - 1].created_at) >= B.parseUtc(c.created_at)), 'credits sort newest first');
"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs029-bankui-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'bankui').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import banking, series_schedule
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            BankedCredit, DraftableAsset, League, LeagueSeries, LeagueUniverse, Player, RosterEntry, SeriesDailyGame,
            Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)
        D0 = datetime.date(2026, 10, 7)
        day = lambda n: D0 + datetime.timedelta(days=n - 1)  # noqa: E731

        with SessionLocal() as db:
            commissioner, owner = User(email="c@example.com", display_name="C"), User(email="o@example.com", display_name="O")
            db.add_all([commissioner, owner])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            league = League(name="BankUI", commissioner_user_id=commissioner.id, size=4, series_length_days=7)
            db.add(league)
            db.flush()
            db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
            t1 = Team(league_id=league.id, name="Mine", owner_user_id=owner.id, is_ai_owned=False)
            db.add_all([t1, Team(league_id=league.id, name="B", is_ai_owned=True), Team(league_id=league.id, name="C", is_ai_owned=True),
                        Team(league_id=league.id, name="D", is_ai_owned=True)])
            db.flush()
            names = ["Connor McDavid", "Leon Draisaitl", "Evan Bouchard", "Darnell Nurse", "Zach Hyman", "Ryan Nugent-Hopkins", "Mattias Ekholm"]
            assets = {}
            for n in names:
                p = Player(universe_id=nhl.id, full_name=n, position="C")
                db.add(p)
                db.flush()
                a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
                db.add(a)
                db.flush()
                db.add(RosterEntry(team_id=t1.id, draftable_asset_id=a.id))
                assets[n] = a.id
            series_schedule.generate_schedule(db, league, D0, cycles=2)
            db.commit()
            team_id, league_id = t1.id, league.id
            tokens = {"owner": create_session(db, owner.id).token, "commissioner": create_session(db, commissioner.id).token}
        auth = {k: {"Authorization": f"Bearer {v}"} for k, v in tokens.items()}

        with SessionLocal() as db:
            for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date.in_([day(4), day(14), day(42)])):
                g.is_matchup_day = False
            # Day 1-3 finals so opponents have a record by day 4 (strength becomes available).
            round1 = db.query(LeagueSeries).filter_by(league_id=league_id, start_date=D0).all()
            for s in round1:
                for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.series_id == s.id, SeriesDailyGame.calendar_date <= day(3)):
                    g.status = "FINAL"
                    g.home_score, g.away_score = (3, 1) if s.team_a_id == g.home_team_id else (1, 3)
            db.commit()
            games = {g.calendar_date: g.id for g in db.query(SeriesDailyGame).filter(
                (SeriesDailyGame.home_team_id == team_id) | (SeriesDailyGame.away_team_id == team_id))}

        NOW = datetime.datetime.combine(day(4), datetime.time(21, 0))
        clock = {"now": NOW}
        banking.utcnow = lambda: clock["now"]

        def ingest(name: str, points: float, date=day(4), when=None):
            with SessionLocal() as db:
                c, _ = banking.ingest_credit(db, team_id=team_id, asset_id=assets[name], source_date=date, points=points,
                                             now=when or clock["now"])
                db.commit()
                return c.id

        c_avail = ingest("Connor McDavid", 1.35)
        c_capped = ingest("Leon Draisaitl", 1.80)
        c_assigned = ingest("Evan Bouchard", 0.90)
        c_assigned_capped = ingest("Zach Hyman", 1.70)
        c_auto = ingest("Darnell Nurse", 0.60)
        c_expired = ingest("Ryan Nugent-Hopkins", 0.50, date=day(42))   # nothing left to bank into
        c_none = ingest("Mattias Ekholm", 0.45, date=day(42))

        owner_get = lambda path: client.get(path, headers=auth["owner"])  # noqa: E731
        r = client.post(f"/banked-credits/{c_assigned}/assign", json={"daily_game_id": games[day(6)]}, headers=auth["owner"])
        check(r.status_code == 200, "a credit is assigned manually")
        r2 = client.post(f"/banked-credits/{c_assigned}/assign", json={"daily_game_id": games[day(6)]}, headers=auth["owner"])
        check(r2.status_code == 409 and r2.json()["detail"]["code"] == "ALREADY_ASSIGNED",
              "a repeated submission of the same assignment is rejected by the API (double submit)")
        r = client.post(f"/banked-credits/{c_assigned_capped}/assign", json={"daily_game_id": games[day(9)]}, headers=auth["owner"])
        check(r.status_code == 200 and r.json()["applied_points"] == 1.5, "a capped credit is assigned at the cap")

        # Recommendation payloads before the fallback runs.
        rec_partial = owner_get(f"/banked-credits/{c_avail}/recommendation").json()
        check(rec_partial["confidence"] == "PARTIAL", "the live recommendation is PARTIAL (opponent strength only)")
        rec_none = owner_get(f"/banked-credits/{c_none}/recommendation").json()
        check(rec_none["confidence"] == "NONE" and rec_none["options"] == [] and rec_none["recommended_game_id"] is None,
              "a credit with no open game gets a NONE recommendation")

        # Neutral: an early source date where nobody has three results yet; full: projections from a future source.
        with SessionLocal() as db:
            for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date == day(2)):
                g.is_matchup_day = False
            db.commit()
        c_neutral = ingest("Connor McDavid", 0.80, date=day(2), when=NOW)
        with SessionLocal() as db:
            neutral = banking.recommend(db, db.get(BankedCredit, c_neutral), NOW)
            avail_ids = sorted(g.id for g in banking.eligible_games(db, db.get(BankedCredit, c_avail), NOW))
            proj = {g: {"projected_margin": 0.1 * i, "win_probability_swing": 0.02 * (9 - i)} for i, g in enumerate(avail_ids, start=1)}
            full = banking.recommend(db, db.get(BankedCredit, c_avail), NOW, proj)

        def as_api(rec, credit_id):
            return {"credit": {"id": credit_id}, "recommended_game_id": rec.recommended_game_id, "confidence": rec.confidence,
                    "basis": rec.basis, "reasons": rec.reasons, "inputs_available": rec.inputs_available,
                    "options": [dict(o.__dict__, calendar_date=o.calendar_date.isoformat()) for o in rec.options]}
        check(neutral.confidence == "NEUTRAL" and full.confidence == "FULL", "neutral and full recommendation shapes are produced")

        # Auto-assign one and expire one through the real fallback.
        clock["now"] = NOW + datetime.timedelta(hours=24)
        with SessionLocal() as db:
            for c in db.query(BankedCredit).filter(BankedCredit.id.in_([c_capped, c_avail, c_neutral, c_none])):
                c.created_at = NOW + datetime.timedelta(hours=10)   # keep these waiting (not yet 24h old)
            db.commit()
        r = client.post(f"/leagues/{league_id}/banked-credits/process-fallback", headers=auth["commissioner"]).json()
        check({a["credit_id"] for a in r["assigned"]} == {c_auto} and set(r["expired"]) == {c_expired},
              "the fallback auto-assigns one credit and expires the one with no open game")
        clock["now"] = NOW

        listing = owner_get(f"/teams/{team_id}/banked-credits").json()
        by_id = {c["id"]: c for c in listing["credits"]}
        check(by_id[c_auto]["assignment_mode"] == "AUTO" and by_id[c_expired]["status"] == "EXPIRED"
              and by_id[c_capped]["capped"] and by_id[c_avail]["status"] == "AVAILABLE", "all five states are present in the API listing")
        schedule = owner_get(f"/teams/{team_id}/series-schedule").json()

        # ---- the served page ----------------------------------------------------------------------------------
        html = client.get("/static/nhl-schedule.html").text
        for marker, why in [
            ('id="bank-alert"', "banking alert region"), ('id="bank-section"', "banked credits section"),
            ('aria-live="polite"', "polite live regions"), ('role="alert"', "errors announced"),
            ('Accept recommendation', "accept action"), ('Choose another game', "choose-another action"),
            ('name="bank-game"', "alternative-game picker (radio group)"), ('<legend>Choose a game</legend>', "picker legend"),
            ('id="reco-confirm"', "confirmation step"), ('aria-expanded', "disclosure state"), ('aria-controls="reco"', "disclosure target"),
            ('disabled aria-disabled="true"', "disabled controls announced"), ('class="bk', "series-tab badge"),
            ('Why this game', "recommendation reasons"), ('B.recommendationView', "recommendation view model"),
            ('if (ui.submitting) return', "double-submit guard"), ('Banking…', "in-flight state"),
            ('Try again', "banking load error retry"), ('only shown to the team', "non-owner state"),
            ('No banked credits', "empty state"), (':focus-visible', "visible keyboard focus"),
            ('.focus()', "focus management"), ('@media (max-width:860px)', "mobile layout kept"),
        ]:
            check(marker in html, f"page has {why}")
        for endpoint in ("/banked-credits`", "/recommendation", "/assign"):
            check(endpoint in html, f"page calls {endpoint}")
        logic_src = (ROOT / "web" / "static" / "nhl-banking-logic.js").read_text(encoding="utf-8")
        check(client.get("/static/nhl-banking-logic.js").status_code == 200 and "nhl-banking-logic.js" in html, "logic module is served and loaded")
        check(not re.search(r"Math\.random|setInterval|WebSocket|EventSource|Notification|serviceWorker|mailto:|sendBeacon", html + logic_src),
              "no polling timers, push, email or notification code")
        check(not re.search(r"win_probability_swing\s*[*+/-]|projected_margin\s*[*+/-]|\*\s*0\.\d|Math\.(exp|log|pow|sqrt)", logic_src + html),
              "no recommendation or probability maths in JavaScript")
        check(not re.search(r'(src|href)=["\']https?://', html), "no external assets")
        check(html.count("renderSeries()") >= 5 and "bankBadge(t)" in html, "series tabs render banking badges")

        # ---- logic over the real payloads under Node -------------------------------------------------------------
        node = shutil.which("node")
        check(node is not None, "node is available to run the banking logic checks")
        fixtures = Path(temp_dir) / "payloads.json"
        script = Path(temp_dir) / "check.js"
        fixtures.write_text(json.dumps({
            "now": NOW.isoformat(), "schedule": schedule, "list": listing["credits"],
            "credits": {"available": by_id[c_avail], "capped": by_id[c_capped], "assigned": by_id[c_assigned],
                        "assigned_capped": by_id[c_assigned_capped], "auto": by_id[c_auto], "expired": by_id[c_expired]},
            "recs": {"neutral": as_api(neutral, c_neutral), "partial": rec_partial, "full": as_api(full, c_avail), "none": rec_none},
        }), encoding="utf-8")
        script.write_text(NODE_CHECK, encoding="utf-8")
        proc = subprocess.run([node, str(script), str(ROOT / "web" / "static" / "nhl-banking-logic.js"), str(fixtures)],
                              capture_output=True, text=True)
        print(proc.stdout, end="")
        check(proc.returncode == 0, "banking logic checks pass under Node" + (f": {proc.stderr.strip()}" if proc.returncode else ""))
        inline = re.findall(r"<script>(.*?)</script>", html, re.S)
        (Path(temp_dir) / "inline.js").write_text(inline[-1], encoding="utf-8")
        proc = subprocess.run([node, "--check", str(Path(temp_dir) / "inline.js")], capture_output=True, text=True)
        check(proc.returncode == 0, "page script is syntactically valid" + (f": {proc.stderr.strip()}" if proc.returncode else ""))
        engine.dispose()

    print("\nAll FS-029 banking UI checks passed.")


if __name__ == "__main__":
    main()
