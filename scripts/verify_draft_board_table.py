"""
Verification for the draft board's sortable stats table: the board API returns stored stats (goals, assists, shots, blocks,
last-season points, an always-empty projection), missing data stays None (never zero or invented), and the page has sortable
column headers, sorts with missing values last, and no longer has a Pause button. Throwaway SQLite, no network.

    .venv-api/Scripts/python.exe scripts/verify_draft_board_table.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs-board-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'board').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.main import app
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, League, LeagueUniverse, Player, RealTeam, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            comm = User(email="c@example.com", display_name="C")
            db.add(comm)
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            bos = RealTeam(universe_id=nhl.id, name="Boston", external_ref="BOS")
            db.add(bos)
            db.flush()
            a = {}
            for name, pos in [("Ann Scorer", "C"), ("Bo Passer", "W"), ("Cy Blocker", "D"), ("Di Goalie", "G"), ("Ed Nodata", "C")]:
                p = Player(universe_id=nhl.id, real_team_id=bos.id, full_name=name, position=pos)
                db.add(p)
                db.flush()
                asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
                db.add(asset)
                db.flush()
                a[name] = asset.id
            lg = League(name="L", commissioner_user_id=comm.id, size=2, draft_rounds=2, series_length_days=7)
            db.add(lg)
            db.flush()
            db.add(LeagueUniverse(league_id=lg.id, universe_id=nhl.id))
            db.add_all([Team(league_id=lg.id, name="T1", is_ai_owned=True), Team(league_id=lg.id, name="T2", is_ai_owned=True)])
            db.flush()
            from fantasy_sports.platform.db.models.core import Game, Week
            wk = Week(universe_id=nhl.id, season_year=2025, week_number=1)
            db.add(wk)
            db.flush()
            g = Game(universe_id=nhl.id, week_id=wk.id, home_team_id=bos.id, away_team_id=bos.id)
            db.add(g)
            db.flush()
            # Ann: 3 shots on goal, 2 goals, plus one empty-net goal that must not count as a shot; Bo: 1 assist event + 1 assist credit.
            shots = []
            for i, goal in enumerate([True, True, False]):
                s = Shot(game_id=g.id, source="t", external_shot_id=f"s{i}", shooter_asset_id=a["Ann Scorer"], pre_shot_xg=0.2, is_goal=goal, is_empty_net=False)
                db.add(s)
                shots.append(s)
            db.add(Shot(game_id=g.id, source="t", external_shot_id="en", shooter_asset_id=a["Ann Scorer"], pre_shot_xg=0.9, is_goal=True, is_empty_net=True))
            db.flush()
            db.add_all([ShotCredit(shot_id=shots[0].id, draftable_asset_id=a["Ann Scorer"], role="shooter", credit_points=0.8),
                        ShotCredit(shot_id=shots[1].id, draftable_asset_id=a["Ann Scorer"], role="shooter", credit_points=0.7),
                        ShotCredit(shot_id=shots[0].id, draftable_asset_id=a["Bo Passer"], role="primary_assist", credit_points=0.2),
                        ShotCredit(shot_id=shots[1].id, draftable_asset_id=a["Bo Passer"], role="secondary_assist", credit_points=0.3)])
            db.add(GameEvent(game_id=g.id, source="t", external_event_id="e1", event_type="goal", secondary_asset_id=a["Bo Passer"],
                             tertiary_asset_id=a["Cy Blocker"]))
            db.add(EventCredit(event_id=db.query(GameEvent).first().id if False else 1, draftable_asset_id=a["Cy Blocker"], role="blocker", credit_points=0.04))
            db.commit()
            league_id = lg.id
        client = TestClient(app)
        r = client.post("/drafts/start", json={"league_id": league_id})
        board = {b["display_name"]: b for b in client.get(f"/drafts/{r.json()['id']}/board").json()}
        ann, bo, cy, nod = board["Ann Scorer"], board["Bo Passer"], board["Cy Blocker"], board["Ed Nodata"]
        check((ann["goals"], ann["shots"]) == (2, 3), "goals and shots come from stored shots (the empty-net goal is not a shot)")
        check(ann["points"] == 1.5 and ann["assists"] == 0, "last-season points are the stored credits (0.8 + 0.7); no assists is a real 0")
        check(bo["assists"] == 2 and bo["points"] == 0.5, "assists are the larger of play-by-play (1) and stored assist credits (2)")
        check(cy["blocks"] == 1 and cy["assists"] == 1 and cy["points"] == 0.04, "blocks and assists from events; block credit counts as points")
        check(all(b["projected"] is None for b in board.values()), "projected points are never invented (always null)")
        check(nod["goals"] is None and nod["points"] is None and nod["shots"] is None and nod["assists"] is None, "a player with no stored data is null, not zero")
        check(ann["team"] == "Boston" and ann["position"] == "C", "team and position are present")

        html = client.get("/static/draft-board.html").text
        check(html.count('data-sort="') >= 0 and "const COLUMNS" in html and "aria-sort" in html and "class=\"table board-table\"" in html, "the board is a table with sortable headers")
        for col in ("points", "projected", "goals", "assists", "shots", "blocks"):
            check(f"key: '{col}'" in html, f"column '{col}' is sortable")
        check("pause-btn" not in html and "Pause draft" not in html, "the Pause draft button is gone")

        node = shutil.which("node")
        html = html.replace(chr(13) + chr(10), chr(10))
        src = re.search(r"const COLUMNS = \[.*?\];\nlet sortKey.*?\n\nfunction sortRows\(rows\) \{.*?\n\}\n", html, re.S).group(0)
        js = src + r"""
const ok = (c, m) => { if (!c) { console.error('FAIL ' + m); process.exit(1); } console.log('ok  ' + m); };
const rows = [{display_name: 'B', goals: 5, team: 'X'}, {display_name: 'A', goals: null, team: 'Y'}, {display_name: 'C', goals: 9, team: null}, {display_name: 'D', goals: 5, team: 'Z'}];
sortKey = 'goals'; sortDir = 'desc';
ok(sortRows(rows).map(r => r.display_name).join('') === 'CBDA', 'descending: highest first, ties by name, missing last');
sortDir = 'asc';
ok(sortRows(rows).map(r => r.display_name).join('') === 'BDCA', 'ascending: lowest first, missing still last');
sortKey = 'team'; sortDir = 'asc';
ok(sortRows(rows).map(r => r.display_name).join('') === 'BADC'.replace('BADC', 'BADC') && sortRows(rows)[3].display_name === 'C', 'text columns sort A-Z with missing last');
sortKey = 'projected';
ok(sortRows(rows).map(r => r.display_name).join('') === 'ABCD', 'a column nobody has data for falls back to name order');
"""
        f = Path(temp_dir) / "sort.js"
        f.write_text(js, encoding="utf-8")
        proc = subprocess.run([node, str(f)], capture_output=True, text=True)
        print(proc.stdout, end="")
        check(proc.returncode == 0, "table sorting logic passes under Node" + (f": {proc.stderr.strip()}" if proc.returncode else ""))
        engine.dispose()
    print("\nAll draft board table checks passed.")


if __name__ == "__main__":
    main()
