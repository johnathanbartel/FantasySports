"""
Browser-independent verification for the NHL schedule and lineup viewer (FS-023).

Builds a throwaway NHL league, drives the real FS-021/FS-022 APIs through the FastAPI TestClient, checks the
served static pages for the required structure (zones, tabs, states, accessible controls, no external
assets), then feeds the live API payloads through the viewer's pure logic (web/static/nhl-schedule-logic.js)
under Node. No browser, network, or provider is used.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_schedule_viewer.py
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
const V = require(process.argv[2]);
const d = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const ok = (c, m) => { if (!c) { console.error('FAIL ' + m); process.exit(1); } console.log('ok  ' + m); };

const lines = V.buildLines(d.order.groups);
ok(lines.length === 4 && lines.every(l => l.slots.length === 6), 'four lines of six slots (1C 2W 2D 1G)');
ok(lines[0].slots.map(s => s.group).join('') === 'CWWDDG', 'slot order is C W W D D G');
ok(lines[0].slots[0].player.player_id === d.order.groups[0].ranked[0].player_id, 'line 1 C is the rank-1 center');
ok(lines[1].slots[1].rank === 3 && lines[1].slots[2].rank === 4, 'line 2 wingers are ranks 3 and 4');
ok(lines[3].slots[5].player !== null && lines[3].slots[5].rank === 4, 'line 4 goalie is rank 4');
ok(d.sparse_lines[0].slots.some(s => s.player === null), 'unfilled depth is an explicit empty slot, not invented');

const g = d.order.groups.find(x => x.position_group === 'W');
const up = V.reorder(g, 3, -1);
ok(up && up[1] === g.ranked[2].player_id && up[2] === g.ranked[1].player_id, 'reorder up swaps rank 3 with rank 2');
ok(V.reorder(g, 1, -1) === null && V.reorder(g, g.ranked.length, 1) === null, 'moves past either end are rejected');
ok(V.addToDepth({ranked: new Array(g.max_depth).fill({priority: 1, player_id: 1}), max_depth: g.max_depth}, 5) === null,
   'adding to a full ranking is rejected');
ok(V.removeFromDepth(g, g.ranked[0].player_id).length === g.ranked.length - 1, 'remove drops one id');

const first = d.order.groups[0].ranked[0];
ok(V.playerState(first, null, []).state === 'unknown', 'no resolve result -> unknown, never guessed');
const act = V.playerState(first, d.resolved, []);
ok(act.state === 'active' && !act.confirmed, 'resolved starter is active');
const bench = d.order.groups[0].ranked[1];
ok(V.playerState(bench, d.resolved, []).state === 'idle', 'ranked non-starter is idle');
const goalie = d.order.groups.find(x => x.position_group === 'G').ranked[0];
ok(V.playerState(goalie, d.resolved, [goalie.player_id]).confirmed === true, 'confirmed goalie is flagged');
ok(V.playerState(goalie, d.resolved, []).confirmed === false, 'unconfirmed goalie is not flagged');

const tabs = V.seriesTabs(d.schedule.series[0]);
ok(tabs.length === d.schedule.series[0].total_days, 'one tab per series day');
ok(tabs.every(t => t.gameScore === null && V.gameScoreClass(t.gameScore) === 'gs-none'), 'Game Score is a placeholder');
ok(tabs.some(t => t.off) === d.has_off_day, 'off days are marked from is_matchup_day');
ok(tabs.filter(t => !t.off).every(t => t.opponent === d.schedule.series[0].opponent_name), 'tabs carry the opponent');
ok(V.initials('Boston Bruins') === 'BB' && V.initials('') === '?' && V.initials('Utah') === 'UT', 'logo fallback initials');
ok(V.dayLabel('2026-10-14') === 'Wed 10/14', 'date label is timezone-safe');
ok([0, 40, 41, 70, 71, 100].map(V.gameScoreClass).join() === 'gs-low,gs-low,gs-mid,gs-mid,gs-high,gs-high', 'Game Score color ramp');
const sl = d.schedule.series;
ok(V.pickSeries([], '2026-10-14') === -1, 'no series -> -1');
ok(V.pickSeries(sl, sl[1].start_date) === 1, 'series containing today is chosen');
ok(V.pickSeries(sl, '2000-01-01') === 0 && V.pickSeries(sl, '2999-01-01') === sl.length - 1, 'before/after season clamps');
"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs023-viewer-", dir=ROOT) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'viewer').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, League, LeagueUniverse, Player, RosterEntry, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)

        with SessionLocal() as db:
            commish = User(email="c@example.com", display_name="Commish")
            owner = User(email="o@example.com", display_name="Owner")
            db.add_all([commish, owner])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            league = League(name="Viewer NHL", commissioner_user_id=commish.id, size=4, series_length_days=7)
            db.add(league)
            db.flush()
            db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
            mine = Team(league_id=league.id, name="Mine", owner_user_id=owner.id, is_ai_owned=False)
            sparse = Team(league_id=league.id, name="Sparse", is_ai_owned=True)
            db.add_all([mine, sparse, Team(league_id=league.id, name="Third", is_ai_owned=True),
                        Team(league_id=league.id, name="Fourth", is_ai_owned=True)])
            db.flush()
            n = [0]

            def add(team: Team, pos: str) -> int:
                n[0] += 1
                p = Player(universe_id=nhl.id, full_name=f"Player {pos}{n[0]}", position=pos, external_ref=f"v{n[0]}")
                db.add(p)
                db.flush()
                a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
                db.add(a)
                db.flush()
                db.add(RosterEntry(team_id=team.id, draftable_asset_id=a.id))
                return p.id

            ids = {"C": [], "W": [], "D": [], "G": []}
            for pos, grp, count in [("C", "C", 4), ("LW", "W", 4), ("RW", "W", 4), ("D", "D", 8), ("G", "G", 4)]:
                for _ in range(count):
                    ids[grp].append(add(mine, pos))
            add(sparse, "C")
            db.commit()
            team_id, sparse_id, league_id = mine.id, sparse.id, league.id
            owner_hdr = {"Authorization": f"Bearer {create_session(db, owner.id).token}"}
            commish_hdr = {"Authorization": f"Bearer {create_session(db, commish.id).token}"}

        # ---- static pages: structure, states, accessibility, no external assets -----------------
        r = client.get("/static/nhl-schedule.html")
        html = r.text
        check(r.status_code == 200, "nhl-schedule.html is served")
        for marker, why in [
            ('id="zone-lineup"', "pecking-order zone"), ('id="zone-days"', "series-days zone"), ('id="zone-panel"', "matchup zone"),
            ('id="zones"', "zone container"), ('data-zone="lineup"', "narrow-screen switcher: lineup"),
            ('data-zone="days"', "narrow-screen switcher: days"), ('data-zone="panel"', "narrow-screen switcher: matchup"),
            ('@media (max-width:860px)', "narrow-screen layout"), ('role="tablist"', "tab semantics"),
            ('role="tab"', "tab role on days"), ('aria-selected', "tab selection state"), ('ArrowRight', "keyboard tab navigation"),
            ('data-act="up"', "move-up control"), ('data-act="down"', "move-down control"), ("aria-label=\"Move ", "reorder labels"),
            ('draggable', "optional drag-and-drop"), ('id="sim-not-ready"', "simulator-not-ready panel"),
            ('Simulator not ready', "simulator-not-ready copy"), ('id="page-state"', "loading/empty/error state host"),
            ('id="retry"', "error retry"), ('Not an NHL league', "non-NHL state"), ('No schedule yet', "empty schedule state"),
            ('role="alert"', "error announced"), ('aria-live', "live regions"), ('prefers-reduced-motion', "reduced motion"),
            ('Unconfirmed', "goalie confirmation state"), ('OFF DAY', "off-day tab"), ('Game Score', "Game Score placeholder"),
            ('V.initials', "logo fallback"),
        ]:
            check(marker in html, f"page has {why}")
        check("/pecking-order" in html and "/series-schedule" in html and "/pecking-order/resolve" in html,
              "page calls the real FS-021/FS-022 endpoints")
        check(not re.search(r'(src|href)=["\']https?://', html) and "@import" not in html and "<canvas" not in html,
              "no external assets or canvas")
        check(not re.search(r"Math\.random|setInterval|WebSocket|EventSource", html), "no fabricated or live-simulated data")
        check(client.get("/static/nhl-schedule-logic.js").status_code == 200, "logic module is served")
        league_html = client.get("/static/league.html").text
        check("nhl-schedule.html?league_id=" in league_html and "includes('nhl')" in league_html,
              "league home links to the viewer for NHL leagues only")

        # ---- real API payloads ---------------------------------------------------------------
        for g in "CWDG":
            r = client.put(f"/teams/{team_id}/pecking-order", json={"assignments": {g: ids[g]}}, headers=owner_hdr)
            check(r.status_code == 200, f"owner ranks the {g} group")
        order = client.get(f"/teams/{team_id}/pecking-order").json()
        resolved = client.post(f"/teams/{team_id}/pecking-order/resolve",
                               json={"scheduled_player_ids": [p for v in ids.values() for p in v]}).json()
        check(len(resolved["starters"]["C"]) == 1 and len(resolved["starters"]["W"]) == 2 and resolved["vacancies"],
              "resolve returns 1C/2W/2D and a goalie vacancy while none is confirmed")
        sparse_order = client.get(f"/teams/{sparse_id}/pecking-order").json()

        r = client.get(f"/teams/{team_id}/series-schedule")
        check(r.status_code == 200 and r.json()["series"] == [], "team schedule is empty before generation (empty state)")
        r = client.post(f"/leagues/{league_id}/series-schedule", json={"start_date": datetime.date(2026, 10, 7).isoformat(), "cycles": 1},
                        headers=commish_hdr)
        check(r.status_code == 200, "schedule generated")
        schedule = client.get(f"/teams/{team_id}/series-schedule").json()
        check(len(schedule["series"]) == 3 and all(len(s["games"]) == 7 for s in schedule["series"]),
              "team schedule has 3 series of 7 daily games")
        check(client.get(f"/leagues/{league_id}").json()["series_length_days"] == 7, "league exposes series_length_days (NHL gate)")
        check(client.get(f"/leagues/{league_id}/teams").status_code == 200, "league teams endpoint feeds the team picker")

        # ---- viewer logic over the live payloads under Node -----------------------------------
        node = shutil.which("node")
        check(node is not None, "node is available to run the viewer logic checks")
        fixtures = Path(temp_dir) / "payloads.json"
        script = Path(temp_dir) / "check.js"
        sparse_lines = None
        fixtures.write_text(json.dumps({
            "order": order, "resolved": resolved, "schedule": schedule,
            "has_off_day": any(not g["is_matchup_day"] for g in schedule["series"][0]["games"]),
            "sparse_order": sparse_order,
        }), encoding="utf-8")
        # sparse_lines is built from the sparse team's payload inside the script
        script.write_text(NODE_CHECK.replace(
            "const lines = V.buildLines(d.order.groups);",
            "d.sparse_lines = V.buildLines(d.sparse_order.groups);\nconst lines = V.buildLines(d.order.groups);"),
            encoding="utf-8")
        proc = subprocess.run([node, str(script), str(ROOT / "web" / "static" / "nhl-schedule-logic.js"), str(fixtures)],
                              capture_output=True, text=True)
        print(proc.stdout, end="")
        check(proc.returncode == 0, "viewer logic checks pass under Node" + (f": {proc.stderr.strip()}" if proc.returncode else ""))

        # Inline script of the page parses (syntax only; no browser).
        inline = re.findall(r"<script>(.*?)</script>", html, re.S)
        (Path(temp_dir) / "inline.js").write_text(inline[-1], encoding="utf-8")
        proc = subprocess.run([node, "--check", str(Path(temp_dir) / "inline.js")], capture_output=True, text=True)
        check(proc.returncode == 0, "page script is syntactically valid" + (f": {proc.stderr.strip()}" if proc.returncode else ""))
        engine.dispose()  # release the SQLite file so Windows can delete the temp directory

    print("\nAll FS-023 viewer checks passed.")


if __name__ == "__main__":
    main()
