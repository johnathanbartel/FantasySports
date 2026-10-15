"""
Offline audit of the live window engine (sports/hockey/simulator/window_engine.py; docs/hockey_spec.md 6.5): a window
game is opened at its first kickoff and published period by period as the real periods end.

A throwaway SQLite database holds two fantasy teams, four real teams and three real games in one window (Sun-Tue):

    A  Sun 13:00  BOS v TOR        B  Sun 19:00  NYR v MTL        C  Tue 19:00  BOS v MTL

Fan1 owns BOS skaters and NYR defense/goalie, Fan2 owns TOR skaters and MTL defense/goalie. BOS and MTL each play two
games, so Best Matchup decides (standings snapshot of that Sunday: BOS 10, TOR 12, NYR 8, MTL 6): BOS counts the game
against MTL (C), MTL counts the game against NYR (B). The clock is walked through the window and the checks are made at
each step. Pure offline: no network; time is a parameter.

Exits 0 only if every check passes. From the repository root:

    .venv-api/Scripts/python.exe src/fantasy_sports/sports/hockey/scripts/run_window_engine_audit.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
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

        def tick(local: datetime.datetime):
            with SessionLocal() as db:
                dg = db.get(SeriesDailyGame, dg_id)
                result = window_engine.tick(db, dg, local)
                db.commit()
                return result

        def set_game(key, status=None, event_period=None):
            with SessionLocal() as db:
                if status:
                    db.get(Game, ids[key]).status = status
                if event_period:
                    db.add(GameEvent(game_id=ids[key], source="t", external_event_id=f"e{key}{event_period}", event_type="faceoff", period=event_period,
                                     game_seconds=(event_period - 1) * 1200))
                db.commit()

        def sim_state():
            with SessionLocal() as db:
                sim = service.get_sim(db, dg_id)
                if sim is None:
                    return None
                evs = db.query(SimulatorEvent).filter_by(simulator_game_id=sim.id).order_by(SimulatorEvent.seq).all()
                return sim.published_periods, sim.status, sim.script_digest, [(e.seq, e.kind, e.period, e.game_second, e.home_score, e.away_score) for e in evs], dict(sim.provenance)

        # ---- before the lock ---------------------------------------------------------------------------------------------
        r = tick(at(SUN, 12))
        check(r.status == "waiting" and sim_state() is None, "before the first kickoff nothing is published")

        # ---- the lock -----------------------------------------------------------------------------------------------------
        set_game("A", "live")
        r = tick(at(SUN, 13, 30))
        pp, status, digest0, events0, prov = sim_state()
        check(r.status == "opened" and pp == 0 and status == "LIVE" and [e[1] for e in events0] == ["period_start"], "at the first kickoff the game opens with Period 1 beginning")
        lineup = prov["window_state"]["lineup"]
        check(len(lineup["home"]) == 6 and len(lineup["away"]) == 6, "the lineup is frozen at the lock: 6 starters a side")
        sel = prov["window_state"]["selections"]
        check(sel[str(f1[1])]["source"] == "best_matchup" and sel[str(f1[1])]["game_id"] == ids["C"]
              and sel[str(f1[1])]["opponent_points"] == {str(ids["A"]): 12, str(ids["C"]): 6},
              "BOS players: Best Matchup at their Sunday 13:00 lock picks Tuesday v MTL (6 points) over Sunday v TOR (12)")
        check(sel[str(f2[1])]["source"] == "single" and str(f2[4]) not in sel, "a single-game player is decided at once; MTL's two-game players wait for their own lock (Sun 19:00)")
        check(prov["wall_clock"] is not None, "the clock is published from the first tick")

        # ---- Sunday evening -------------------------------------------------------------------------------------------------
        set_game("A", "final")
        set_game("B", "live")
        tick(at(SUN, 19, 30))
        pp, _, _, _, prov = sim_state()
        sel = prov["window_state"]["selections"]
        check(sel[str(f2[4])]["game_id"] == ids["B"] and sel[str(f2[4])]["opponent_points"] == {str(ids["B"]): 8, str(ids["C"]): 10},
              "MTL players are decided at Sun 19:00: Sunday v NYR (8 points) beats Tuesday v BOS (10)")
        check(pp == 0, "no period is published while its real periods are still being played")
        tick(at(SUN, 20, 5))
        check(sim_state()[0] == 0, "Period 1 waits for the feed: the real periods end at 20:00 but the stored game has no later period yet")
        set_game("B", event_period=2)
        r = tick(at(SUN, 20, 5))
        pp, status, digest1, events1, prov = sim_state()
        check(r.status == "published" and pp == 1 and status == "LIVE", "Period 1 is published as soon as its real periods have ended and the feed shows them complete")
        check(events1[: len(events0)] == events0 and events1[-1][1:3] == ("period_start", 2), "the published events continue the opening event and end with Period 2 beginning")
        check(all(1 <= e[3] <= 1199 for e in events1 if e[1] not in ("period_start", "period_end")) and all(e[2] == 1 for e in events1[:-1]),
              "every Period 1 event sits strictly inside Period 1")
        check(len(prov["division"]["periods"][0]["rows"]) == 12, "the division table lists 12 real periods for fantasy period 1")
        in_p1 = {"home": 0.0, "away": 0.0}
        for row in prov["division"]["periods"][0]["rows"]:
            in_p1[row["side"]] += row["points"]
        check(abs(in_p1["home"] - prov["raw"]["home"]) < 1e-6 and abs(in_p1["away"] - prov["raw"]["away"]) < 1e-6
              and all(r["points"] == 0.0 for p in prov["division"]["periods"][1:] for r in p["rows"]),
              "after Period 1 the cumulative credit is exactly the credit of its 12 real periods (nothing from later periods)")
        check(digest1 != digest0, "the digest chains forward when a period is appended")
        # Period 2 must wait: its last real periods end at 22:00 and B's third period is not final yet.
        tick(at(SUN, 21, 30))
        check(sim_state()[0] == 1, "Period 2 is not published before its real periods have nominally ended")
        set_game("B", "final")
        tick(at(SUN, 22, 30))
        pp, _, digest2, events2, _ = sim_state()
        check(pp == 2 and events2[: len(events1)] == events1, "Period 2 is published and Period 1's events are untouched")
        check(all(a[4] <= b[4] and a[5] <= b[5] for a, b in zip(events2, events2[1:])), "the scoreboard only climbs (ratchet)")

        # ---- Tuesday ---------------------------------------------------------------------------------------------------------
        set_game("C", "live")
        tick(at(TUE, 21, 30))
        check(sim_state()[0] == 2, "Period 3 is not published before the last real period has nominally ended (22:00)")
        tick(at(TUE, 22, 30))
        check(sim_state()[0] == 2 and sim_state()[1] == "LIVE", "...nor while the last game is not final in the feed")
        set_game("C", "final")
        r = tick(at(TUE, 22, 30))
        pp, status, digest3, events3, prov = sim_state()
        check(r.status == "published" and pp == 3 and status == "FINAL", "the last period is published and the game is FINAL")
        check(events3[: len(events2)] == events2 and events3[-1][1] == "game_end", "earlier periods are untouched and the game ends")
        check(events3[-1][3] == 3600 and events3[-1][4] != events3[-1][5], "the final is decisive")
        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, dg_id)
            check((dg.status, dg.home_score, dg.away_score) == ("FINAL", events3[-1][4], events3[-1][5]), "the window row mirrors the final score")
        again = tick(at(TUE, 23, 0))
        check(again.status == "idle" and sim_state()[2] == digest3 and sim_state()[3] == events3, "ticking again changes nothing (idempotent)")

        # ---- the final matches the whole-game normalization (same credit, same seed) ------------------------------------------
        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, dg_id)
            sim = service.get_sim(db, dg_id)
            home, away = db.get(Team, dg.home_team_id), db.get(Team, dg.away_team_id)
            state = sim.provenance["window_state"]
            by_asset = {}
            for t in (home, away):
                for _, (aid, pl) in po.roster_players(db, t.id).items():
                    by_asset[aid] = pl
            side_of = {a: s for s in ("home", "away") for a in state["lineup"][s]}
            counting = []
            from fantasy_sports.sports.hockey.simulator import CountingGame
            for a, rec in state["selections"].items():
                g = db.get(Game, rec["game_id"])
                counting.append(CountingGame(int(a), by_asset[int(a)].full_name, side_of[int(a)], g.id, g.kickoff_at))
            inputs = window_engine._inputs(db, dg, home, away, by_asset, side_of, counting, state["selections"], state)
            whole = HockeySimulatorAdapter().build_script(inputs, dg.id)
            periods = prov["normalization"]["periods"]
            ratcheted = any(p["ratcheted"] or p["forced_leader"] for p in periods)
            last = whole.events[-1]
            if not ratcheted:
                check((last.home_score, last.away_score) == (events3[-1][4], events3[-1][5]),
                      f"with no ratchet the period-by-period final {events3[-1][4]}-{events3[-1][5]} equals the whole-game normalization")
            else:
                check(True, f"a ratchet applied here (periods: {[(p['home_goals'], p['away_goals']) for p in periods]}); whole game was {last.home_score}-{last.away_score}")
            check(abs(prov["raw"]["margin"] - whole.provenance["raw"]["margin"]) < 1e-6, "the cumulative credit after the last period ties out to the whole game's raw margin")
            check(abs(sum(p["points"] for p in prov["box_score"]["home"]) - prov["raw"]["home"]) < 1e-5
                  and abs(sum(p["points"] for p in prov["box_score"]["away"]) - prov["raw"]["away"]) < 1e-5, "the box score ties out to the raw credit")

        print("\nAll window engine audits passed.")


if __name__ == "__main__":
    main()
