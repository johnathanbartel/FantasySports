"""
Checks the live, unveiled box score (hockey/simulator/live_box.py) against the window engine on the same synthetic
three-game window as run_window_engine_audit.py: the view must be read-only, must map every real period to the
fantasy period the engine's own division uses, and its credit must tie out to the stored credit and to what the
engine published. Offline; run with .venv-api.
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

        # A line-1 winger with no game this window, and a ranked backup who has one: the backup must dress, and the box score must say so.
        with SessionLocal() as db:
            nhl_u = db.query(Universe).filter_by(code="nhl").one()
            sea = RealTeam(universe_id=nhl_u.id, name="SEA", external_ref="SEA")
            db.add(sea)
            db.flush()
            lw = db.query(Player).filter_by(full_name="One LW2").one()
            lw.real_team_id = sea.id
            bos = db.query(RealTeam).filter_by(external_ref="BOS").one()
            backup = Player(universe_id=nhl_u.id, real_team_id=bos.id, full_name="One Backup W", position="LW")
            db.add(backup)
            db.flush()
            asset = DraftableAsset(universe_id=nhl_u.id, asset_type="player", player_id=backup.id)
            db.add(asset)
            db.flush()
            fan1 = db.query(Team).filter_by(name="Fan1").one()
            db.add(RosterEntry(team_id=fan1.id, draftable_asset_id=asset.id))
            db.flush()
            rw = db.query(Player).filter_by(full_name="One RW3").one()
            po.replace_groups(db, fan1, {"W": [lw.id, rw.id, backup.id]})
            db.commit()

        def tick(local: datetime.datetime):
            with SessionLocal() as db:
                dg = db.get(SeriesDailyGame, dg_id)
                result = window_engine.tick(db, dg, local)
                db.commit()
                return result

        def box(local: datetime.datetime) -> dict:
            from fantasy_sports.sports.hockey.simulator import live_box
            with SessionLocal() as db:
                dg = db.get(SeriesDailyGame, dg_id)
                before = (db.query(SimulatorEvent).count(), getattr(service.get_sim(db, dg_id), "script_digest", None))
                out = live_box.live_box_score(db, dg, local)
                db.rollback()
                after = (db.query(SimulatorEvent).count(), getattr(service.get_sim(db, dg_id), "script_digest", None))
                check(before == after and not db.dirty and not db.new, "the live box score wrote nothing")
                return out

        def set_game(key, status=None, event_period=None):
            with SessionLocal() as db:
                if status:
                    db.get(Game, ids[key]).status = status
                if event_period:
                    db.add(GameEvent(game_id=ids[key], source="t", external_event_id=f"e{key}{event_period}", event_type="faceoff", period=event_period,
                                     game_seconds=(event_period - 1) * 1200))
                db.commit()

        def tie(d: dict) -> None:
            total = round(sum(p["total"] for p in d["players"]), 4)
            by_period = round(sum(p["credit"]["home"] + p["credit"]["away"] for p in d["periods"]), 4)
            check(abs(total - by_period) < 1e-3 and abs(total - (d["side_credit"]["home"] + d["side_credit"]["away"])) < 1e-3,
                  "player totals, fantasy-period credit and side credit all agree")
            check(all(p["real_periods"] == d["chunk_size"] for p in d["periods"]), "every fantasy period holds N/3 real periods")

        # ---- before the lock: a projection, nothing stored ---------------------------------------------------------------
        d = box(at(SUN, 12))
        check(d["state"] == "live" and not d["lineup_frozen"] and d["published_periods"] == 0 and d["sim_status"] is None,
              "before the first kickoff the box score is a projection of the lineup, with no simulation yet")
        check(d["counting_games"] == 12 and d["real_periods_pooled"] == 36 and d["chunk_size"] == 12, "12 starters pool 36 real periods, 12 per fantasy period")
        check(all(r["projected"] for p in d["periods"] for r in p["rows"] if r["asset_id"] in {pl["asset_id"] for pl in d["players"] if pl["selection"]["state"].startswith("projected")}),
              "undecided players are flagged as projected")
        tie(d)

        w = next(g for g in d["lineups"]["home"]["groups"] if g["group"] == "W")
        check([(r["player"], r["has_game"], r["starter_now"]) for r in w["ranked"]] == [("One LW2", False, False), ("One RW3", True, True), ("One Backup W", True, True)],
              "the pecking order report shows the idle line-1 winger sitting out and his ranked backup stepping in")
        check(not w["vacancies"] and d["lineups"]["home"]["ranked_count"] == 7 and all(len(t["groups"]) == 4 for t in d["lineups"].values()),
              "...with no vacancy, and the report covers every group of both teams")
        check(d["counting_games"] == 12, "the backup fills the slot, so the matchup still has 12 starters")

        # ---- after the lock ------------------------------------------------------------------------------------------------
        set_game("A", "live")
        tick(at(SUN, 13, 30))
        d = box(at(SUN, 13, 30))
        check(d["lineup_frozen"] and d["sim_status"] == "LIVE" and d["published_periods"] == 0, "after the lock the lineup is frozen and the game is live")
        bos = [p for p in d["players"] if p["selection"]["source"] == "best_matchup"]
        check(bos and all(len(p["selection"]["candidates"]) == 2 and sum(c["chosen"] for c in p["selection"]["candidates"]) == 1 for p in bos),
              "a two-game player shows both candidate games and which one counts")
        first = [r for p in d["periods"] for r in p["rows"] if r["nominal_state"] == "in_progress"]
        check(first and all(r["real_period"] == 1 for r in first), "only first periods are in progress at 13:30")
        check(d["periods"][0]["waiting_on"] and not d["periods"][0]["published"], "Period 1 says what it is waiting on")
        tie(d)

        # ---- credit arrives before the period is published ----------------------------------------------------------------
        set_game("A", "final")
        set_game("B", "live")
        tick(at(SUN, 19, 30))
        d = box(at(SUN, 20, 5))
        check(d["published_periods"] == 0 and any("not complete in the stored feed" in w for w in d["periods"][0]["waiting_on"]),
              "at 20:05 Period 1 is nominally over but waits on the stored feed, and the box score says so")
        check(d["periods"][0]["credit"]["home"] + d["periods"][0]["credit"]["away"] != 0.0, "...while the credit already earned in it is visible")
        pre = d["side_credit"]

        # ---- published ---------------------------------------------------------------------------------------------------
        set_game("B", event_period=2)
        tick(at(SUN, 20, 5))
        d = box(at(SUN, 20, 5))
        check(d["published_periods"] == 1 and d["periods"][0]["published"] and "published_goals" in d["periods"][0], "Period 1 shows as published with its goals")
        check(all(p["ties_out"] for p in d["players"]), "published box score equals the live credit of the published periods")
        with SessionLocal() as db:
            sim = service.get_sim(db, dg_id)
            last = db.query(SimulatorEvent).filter_by(simulator_game_id=sim.id).order_by(SimulatorEvent.seq.desc()).first()
            check((d["periods"][0]["published_goals"]["home_total"], d["periods"][0]["published_goals"]["away_total"]) == (last.home_score, last.away_score),
                  "the goals shown for Period 1 are the ones in the stored events")
        check(d["side_credit"] == pre, "publishing did not change the credit earned")
        tie(d)

        # ---- final -------------------------------------------------------------------------------------------------------
        set_game("B", "final")
        tick(at(SUN, 22, 30))
        set_game("C", "live")
        set_game("C", "final")
        tick(at(TUE, 22, 30))
        d = box(at(TUE, 22, 30))
        check(d["published_periods"] == 3 and all(p["published"] and not p["waiting_on"] for p in d["periods"]), "after the last period all three are published, nothing waiting")
        check(all(p["ties_out"] for p in d["players"]), "the final box score ties out for every player")
        tie(d)
        # ---- a frozen starter who leaves the active roster after the lock is named, not silently lost -----------------------------------
        with SessionLocal() as db:
            tuch = db.query(Player).filter_by(full_name="One D4").one()
            fan1 = db.query(Team).filter_by(name="Fan1").one()
            entry = [e for e in db.query(RosterEntry).filter_by(team_id=fan1.id) if e.draftable_asset.player_id == tuch.id][0]
            entry.on_injured_list = True
            db.commit()
        d = box(at(TUE, 22, 30))
        gone = d["lineups"]["home"]["dropped_from_matchup"]
        check([g["player"] for g in gone] == ["One D4"] and d["counting_games"] == 11, "a frozen starter put on the injured list is named as not counted, and the matchup shrinks to 11")
        print("live box score checks passed")


if __name__ == "__main__":
    main()
