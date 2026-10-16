"""
Checks period-by-period watching of an NHL window game (platform/simulator/service.py): no spoiler veil, a public clock,
the intermission while a period is simulated, the score moving only when a period is watched, one period at a time.
Same synthetic three-game window as run_window_engine_audit.py. Offline; run with .venv-api.
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import types
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


        def prov_after_il():
            with SessionLocal() as db:
                return dict(service.get_sim(db, dg_id).provenance)

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

        with SessionLocal() as db:
            viewer = commissioner_id = db.query(User).first().id

        def view(local: datetime.datetime):
            with SessionLocal() as db:
                sim = service.get_sim(db, dg_id)
                v = service.view_state(db, sim, viewer, local)
                db.commit()
                return v

        def events_for(local: datetime.datetime):
            with SessionLocal() as db:
                sim = service.get_sim(db, dg_id)
                page, bound, _ = service.view_events(db, sim, viewer, local, 0, 500)
                page = [types.SimpleNamespace(seq=e.seq, period=e.period) for e in page]
                db.commit()
                return page, bound

        def act(fn, local: datetime.datetime, *args):
            with SessionLocal() as db:
                sim = service.get_sim(db, dg_id)
                out = fn(db, sim, viewer, local, *args)
                db.commit()
                return out

        def refuses(fn, local, *args) -> str | None:
            try:
                act(fn, local, *args)
            except service.SimulatorError as e:
                return e.code
            return None

        def schedule_ready():
            from fantasy_sports.api.routers import series_schedule as router
            with SessionLocal() as db:
                league = db.query(League).first()
                out = router._out(league, router._league_series(db, league.id), db, viewer)
                return [g for s in out.series for g in s.daily_games if g.window_index == 1][0].highlights_ready

        def schedule_score():
            """What the league scoreboard shows this viewer for the window game: (home, away, owner_watched)."""
            from fantasy_sports.api.routers import series_schedule as router
            with SessionLocal() as db:
                league = db.query(League).first()
                out = router._out(league, router._league_series(db, league.id), db, viewer)
                g = [g for s in out.series for g in s.daily_games if g.window_index == 1][0]
                return g.home_score, g.away_score, g.owner_watched

        with SessionLocal() as db:                       # human-owned teams: the league scoreboard veils by viewer (AI v AI shows everything)
            for t in db.query(Team).all():
                t.is_ai_owned = False
                t.owner_user_id = db.query(User).first().id
            db.commit()

        # ---- the game is live: public clock, no veil, no score ---------------------------------------------------------------
        set_game("A", "live")
        tick(at(SUN, 13, 30))
        v = view(at(SUN, 13, 30))
        check(v.progressive and not v.veiled and v.status == "LIVE", "a window game has no spoiler veil")
        check(v.period == 1 and v.seconds_remaining is not None and 0 < v.seconds_remaining < 1200, "the live clock is public before the viewer has watched anything")
        check(v.home_score is None and v.away_score is None, "...but the score is hidden")
        check(v.home_shots is not None and v.away_shots is not None, "...while shots on goal are public")
        check(schedule_ready() is False, "the Highlights button does not flash while nothing has been simulated")
        early = v.home_shots + v.away_shots
        check([p["phase"] for p in v.period_phases] == ["in_progress", "upcoming", "upcoming"], "Period 1 is in progress, the others not started")
        check(not v.can_skip and events_for(at(SUN, 13, 30))[1] == 0, "nothing can be skipped to or fetched while no period is published")

        # ---- a frozen starter leaves the active roster after the lock: the game must keep running (it used to raise KeyError) ----------------
        with SessionLocal() as db:
            d4 = db.query(Player).filter_by(full_name="One D4").one()
            fan1 = db.query(Team).filter_by(name="Fan1").one()
            entry = [e for e in db.query(RosterEntry).filter_by(team_id=fan1.id) if e.draftable_asset.player_id == d4.id][0]
            entry.on_injured_list = True
            db.commit()
        r = tick(at(SUN, 13, 35))
        check(r.status in ("idle", "opened"), "the engine ticks normally after a frozen starter goes on the injured list")
        check("engine_error" not in prov_after_il(), "...and records no error")

        # ---- the engine's read-outs can fail without stopping the game, and it says so ---------------------------------------------
        def prov_now():
            with SessionLocal() as db:
                return dict(service.get_sim(db, dg_id).provenance)

        real_live_shots = window_engine._live_shots

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        window_engine._live_shots = boom
        r = tick(at(SUN, 13, 31))
        check(r.status in ("idle", "opened") and "boom" in prov_now().get("engine_error", ""), "a failing read-out is recorded and does not stop the tick")
        window_engine._live_shots = real_live_shots
        tick(at(SUN, 13, 32))
        check("engine_error" not in prov_now() and prov_now()["ticked_at"], "the next good tick clears it and stamps when the engine ran")

        # ---- a game page drives only its own window (offline: no feed is fetched) ---------------------------------------------------
        from fantasy_sports.platform import nhl_daily_sync as sync_module
        sync_module._fetch_json = lambda *a, **k: None
        sync_module.current_eastern_date = lambda: SUN
        service.utcnow = lambda: at(SUN, 13, 40)
        with SessionLocal() as db:
            sync_module.sync_window_game(db, db.get(SeriesDailyGame, dg_id))
        check(prov_now()["ticked_at"].startswith(at(SUN, 13, 40).isoformat(timespec="seconds")[:16]), "sync_window_game ticks the game it was given")

        # ---- the real periods are over, the simulation has not run: the intermission -------------------------------------------
        set_game("A", "final")
        set_game("B", "live")
        tick(at(SUN, 19, 30))
        v = view(at(SUN, 20, 5))
        check(v.period_phases[0]["phase"] == "simulating" and v.published_periods == 0, "Period 1 is over but not published: it is simulating (the Zamboni)")
        check(v.period == 2 or v.period == 1, "the clock keeps running")
        sim_pct = v.period_phases[0]["percent"]
        check(0 <= sim_pct < 100, "the Zamboni meter shows how much of the feed has arrived (not done yet)")
        before_publish = v.home_shots + v.away_shots
        before_sides = (v.home_shots, v.away_shots)
        check(before_publish >= early and before_publish > 0, "shots on goal have built up during the period, before anything is published")
        later = view(at(SUN, 19, 40))
        check(early <= later.home_shots + later.away_shots <= before_publish, "shot totals only climb as the clock advances")

        # ---- published, not watched ----------------------------------------------------------------------------------------
        set_game("B", event_period=2)
        tick(at(SUN, 20, 5))
        v = view(at(SUN, 20, 5))
        check(v.period_phases[0]["phase"] == "ready" and v.home_score is None, "Period 1 is ready to watch and the score is still hidden")
        check((v.home_shots, v.away_shots) == before_sides, "publishing a period leaves the scoreboard's shot count exactly as it was: the estimate IS the highlights")
        check(v.can_skip, "...and the viewer may reveal what has been simulated")
        check(schedule_ready() is True, "the Highlights button flashes: a simulated period is waiting to be watched")
        check(events_for(at(SUN, 20, 5)) == ([], 0) or events_for(at(SUN, 20, 5))[1] == 0, "without watching, no events are served")
        check(refuses(service.progress, at(SUN, 20, 5), 1) == "NOT_WATCHING", "progress needs a watch first")
        act(service.watch, at(SUN, 20, 5))
        page, bound = events_for(at(SUN, 20, 5))
        check(page and all(e.period <= 2 for e in page) and bound == max(e.seq for e in page if e.period == 1), "watching serves exactly period 1's events (and the opening of period 2)")
        v = view(at(SUN, 20, 5))
        check(v.home_score is None and v.shown_seq == 0, "...yet fetching them does not move the score: only watching does")
        last1 = v.period_phases[0]["last_seq"]
        check(refuses(service.progress, at(SUN, 20, 5), last1 + 1) == "AHEAD_OF_GAME", "a viewer cannot claim to have watched beyond the period they may watch")
        act(service.progress, at(SUN, 20, 5), last1)
        v = view(at(SUN, 20, 5))
        with SessionLocal() as db:
            sim = service.get_sim(db, dg_id)
            evs = service._events(db, sim)
            at_end = [e for e in evs if e.seq == last1][0]
        check(v.period_phases[0]["phase"] == "watched" and (v.home_score, v.away_score) == (at_end.home_score, at_end.away_score),
              "after watching period 1 the score is the score at the end of period 1")
        check(schedule_score()[:2] == (at_end.home_score, at_end.away_score) and schedule_score()[2], "the league scoreboard agrees")
        check(schedule_ready() is False, "...and stops flashing once the period is watched")

        # ---- period 2 published: only one period at a time ---------------------------------------------------------------------
        set_game("B", "final")
        tick(at(SUN, 22, 30))
        v = view(at(SUN, 22, 30))
        check([p["phase"] for p in v.period_phases][:2] == ["watched", "ready"], "period 2 is ready")
        check(schedule_ready() is True, "...and the button flashes again for it")
        check((v.home_score, v.away_score) == (at_end.home_score, at_end.away_score), "publishing period 2 does not change what the viewer sees")
        hidden = schedule_score()
        check(hidden[:2] == (at_end.home_score, at_end.away_score), "nor does it change the league scoreboard")
        page, bound = events_for(at(SUN, 22, 30))
        with SessionLocal() as db:
            sim = service.get_sim(db, dg_id)
            marks = service.period_marks(service._events(db, sim))
        check(bound == marks[2], "events are served through the end of period 2, no further")
        act(service.progress, at(SUN, 22, 30), marks[2])
        v = view(at(SUN, 22, 30))
        check(v.period_phases[1]["phase"] == "watched", "period 2 watched")

        # ---- reveal everything simulated so far -----------------------------------------------------------------------------------
        set_game("C", "live")
        set_game("C", "final")
        tick(at(TUE, 22, 30))
        v = view(at(TUE, 22, 30))
        check(v.status == "FINAL" and v.period_phases[2]["phase"] == "ready" and not v.is_complete, "the game is final but the viewer has one period left")
        act(service.skip, at(TUE, 22, 30))
        v = view(at(TUE, 22, 30))
        check(v.is_complete and v.provenance and v.home_score is not None, "skipping to the box score completes the game")
        check(schedule_ready() is False, "nothing left to watch, nothing flashes")

        # ---- filler shots: realism only ----------------------------------------------------------------------------------------
        with SessionLocal() as db:
            sim = service.get_sim(db, dg_id)
            evs = service._events(db, sim)
            goalies = {f1[6], f2[6]}
            filler = [(e.period, e.detail["asset_id"], e.team_side, e.home_score, e.away_score) for e in evs if (e.detail or {}).get("origin") == "filler"]
            by_player = {}
            for per, a, side, hs, as_ in filler:
                by_player[(per, a)] = by_player.get((per, a), 0) + 1
            last = evs[-1]
            norm = sim.provenance["normalization"]
            check(filler and all(1 <= n <= 3 for n in by_player.values()), "every skater who played in a period has 1 to 3 filler shots in it")
            check(not any(a in goalies for _, a, *_ in filler), "goalies get none")
            check(all(e.kind == "shot" and (e.detail or {}).get("fabricated") for e in evs if (e.detail or {}).get("origin") == "filler"), "filler events are shots, labelled fabricated")
            check((last.home_score, last.away_score) == (norm["home_goals"], norm["away_goals"]), "the final score is still the normalized goals: filler never scores")
            for a, b in zip(evs, evs[1:]):
                if (b.detail or {}).get("origin") == "filler":
                    assert (a.home_score, a.away_score) == (b.home_score, b.away_score), "a filler shot changed the score"
            check(True, "...and no filler shot moved the score")
            check(last.home_shots >= len([f for f in filler if f[2] == "home"]) and last.away_shots >= len([f for f in filler if f[2] == "away"]), "filler shots are counted in the shot totals")
            players = {(per, a) for per, a, *_ in filler}
            check({per for per, _ in players} == {1, 2, 3}, "all three periods have filler")
            with SessionLocal() as db2:
                again = service._events(db2, service.get_sim(db2, dg_id))
                check([e.headline for e in again] == [e.headline for e in evs], "the published events are stable")
        # ---- one shot count everywhere ----------------------------------------------------------------------------------------
        from fantasy_sports.sports.hockey.simulator import live_box
        with SessionLocal() as db:
            sim = service.get_sim(db, dg_id)
            evs = service._events(db, sim)
            box = live_box.live_box_score(db, db.get(SeriesDailyGame, dg_id), at(TUE, 22, 30))
            side_rows = {s: sum(r["shots"] for p in box["periods"] for r in p["rows"] if r["side"] == s) for s in ("home", "away")}
            shot_events = {s: sum(1 for e in evs if e.team_side == s and e.kind == "shot") + sum(1 for e in evs if e.team_side == s and e.kind == "score" and e.detail.get("origin") == "earned") for s in ("home", "away")}
            check((evs[-1].home_shots, evs[-1].away_shots) == (shot_events["home"], shot_events["away"]), "the shot total equals the shot events in the highlights (goals from invented credit add none)")
            check((box["shots_on_goal"]["home"], box["shots_on_goal"]["away"]) == (evs[-1].home_shots, evs[-1].away_shots), "the box score page's shot total equals the highlights")
            check(side_rows == {"home": evs[-1].home_shots, "away": evs[-1].away_shots}, "the box score's per-period rows add up to the same number")
        # ---- void and re-simulate -----------------------------------------------------------------------------------------------------------
        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, dg_id)
            check(service.reset_simulation(db, dg) is True, "a simulation can be voided")
            db.commit()
        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, dg_id)
            sim_gone = service.get_sim(db, dg_id) is None
            check(sim_gone and dg.status == "SCHEDULED" and (dg.home_score, dg.away_score) == (0, 0) and dg.series.status == "SCHEDULED",
                  "voiding clears the simulation and returns the game and its series to scheduled")
            check(db.query(SimulatorEvent).count() == 0, "...and deletes the events")
        service.utcnow = lambda: at(TUE, 23, 0)
        tick(at(TUE, 23, 0))
        with SessionLocal() as db:
            sim = service.get_sim(db, dg_id)
            check(sim is not None and sim.status == "FINAL" and sim.published_periods == 3 and sim.provenance["window_state"]["schedule_verified"] is True,
                  "the next tick simulates it again from the stored feed, on a verified lineup, all the way to final")
        print("period-by-period watching checks passed")


if __name__ == "__main__":
    main()
