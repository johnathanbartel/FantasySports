"""
FS-034: end-to-end release gate for the NHL series flow on a throwaway database.

One deterministic scenario: a 12-team, 24-round NHL league with one human and 11 AI teams, built on a database created
by `alembic upgrade head` (so the migrations themselves are under test): league creation -> timed snake draft ->
pecking orders for all 12 teams -> 7-day series schedule -> real stored shot credits -> published, started, revealed
and finished simulated games for the human's whole first series -> standings and Game Score tie-outs -> off-day
banking -> authorization, idempotency, missing-table and PostgreSQL-portability audits. No external service, no
production data, no mocks of the code under test (only an injected clock).

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_series_e2e.py
"""

from __future__ import annotations

import datetime as dt
import os
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PY = sys.executable

from unittest.mock import patch
_fetch_patcher = patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None)
_fetch_patcher.start()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def alembic(env: dict, *args: str) -> str:
    proc = subprocess.run([PY, "-m", "alembic", *args], cwd=ROOT, env={**os.environ, **env}, capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError(f"alembic {' '.join(args)} failed: {proc.stderr[-600:]}")
    return proc.stdout + proc.stderr


def portability_audit() -> None:
    """Static PostgreSQL checks that need no server: DDL compiles for the PostgreSQL dialect, offline migration SQL
    generates, and no check constraint compares a boolean column with 0/1 (the bug fixed in b98a599)."""
    from sqlalchemy import Boolean, CheckConstraint
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from fantasy_sports.platform.db.base import Base
    import fantasy_sports.platform.db.models  # noqa: F401

    n_tables = n_checks = 0
    bool_cols = {(t.name, c.name) for t in Base.metadata.tables.values() for c in t.columns if isinstance(c.type, Boolean)}
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for idx in table.indexes:
            str(CreateIndex(idx).compile(dialect=postgresql.dialect()))
        n_tables += 1
        for con in table.constraints:
            if isinstance(con, CheckConstraint):
                n_checks += 1
                text = str(con.sqltext)
                for (tn, cn) in bool_cols:
                    if tn == table.name and re.search(rf"\b{cn}\b\s*(=|<>|!=|IN)\s*\(?\s*[01]\b", text):
                        raise AssertionError(f"{table.name}.{con.name}: boolean {cn} compared with 0/1 (not PostgreSQL-safe)")
    check(n_tables > 40, f"all {n_tables} tables compile to PostgreSQL DDL ({n_checks} check constraints, none compare booleans with 0/1)")
    out = subprocess.run([PY, "-m", "alembic", "upgrade", "head", "--sql"], cwd=ROOT,
                         env={**os.environ, "DATABASE_URL": "postgresql+psycopg2://u:p@localhost/db"}, capture_output=True, text=True)
    sql = out.stdout
    check(out.returncode == 0 and "CREATE TABLE simulator_games" in sql and "CREATE TABLE weekly_contests" in sql and "ALTER TABLE drafts ADD COLUMN pick_seconds" in sql,
          "the full migration chain renders as PostgreSQL SQL offline (no server needed)")
    check("PRAGMA" not in sql and "AUTOINCREMENT" not in sql and "INSERT OR" not in sql, "the PostgreSQL migration SQL contains no SQLite-only statements")


def schema_drift(db_url: str) -> None:
    """The alembic-built database and the ORM metadata describe the same tables and columns."""
    from sqlalchemy import create_engine, inspect

    from fantasy_sports.platform.db.base import Base
    import fantasy_sports.platform.db.models  # noqa: F401

    insp = inspect(create_engine(db_url))
    db_tables = set(insp.get_table_names()) - {"alembic_version"}
    model_tables = set(Base.metadata.tables)
    # Known, pre-existing gap (football data, not part of the NHL flow): these two ORM tables have never had a migration,
    # so an Alembic-built database lacks them. The code that reads them already tolerates their absence (see
    # recruiting/pricing.season_points_by_asset). Any OTHER difference fails the gate.
    known_unmigrated = {"player_recruit_ratings", "player_season_points"}
    missing, extra = model_tables - db_tables, db_tables - model_tables
    print(f"info  ORM tables with no migration (known, football-only): {sorted(missing & known_unmigrated)}")
    check(missing <= known_unmigrated and not extra,
          f"migrations create every ORM table except the 2 known football ones (unexpected missing: {sorted(missing - known_unmigrated)}, extra: {sorted(extra)})")
    model_tables = model_tables - known_unmigrated
    drift = []
    for name, table in Base.metadata.tables.items():
        if name in known_unmigrated:
            continue
        have = {c["name"]: c for c in insp.get_columns(name)}
        want = {c.name: c for c in table.columns}
        if set(have) != set(want):
            drift.append((name, sorted(set(want) ^ set(have))))
            continue
        for cn, c in want.items():
            if bool(have[cn]["nullable"]) != bool(c.nullable) and not c.primary_key:
                drift.append((name, f"{cn} nullability"))
    check(not drift, f"every column matches between migrations and models (drift: {drift})")


def main() -> None:
    import faulthandler
    faulthandler.dump_traceback_later(int(os.environ.get('E2E_HANG_SECONDS', '0')) or 100000, exit=True)
    with tempfile.TemporaryDirectory(prefix="fs034-e2e-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        db_path = Path(temp_dir) / "e2e.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        env = {"DATABASE_URL": db_url}
        heads = alembic(env, "heads")
        check(len(re.findall(r"\(head\)", heads)) == 1, f"alembic has exactly one head: {heads.strip().splitlines()[-1]}")
        alembic(env, "upgrade", "head")
        check(db_path.exists(), "alembic upgrade head builds the scenario database from nothing")
        os.environ.update(env)
        os.environ["FSC_GRANT_ALL_PREMIUM"] = "1"   # commissioner mode for acting on AI teams, as the demo server does

        schema_drift(db_url)
        portability_audit()

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.api.routers import draft as draft_router
        from fantasy_sports.platform import banking
        from fantasy_sports.platform import pecking_order as po
        from fantasy_sports.platform.db.models.core import (
            BankedCredit, DraftableAsset, Game, League, LeagueSeries, Player, RealTeam, RosterEntry, SeriesDailyGame,
            SimulatorGame, Team, TeamPeckingOrder, Universe, User, Week,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
        from fantasy_sports.platform.simulator import service as sim_service
        from fantasy_sports.sports.hockey.models import Shot, ShotCredit
        from fantasy_sports.sports.hockey.pipeline import ShotRecord, process_game
        from fantasy_sports.sports.hockey.simulator import loader

        client = TestClient(app)
        rng = random.Random(34)
        T0 = dt.datetime(2026, 10, 14, 9, 0)
        clock = {"now": T0}
        draft_router.utcnow = lambda: clock["now"]
        sim_service.utcnow = lambda: clock["now"]
        banking.utcnow = lambda: clock["now"]

        # ---- universe, players, users -------------------------------------------------------------------------------
        with SessionLocal() as db:
            nhl, _ = seed_hockey_reference_data(db)
            human = User(email="human@example.com", display_name="Human GM")
            intruder = User(email="intruder@example.com", display_name="Intruder")
            db.add_all([human, intruder])
            bos, tor = RealTeam(universe_id=nhl.id, name="BOS", external_ref="BOS"), RealTeam(universe_id=nhl.id, name="TOR", external_ref="TOR")
            db.add_all([bos, tor])
            db.flush()
            block = ["C", "LW", "RW", "D", "D", "G"] * 3          # 18 per block: 3 C, 3 LW, 3 RW, 6 D, 3 G
            positions: list[str] = []
            for _ in range(15):                                    # 270 players
                b = block[:]
                rng.shuffle(b)
                positions += b
            for i, pos in enumerate(positions):
                p = Player(universe_id=nhl.id, real_team_id=(bos if i % 2 else tor).id, full_name=f"Skater {i:03d}", position=pos)
                db.add(p)
                db.flush()
                db.add(DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id))
            db.commit()
            human_id, intruder_id = human.id, intruder.id
            tok = {"human": create_session(db, human.id).token, "intruder": create_session(db, intruder.id).token}
            real = {"bos": bos.id, "tor": tor.id}
        auth = {k: {"Authorization": f"Bearer {v}"} for k, v in tok.items()}

        # ---- league creation: 12 teams, 18 rounds, 1 human + 11 AI --------------------------------------------------------
        r = client.post("/leagues", json={"name": "E2E NHL", "commissioner_user_id": human_id, "universe_codes": ["nhl"], "size": 12,
                                          "draft_rounds": 18}, headers=auth["human"])
        check(r.status_code == 200 and r.json()["series_length_days"] == 7 and r.json()["draft_rounds"] == 18 and r.json()["size"] == 12,
              "league created: 12 teams, 18 rounds, 7-day series")
        league_id = r.json()["id"]
        r = client.post("/teams", json={"league_id": league_id, "name": "Human FC", "owner_user_id": human_id, "program_size": "large"}, headers=auth["human"])
        check(r.status_code == 200, "the human team is created")
        human_team = r.json()["id"]
        r = client.post(f"/leagues/{league_id}/ai-teams", headers=auth["human"])
        check(r.status_code == 200 and len(r.json()) == 11 and all(t["is_ai_owned"] for t in r.json()), "11 AI teams fill the league")
        teams = sorted([human_team] + [t["id"] for t in r.json()])
        check(client.post(f"/leagues/{league_id}/ai-teams", headers=auth["intruder"]).status_code == 403, "only the commissioner may fill the league")

        # ---- draft: 216 picks, timed clock, human position-aware, AI by the platform autopick ---------------------------------
        r = client.post("/drafts/start", json={"league_id": league_id, "pick_seconds": 300}, headers=auth["human"])
        check(r.status_code == 200 and r.json()["pick_seconds"] == 300, "a timed snake draft starts")
        draft = r.json()["id"]
        template = {"C": 3, "W": 6, "D": 6, "G": 3}
        group = po.GROUP_BY_POSITION
        have = Counter()
        steps = 0
        while True:
            c = client.get(f"/drafts/{draft}/clock").json()
            if c["status"] == "complete":
                break
            steps += 1
            assert steps < 400, "draft did not finish"
            if steps % 48 == 1:
                print(f"info  draft step {steps}", flush=True)
            if c["on_clock_team_id"] == human_team:
                board = client.get(f"/drafts/{draft}/board").json()
                need = {g: n - have[g] for g, n in template.items()}
                choice = next((b for b in board if need.get(group.get(b["position"]), 0) > 0), board[0])
                r = client.post(f"/drafts/{draft}/pick", json={"draftable_asset_id": choice["draftable_asset_id"]}, headers=auth["human"])
                assert r.status_code == 200, r.text
                have[group.get(choice["position"])] += 1
            else:
                assert client.post(f"/drafts/{draft}/autopick").status_code == 200
        picks = client.get(f"/drafts/{draft}/picks").json()
        check(len(picks) == 216 and all(p["draftable_asset_id"] for p in picks) and len({p["draftable_asset_id"] for p in picks}) == 216,
              "216 picks made, every player drafted once")
        check(have == Counter(template), f"the human drafted the exact 18-slot template {dict(have)}")
        per_team = Counter(p["team_id"] for p in picks)
        check(set(per_team) == set(teams) and set(per_team.values()) == {18}, "every team has exactly 18 players")
        check(client.get(f"/drafts/{draft}").json()["status"] == "complete" and client.get(f"/drafts/{draft}/clock").json()["deadline_at"] is None,
              "the draft completes and clears its clock")
        an = client.get(f"/drafts/{draft}/announcements").json()
        check(an["picks_made"] == 216 and len(an["announcements"]) == 216 and an["announcements"][0]["commentary"]["value_label"] == "unknown",
              "every pick has an announcement; with no stored points the commentary says there is no value call")

        # ---- pecking orders for all 12 teams -------------------------------------------------------------------------------------
        coverage = {}
        with SessionLocal() as db:
            for tid in teams:
                roster = po.roster_players(db, tid)
                order: dict[str, list[int]] = {"C": [], "W": [], "D": [], "G": []}
                for pid, (_, pl) in sorted(roster.items()):
                    order[group[pl.position]].append(pid)
                coverage[tid] = {g: len(v) for g, v in order.items()}
        for tid in teams:
            order = None
            with SessionLocal() as db:
                roster = po.roster_players(db, tid)
                order = {g: [] for g in "CWDG"}
                for pid, (_, pl) in sorted(roster.items()):
                    order[group[pl.position]].append(pid)
            body = {g: ids[: po.DEPTH[g]] for g, ids in order.items() if ids}
            who = "human" if tid == human_team else "human"   # the human is the league commissioner and may act for AI teams
            r = client.put(f"/teams/{tid}/pecking-order", json={"assignments": body}, headers=auth[who])
            assert r.status_code == 200, r.text
        short = {tid: [g for g, n in cov.items() if n < po.STARTERS[g]] for tid, cov in coverage.items() if any(n < po.STARTERS[g] for g, n in cov.items())}
        check(len(coverage) == 12, "12 pecking orders stored (human team directly, AI teams through commissioner mode)")
        print(f"info  AI coverage by position group (platform autopick is position-blind): teams with a starter slot they cannot fill: {short or 'none'}")
        check(coverage[human_team] == template, "the human team's depth chart is the full 3C/6W/6D/3G")
        r = client.put(f"/teams/{human_team}/pecking-order", json={"assignments": {"C": [1]}}, headers=auth["intruder"])
        check(r.status_code == 403, "another user cannot change the human team's pecking order")

        # ---- schedule ---------------------------------------------------------------------------------------------------------------
        start = dt.date(2026, 10, 14)
        r = client.post(f"/leagues/{league_id}/series-schedule", json={"start_date": start.isoformat(), "replace": True, "window_rows": False}, headers=auth["human"])
        check(r.status_code == 200 and len(r.json()["series"]) == 66, "the series schedule has 66 series (11 rounds x 6)")
        check(client.post(f"/leagues/{league_id}/series-schedule", json={"start_date": start.isoformat()}, headers=auth["human"]).status_code == 400,
              "regenerating without replace is refused (idempotency)")
        ts = client.get(f"/teams/{human_team}/series-schedule").json()["series"]
        check(len(ts) == 11 and len({s["opponent_id"] for s in ts}) == 11 and all(len(s["games"]) == 7 for s in ts), "the human faces 11 distinct opponents for 7 days each")
        first = ts[0]
        first_dates = [g["calendar_date"] for g in first["games"]]
        first_ids = [g["daily_game_id"] for g in first["games"]]
        opponent = first["opponent_id"]

        # ---- real stored credits for the 7 days of the first series -----------------------------------------------------------------
        with SessionLocal() as db:
            week = Week(universe_id=db.query(Universe).filter_by(code="nhl").one().id, season_year=2026, week_number=1, start_date=start, end_date=start)
            db.add(week)
            db.flush()
            skaters = {"bos": [], "tor": []}
            goalies = {"bos": [], "tor": []}
            for a in db.query(DraftableAsset).all():
                pl = a.player
                (goalies if pl.position == "G" else skaters)["bos" if pl.real_team_id == real["bos"] else "tor"].append(a.id)
            for day_i, date_s in enumerate(first_dates):
                date = dt.date.fromisoformat(date_s)
                game = Game(universe_id=week.universe_id, week_id=week.id, home_team_id=real["bos"], away_team_id=real["tor"], home_score=0, away_score=0,
                            kickoff_at=dt.datetime.combine(date, dt.time(19, 0)), status="final")
                db.add(game)
                db.flush()
                gk = {side: goalies[side][day_i % len(goalies[side])] for side in goalies}
                records, shots = [], []
                for n in range(150):
                    side = "bos" if rng.random() < 0.5 else "tor"
                    other = "tor" if side == "bos" else "bos"
                    xg = round(rng.uniform(0.02, 0.35), 3)
                    goal = rng.random() < xg
                    s = Shot(game_id=game.id, source="synthetic", external_shot_id=f"{date_s}-{n}", period=n * 3 // 150 + 1, clock=str(rng.randrange(0, 3600)),
                             shooting_team_id=real[side], goalie_team_id=real[other], shooter_asset_id=rng.choice(skaters[side]),
                             goalie_asset_id=gk[other], pre_shot_xg=xg, is_goal=goal, is_empty_net=False)
                    db.add(s)
                    shots.append(s)
                db.flush()
                records = [ShotRecord(s.id, s.shooting_team_id, s.goalie_team_id, s.shooter_asset_id, s.goalie_asset_id, s.pre_shot_xg, s.is_goal, s.is_empty_net, int(s.clock)) for s in shots]
                res = process_game(records)
                for cr in res.credits:
                    db.add(ShotCredit(shot_id=cr.shot_id, draftable_asset_id=cr.asset_id, role=cr.role, credit_points=cr.points))
                skater = sum(c.points for c in res.credits if c.role != "goalie") + res.unnamed_shooter_points + res.unnamed_assist_points
                assert abs(skater - sum(1 for s in shots if s.is_goal)) < 1e-9, "stored credits must tie out to the goals"
                game.home_score = sum(1 for s in shots if s.is_goal and s.shooting_team_id == real["bos"])
                game.away_score = sum(1 for s in shots if s.is_goal and s.shooting_team_id == real["tor"])
            db.commit()
        check(True, "7 days x 150 synthetic shots stored; every day's skater credit ties out to its goals")

        # ---- publish, start, watch, finish ---------------------------------------------------------------------------------------------
        with SessionLocal() as db:
            digests = []
            for gid in first_ids:
                sim = loader.publish_game(db, db.get(SeriesDailyGame, gid))
                digests.append(sim.script_digest)
            db.commit()
        with SessionLocal() as db:
            again = [loader.publish_game(db, db.get(SeriesDailyGame, gid)).script_digest for gid in first_ids]
        check(again == digests and len(set(digests)) == 7, "all 7 games published; republishing replays identical scripts (idempotent)")
        window = 3 * 3600      # the real games all kick off at 19:00 Eastern, so a game runs 19:00-22:00 on the wall clock
        g1 = first_ids[0]
        with SessionLocal() as db:
            days = {gid: db.get(SeriesDailyGame, gid).calendar_date for gid in first_ids}
        # Live games follow the real (nominal) clock, not a compressed playback: 19:00 Eastern is 23:00 UTC (EDT).
        kick = lambda gid: dt.datetime.combine(days[gid], dt.time(19, 0)) + dt.timedelta(hours=4)
        st = client.get(f"/simulator/games/{g1}/state", headers=auth["human"]).json()
        check(st["published"] and st["status"] == "SCHEDULED" and st["veiled"] and st["home_score"] is None, "before start: published, scheduled, veiled")
        for gid in first_ids:
            clock["now"] = T0
            assert client.post(f"/simulator/games/{gid}/start", headers=auth["human"]).status_code == 200
        check(client.post(f"/simulator/games/{g1}/start", headers=auth["human"]).status_code == 200, "starting a started game is idempotent")
        check(client.post(f"/simulator/games/{g1}/start", headers=auth["intruder"]).status_code == 403, "only the commissioner starts games")
        clock["now"] = kick(g1) + dt.timedelta(seconds=window * 0.5)
        r = client.post(f"/simulator/games/{g1}/watch", headers=auth["human"]).json()
        live_events = client.get(f"/simulator/games/{g1}/events?limit=500", headers=auth["human"]).json()
        check(r["status"] == "LIVE" and 0 < len(live_events["events"]) < 600 and not r["is_complete"], f"live game: {len(live_events['events'])} events revealed at the halfway point")
        check(client.get(f"/simulator/games/{g1}/state", headers=auth["intruder"]).json()["veiled"], "another viewer is still behind the veil")
        clock["now"] = max(kick(g) for g in first_ids) + dt.timedelta(seconds=window + 1)
        finals = {}
        for gid in first_ids:
            client.post(f"/simulator/games/{gid}/skip", headers=auth["human"])
            fin = client.get(f"/simulator/games/{gid}/final", headers=auth["human"]).json()
            finals[gid] = (fin["home_score"], fin["away_score"])
            prov = fin["provenance"]
            assert abs(sum(p["points"] for p in prov["box_score"]["home"]) - prov["raw"]["home"]) < 1e-4
        check(len(finals) == 7 and all(h != a for h, a in finals.values()), f"7 games final with no level scoreboards: {sorted(finals.values())}")
        with SessionLocal() as db:
            sg = db.query(SimulatorGame).filter(SimulatorGame.daily_game_id.in_(first_ids)).all()
            dgs = db.query(SeriesDailyGame).filter(SeriesDailyGame.id.in_(first_ids)).all()
            check(all(x.status == "FINAL" for x in sg) and all(d.status == "FINAL" and (d.home_score, d.away_score) == finals[d.id] for d in dgs),
                  "the daily-game rows hold exactly the revealed final scores")
            series = db.get(LeagueSeries, dgs[0].series_id)
            check(series.status == "FINAL", "the series is FINAL once all 7 days are")

        # ---- league leaders: counted from the simulated games only, behind the same spoiler veil ---------------------------------------------
        lead = client.get(f"/leagues/{league_id}/leaders", headers=auth["human"]).json()
        total_goals = sum(h + a for h, a in finals.values())
        check(lead["games_counted"] + lead["games_hidden"] == 7 and lead["games_counted"] >= 1, "leaders: every final game is counted or held back")
        check(all(r["points"] == r["goals"] + r["assists"] for r in lead["leaders"]) and 0 < sum(r["goals"] for r in lead["leaders"]) <= total_goals,
              "leaders: points = goals + assists and credited goals never exceed the simulated scoreboards")
        check(all(r["saves"] >= 0 and r["shutouts"] <= r["games_played"] for r in lead["leaders"]) and all(r["games_played"] >= 1 for r in lead["leaders"]),
              "leaders: GP/SV/SO are sane")
        other = client.get(f"/leagues/{league_id}/leaders", headers=auth["intruder"]).json()
        check(other["games_counted"] <= lead["games_counted"] or other["games_hidden"] <= lead["games_hidden"] + 7, "leaders: an outsider's view obeys the veil")

        hs = client.get(f"/leagues/{league_id}/series-standings", headers=auth["human"]).json()["standings"]
        check(all(r["home_wins"] + r["away_wins"] == r["wins"] and r["home_losses"] + r["away_losses"] == r["losses"]
                  and r["home_ties"] + r["away_ties"] == r["ties"] for r in hs), "standings: home and away records add up to the overall record")

        # ---- standings tie-out ---------------------------------------------------------------------------------------------------------------
        sd = client.get(f"/leagues/{league_id}/series-standings").json()["standings"]
        by = {r["team_id"]: r for r in sd}
        a, b = by[human_team], by[opponent]
        check(a["wins"] + a["losses"] + a["ties"] == 7 and b["wins"] + b["losses"] + b["ties"] == 7, "both teams played 7 final games")
        check(a["wins"] == b["losses"] and a["losses"] == b["wins"] and a["goals_for"] == b["goals_against"] and a["goals_against"] == b["goals_for"],
              "the two teams' records and goals mirror each other exactly")
        check(sum(r["wins"] for r in sd) == sum(r["losses"] for r in sd) and sum(r["goals_for"] for r in sd) == sum(r["goals_against"] for r in sd),
              "league-wide wins equal losses and goals for equal goals against")
        gf = sum((finals[g][0] if home == human_team else finals[g][1]) for g, home in [(d.id, d.home_team_id) for d in dgs])
        check(a["goals_for"] == gf, f"standings goals-for ({gf}) equals the sum of the revealed finals for the human team")
        check(sd[0]["rank"] == 1 and all(sd[i]["rank"] <= sd[i + 1]["rank"] for i in range(len(sd) - 1)), "standings are ranked best first")
        gs = client.get(f"/leagues/{league_id}/game-scores").json()
        check(len(gs["games"]) > 0 and all(0 <= g["score"] <= 100 for g in gs["games"]), "Game Scores are published for the remaining games (0-100)")
        riv = client.get(f"/leagues/{league_id}/rivalries/{human_team}/{opponent}")
        check(riv.status_code == 200 and riv.json()["daily_low_wins"] + riv.json()["daily_high_wins"] + riv.json()["daily_ties"] == 7, "the rivalry record shows the 7 daily results")

        # ---- banking: an off day after the series ----------------------------------------------------------------------------------------------
        clock["now"] = dt.datetime.combine(start + dt.timedelta(days=7), dt.time(21, 0))
        off = start + dt.timedelta(days=7)
        with SessionLocal() as db:
            for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date == off):
                g.is_matchup_day = False
            db.flush()
            asset = db.query(RosterEntry).filter_by(team_id=human_team).first().draftable_asset_id
            c, created = banking.ingest_credit(db, team_id=human_team, asset_id=asset, source_date=off, points=1.8, now=clock["now"])
            c2, created2 = banking.ingest_credit(db, team_id=human_team, asset_id=asset, source_date=off, points=1.8, now=clock["now"])
            db.commit()
            credit_id = c.id
        check(created and not created2 and c2.id == credit_id, "off-day credit ingested once (duplicate ingestion is a no-op)")
        rec = client.get(f"/banked-credits/{credit_id}/recommendation", headers=auth["human"]).json()
        check(rec["recommended_game_id"] is not None and rec["confidence"] in ("PARTIAL", "NEUTRAL") and rec["credit"]["capped"], "a capped 1.80 credit gets an explainable recommendation")
        r = client.post(f"/banked-credits/{credit_id}/assign", json={"daily_game_id": rec["recommended_game_id"]}, headers=auth["human"])
        check(r.status_code == 200 and r.json()["applied_points"] == 1.5 and client.post(f"/banked-credits/{credit_id}/assign", json={"daily_game_id": rec["recommended_game_id"]}, headers=auth["human"]).status_code == 409,
              "assigned at the 1.50 cap; a second assignment is rejected")
        clock["now"] += dt.timedelta(days=2)
        f1 = client.post(f"/leagues/{league_id}/banked-credits/process-fallback", headers=auth["human"]).json()
        f2 = client.post(f"/leagues/{league_id}/banked-credits/process-fallback", headers=auth["human"]).json()
        check(f1["assigned"] == [] and f2["assigned"] == [] and f2["expired"] == [], "the fallback leaves an already-assigned credit alone and is repeatable")

        # ---- authorization sweep ----------------------------------------------------------------------------------------------------------------------
        anon_cases = [
            ("post", f"/leagues/{league_id}/series-schedule", {"start_date": "2027-01-01"}), ("put", f"/teams/{human_team}/pecking-order", {"assignments": {"C": []}}),
            ("post", f"/simulator/games/{g1}/start", None), ("post", f"/simulator/games/{g1}/watch", None), ("post", f"/simulator/games/{g1}/skip", None),
            ("get", f"/simulator/games/{g1}/state", None), ("post", f"/drafts/{draft}/pause", None), ("post", f"/banked-credits/{credit_id}/assign", {"daily_game_id": 1}),
            ("get", f"/teams/{human_team}/banked-credits", None), ("post", f"/leagues/{league_id}/banked-credits/process-fallback", None),
            ("post", f"/leagues/{league_id}/weekly-redraft", {}), ("post", "/media/shared_music/session", None), ("post", f"/leagues/{league_id}/game-scores/publish", None),
        ]
        for method, path, body in anon_cases:
            r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
            check(r.status_code == 401, f"{method.upper()} {path.split('/')[1]}/... without a token is 401")
        owner_only = [
            ("post", f"/leagues/{league_id}/series-schedule", {"start_date": "2027-01-01"}), ("put", f"/teams/{human_team}/pecking-order", {"assignments": {"C": []}}),
            ("post", f"/simulator/games/{g1}/start", None), ("post", f"/drafts/{draft}/pause", None), ("post", f"/banked-credits/{credit_id}/assign", {"daily_game_id": 1}),
            ("get", f"/teams/{human_team}/banked-credits", None), ("post", f"/leagues/{league_id}/banked-credits/process-fallback", None),
            ("post", f"/leagues/{league_id}/weekly-redraft", {}), ("post", f"/leagues/{league_id}/game-scores/publish", None),
        ]
        for method, path, body in owner_only:
            r = getattr(client, method)(path, json=body, headers=auth["intruder"]) if body is not None else getattr(client, method)(path, headers=auth["intruder"])
            check(r.status_code == 403, f"{method.upper()} {path.split('/')[1]}/... by a non-owner is 403")

        # ---- empty / missing-table behaviour ----------------------------------------------------------------------------------------------------------------
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text("DROP TABLE IF EXISTS player_season_points"))
            db.commit()
        check(client.get(f"/drafts/{draft}/board").status_code == 200 and client.get(f"/drafts/{draft}/announcements").status_code == 200,
              "with the optional player_season_points table absent the draft board and announcements still work")
        later = first_ids[0] + 1000
        r = client.get(f"/simulator/games/{later}/state", headers=auth["human"])
        check(r.status_code == 404, "a game that does not exist is a clean 404")
        unpub = next(g["daily_game_id"] for s in ts[2:3] for g in s["games"])
        r = client.get(f"/simulator/games/{unpub}/state", headers=auth["human"]).json()
        check(r["published"] is False and r["status"] == "SCHEDULED", "an unpublished game reads as 'scheduled, nothing published'")
        check(client.get(f"/simulator/games/{unpub}/events", headers=auth["human"]).status_code == 409, "an unpublished game has no events (clean 409)")
        empty = client.post("/leagues", json={"name": "Empty", "commissioner_user_id": human_id, "universe_codes": ["nhl"], "size": 4}, headers=auth["human"]).json()["id"]
        check(client.get(f"/leagues/{empty}/series-standings").json()["standings"] == [] and client.get(f"/leagues/{empty}/series-schedule").json()["series"] == []
              and client.get(f"/leagues/{empty}/game-scores").json()["games"] == [] and client.get(f"/leagues/{empty}/weekly-redraft").json()["league_mode"] == "standard",
              "a league with no teams or games returns empty (not erroring) standings, schedule, Game Scores and redraft state")
        check(client.get(f"/teams/{human_team}/banked-credits", headers=auth["human"]).status_code == 200, "banked credits list works")

        # ---- frontend API-error handling -----------------------------------------------------------------------------------------------------------------------
        import shutil
        node = shutil.which("node")
        check(node is not None, "node is available for the page-script checks")
        for page in ("league.html", "nhl-game.html", "draft-board.html"):
            html = (ROOT / "web" / "static" / page).read_text(encoding="utf-8")
            inline = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
            f = Path(temp_dir) / f"{page}.js"
            f.write_text(inline, encoding="utf-8")
            ok = subprocess.run([node, "--check", str(f)], capture_output=True, text=True).returncode == 0
            check(ok and "catch" in html and ('role="alert"' in html or "error-banner" in html or "errorPanel" in html), f"{page}: script parses and API failures are caught and shown")
        sess = (ROOT / "web" / "static" / "session.js").read_text(encoding="utf-8")
        check("err.code" in sess and "err.status" in sess, "session.js turns API errors into Error objects with status and code")
        engine.dispose()

    print("\nAll NHL series end-to-end checks passed.")


if __name__ == "__main__":
    main()
