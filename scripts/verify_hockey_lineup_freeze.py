"""
Checks that a window game never freezes its lineup on a partly stored schedule, repairs an old short freeze once, and that
the league-wide lineup audit (hockey/simulator/lineup_audit.py) says which games are right, repairable or locked.
Same synthetic three-game window as run_window_engine_audit.py. Offline; run with .venv-api.
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SUN = datetime.date(2026, 10, 11)
TUE = SUN + datetime.timedelta(days=2)


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    """An Eastern wall time as naive UTC (the engine takes UTC; October is EDT = UTC-4)."""
    return datetime.datetime.combine(day, datetime.time(hour, minute)) + datetime.timedelta(hours=4)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs-winengine-", ignore_cleanup_errors=True) as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tmp) / 'engine').as_posix()}"
        from fantasy_sports.platform import pecking_order as po
        from fantasy_sports.platform import series_schedule as ss
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, Game, League, LeagueUniverse, Player, RealTeam, RosterEntry, SeriesDailyGame, SimulatorEvent,
            Team, Universe, User, Week,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.platform.simulator import service
        from fantasy_sports.sports.hockey.models import GameEvent, NhlStandingsSnapshot, Shot, ShotCredit
        from fantasy_sports.sports.hockey.pipeline import ShotRecord, process_game
        from fantasy_sports.sports.hockey.simulator import HockeySimulatorAdapter, window_engine

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            commissioner = User(email="c@example.com", display_name="C")
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add_all([commissioner, nhl])
            db.flush()
            real = {}
            for abbr in ("BOS", "TOR", "NYR", "MTL"):
                real[abbr] = RealTeam(universe_id=nhl.id, name=abbr, external_ref=abbr)
            db.add_all(real.values())
            db.flush()
            league = League(name="Engine", commissioner_user_id=commissioner.id, size=2, series_length_days=7)
            db.add(league)
            db.flush()
            db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
            fan = [Team(league_id=league.id, name=f"Fan{i + 1}", is_ai_owned=True) for i in range(2)]
            db.add_all(fan)
            db.flush()

            def roster(team, skater_team, back_team, prefix):
                assets, ids = {}, []
                for pos, rt in (("C", skater_team), ("LW", skater_team), ("RW", skater_team), ("D", back_team), ("D", back_team), ("G", back_team)):
                    pl = Player(universe_id=nhl.id, real_team_id=real[rt].id, full_name=f"{prefix} {pos}{len(ids) + 1}", position=pos)
                    db.add(pl)
                    db.flush()
                    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=pl.id)
                    db.add(a)
                    db.flush()
                    db.add(RosterEntry(team_id=team.id, draftable_asset_id=a.id))
                    ids.append(pl.id)
                    assets[len(ids)] = a.id            # 1 C, 2 LW, 3 RW, 4 D, 5 D, 6 G
                db.flush()
                po.replace_groups(db, team, {"C": [ids[0]], "W": ids[1:3], "D": ids[3:5], "G": [ids[5]]})
                return assets
            f1, f2 = roster(fan[0], "BOS", "NYR", "One"), roster(fan[1], "TOR", "MTL", "Two")

            week = Week(universe_id=nhl.id, season_year=2026, week_number=1, start_date=SUN, end_date=SUN + datetime.timedelta(days=6))
            db.add(week)
            db.flush()
            games = {}
            for key, day, hour, home, away in (("A", SUN, 13, "BOS", "TOR"), ("B", SUN, 19, "NYR", "MTL"), ("C", TUE, 19, "BOS", "MTL")):
                g = Game(universe_id=nhl.id, week_id=week.id, home_team_id=real[home].id, away_team_id=real[away].id, home_score=0, away_score=0,
                         kickoff_at=datetime.datetime.combine(day, datetime.time(hour)), status="scheduled")
                db.add(g)
                db.flush()
                games[key] = g
            for abbr, pts in (("BOS", 10), ("TOR", 12), ("NYR", 8), ("MTL", 6)):
                db.add(NhlStandingsSnapshot(universe_id=nhl.id, real_team_id=real[abbr].id, as_of_date=SUN, points=pts, games_played=5))

            # (game, shooter asset, shooter real team, goalie asset or None, xG, goal, game clock seconds)
            spec = {
                "A": [(f1[1], "BOS", None, 0.30, True, 300), (f2[2], "TOR", None, 0.40, True, 1500), (f1[2], "BOS", None, 0.20, False, 2600),
                      (f2[1], "TOR", None, 0.50, True, 2700)],
                "B": [(f1[4], "NYR", f2[6], 0.30, True, 400), (f2[4], "MTL", f1[6], 0.40, True, 1400), (f1[5], "NYR", f2[6], 0.25, True, 2500),
                      (f2[5], "MTL", f1[6], 0.35, False, 2800)],
                "C": [(f1[1], "BOS", f2[6], 0.30, True, 200), (f1[3], "BOS", f2[6], 0.45, True, 1300), (f2[5], "MTL", None, 0.35, True, 2500),
                      (f1[2], "BOS", f2[6], 0.30, True, 3000)],
            }
            n = 0
            for key, shots in spec.items():
                records = []
                for shooter, team, goalie, xg, goal, clock in shots:
                    n += 1
                    other = games[key].away_team_id if real[team].id == games[key].home_team_id else games[key].home_team_id
                    sh = Shot(game_id=games[key].id, source="t", external_shot_id=f"s{n}", period=clock // 1200 + 1, clock=str(clock),
                              shooting_team_id=real[team].id, goalie_team_id=other, shooter_asset_id=shooter, goalie_asset_id=goalie,
                              pre_shot_xg=xg, is_goal=goal, is_empty_net=False)
                    db.add(sh)
                    db.flush()
                    records.append(ShotRecord(sh.id, sh.shooting_team_id, sh.goalie_team_id, shooter, goalie, xg, goal, False, clock))
                for c in process_game(records).credits:
                    db.add(ShotCredit(shot_id=c.shot_id, draftable_asset_id=c.asset_id, role=c.role, credit_points=c.points))
            series = ss.generate_schedule(db, league, SUN, cycles=1)
            db.commit()
            dg_id = db.query(SeriesDailyGame).filter_by(window_index=1).first().id
            ids = {k: g.id for k, g in games.items()}

        def tick(local: datetime.datetime, verified: bool = True):
            with SessionLocal() as db:
                dg = db.get(SeriesDailyGame, dg_id)
                result = window_engine.tick(db, dg, local, schedule_verified=verified)
                db.commit()
                return result

        def window_state():
            with SessionLocal() as db:
                return dict(service.get_sim(db, dg_id).provenance["window_state"])

        def move_games(keys, date):
            """Take real games out of the window (as if not stored yet) or put them back (as if ingested later)."""
            with SessionLocal() as db:
                for key in keys:
                    g = db.get(Game, ids[key])
                    if date is None:
                        g.kickoff_at = originals[key]
                    else:
                        originals.setdefault(key, g.kickoff_at)
                        g.kickoff_at = datetime.datetime.combine(date, datetime.time(19))
                db.commit()

        originals: dict = {}
        from fantasy_sports.sports.hockey.simulator import lineup_audit

        def audit() -> dict:
            with SessionLocal() as db:
                return lineup_audit.lineup_audit(db, db.query(League).first().id)[0]

        # ---- the schedule of the window is only partly stored ------------------------------------------------------------------------------
        move_games(["B", "C"], SUN + datetime.timedelta(days=9))
        r = tick(at(SUN, 13, 30), verified=False)
        with SessionLocal() as db:
            check(r.status == "waiting" and service.get_sim(db, dg_id) is None,
                  "with the window's schedule not verified the lineup is NOT frozen: the game waits")
        check(audit()["status"] == "not started", "the audit says the game has not started")
        tick(at(SUN, 13, 30))                       # verified, but only games A is stored: the (old) behaviour that froze a short lineup
        short = window_state()
        check(sorted(len(v) for v in short["lineup"].values()) == [3, 3] and short["schedule_verified"] is True,
              "frozen on a partial schedule a lineup is short (3 starters a side: the defensemen and goalies had no stored game)")

        # ---- an old freeze (made before the fix) is repaired once, while nothing is published ---------------------------------------------------
        with SessionLocal() as db:                  # make it look like a lineup frozen by the old code
            sim = service.get_sim(db, dg_id)
            prov = dict(sim.provenance)
            prov["window_state"] = {k: v for k, v in short.items() if k != "schedule_verified"}
            service.update_provenance(db, sim, prov)
            db.commit()
        a = audit()
        check(a["status"] == "ok" or a["status"] == "repairs on sync", "(the audit reads the stored lineup against today's schedule)")
        move_games(["B", "C"], None)                # the rest of the schedule arrives
        a = audit()
        check(a["status"] == "repairs on sync" and all(s["missing_from_frozen"] for s in a["sides"].values()),
              "the audit names the players the short lineup is missing, and says it repairs on the next sync")
        tick(at(SUN, 13, 31), verified=False)
        check(sorted(len(v) for v in window_state()["lineup"].values()) == [3, 3], "an unverified tick does not touch the lineup")
        tick(at(SUN, 13, 32))
        fixed = window_state()
        check(sorted(len(v) for v in fixed["lineup"].values()) == [6, 6] and fixed["schedule_verified"] is True and fixed["lineup_repaired"]["was"] == short["lineup"],
              "on a verified tick the lineup is resolved again on the full schedule: 6 starters a side, with what it was kept")
        check(audit()["status"] == "ok" and audit()["repaired"] is not None, "the audit now says ok and shows it was repaired")
        with SessionLocal() as db:
            check(service.get_sim(db, dg_id).provenance["window_state"]["selections"] is not None, "selections are kept for the starters who were already in")

        # ---- a verified lineup is frozen for good; one with published periods is never rewritten -----------------------------------------------------
        with SessionLocal() as db:
            fan1 = db.query(Team).filter_by(name="Fan1").one()
            po.replace_groups(db, fan1, {"W": [db.query(Player).filter_by(full_name="One RW3").one().id]})
            db.commit()
        tick(at(SUN, 13, 33))
        check(window_state()["lineup"] == fixed["lineup"], "a verified lineup does not follow later pecking order edits")
        with SessionLocal() as db:                  # an old-style freeze that already published a period
            sim = service.get_sim(db, dg_id)
            prov = dict(sim.provenance)
            prov["window_state"] = {k: v for k, v in window_state().items() if k != "schedule_verified"}
            service.update_provenance(db, sim, prov)
            sim.published_periods = 1
            a = lineup_audit.lineup_audit(db, db.query(League).first().id)[0]
            check(a["status"] == "locked, differs", "an unverified lineup with published periods that differs from the full schedule is reported as locked")
            db.rollback()
        # ---- the operator scripts (never the web app): audit is read-only, re-simulation is a dry run unless --yes ---------------------------------
        import contextlib
        import io
        sys.path.insert(0, str(ROOT / "scripts"))
        import audit_hockey_lineups
        import resimulate_hockey_games

        with SessionLocal() as db:
            league_id = db.query(League).first().id

        def run(module, *argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = module.main([str(x) for x in argv])
            return code, buf.getvalue()

        code, out = run(audit_hockey_lineups, "--league", league_id, "--all")
        check(code == 0 and "3 window games" in out and "[frozen, differs]" in out, "the audit script prints every window game with its status")
        code, out = run(audit_hockey_lineups, "--league", league_id, "--since", "2026-01-01")
        check("1 window games" in out and "not started" not in out, "by default only games with a simulation are audited, not the unplayed rest of the season's schedule")
        code, out = run(audit_hockey_lineups, "--league", league_id + 999)
        check(code == 2 and f"{league_id}:" in out, "a wrong league id is refused and the real league ids are listed (a game id is not a league id)")

        with SessionLocal() as db:                  # make the game look like the bad case: old freeze, a period published, lineup differs
            sim = service.get_sim(db, dg_id)
            prov = dict(sim.provenance)
            state = {k: v for k, v in prov["window_state"].items() if k != "schedule_verified"}
            state["lineup"] = {side: ids[:-2] for side, ids in state["lineup"].items()}
            prov["window_state"] = state
            service.update_provenance(db, sim, prov)
            sim.published_periods = 1
            db.commit()
        check(audit()["status"] == "locked, differs", "(the audit now calls it locked and wrong)")
        code, out = run(resimulate_hockey_games, "--league", league_id, "--since", "2026-01-01")
        with SessionLocal() as db:
            check(code == 0 and "DRY RUN" in out and service.get_sim(db, dg_id) is not None, "without --yes the re-simulation only reports and changes nothing")
        code, out = run(resimulate_hockey_games, "--league", league_id, "--since", "2026-01-01", "--game", dg_id + 1000, "--yes")
        with SessionLocal() as db:
            check("Nothing to do" in out and service.get_sim(db, dg_id) is not None, "another game id is left alone, even with --yes")
        code, out = run(resimulate_hockey_games, "--league", league_id, "--since", "2026-01-01", "--yes")
        with SessionLocal() as db:
            check("Voided 1 game" in out and service.get_sim(db, dg_id) is None and db.get(SeriesDailyGame, dg_id).status == "SCHEDULED",
                  "with --yes the flagged game is voided, ready to be simulated again")
        code, out = run(resimulate_hockey_games, "--league", league_id, "--since", "2026-01-01", "--yes")
        check("Nothing to do" in out, "a game that is not flagged is never touched")
        print("lineup freeze checks passed")


if __name__ == "__main__":
    main()
