"""
Checks that a starter who gets no ice time in his counting game is replaced by the next player on the pecking order:

  * the boxscore ice-time parser and storage (nhl_daily_sync.parse_boxscore_toi / store_game_toi), offline, no network;
  * EARLY: with nothing published, the replacement takes the starter's place (and a replacement who also sat out is skipped);
  * a game whose ice times were never stored replaces nobody;
  * LATE: with periods already published, the replacement is added to the LAST period, and only if his game is final.

Same synthetic three-game window as verify_hockey_lineup_freeze.py. Offline; run with .venv-api.
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
    with tempfile.TemporaryDirectory(prefix="fs-toi-", ignore_cleanup_errors=True) as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tmp) / 'toi').as_posix()}"
        from fantasy_sports.platform import nhl_daily_sync as sync
        from fantasy_sports.platform import pecking_order as po
        from fantasy_sports.platform import series_schedule as ss
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, Game, League, LeagueUniverse, Player, RealTeam, RosterEntry, SeriesDailyGame, Team, Universe, User, Week,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.platform.simulator import service
        from fantasy_sports.sports.hockey.models import NhlStandingsSnapshot, PlayerGameToi, Shot, ShotCredit
        from fantasy_sports.sports.hockey.pipeline import ShotRecord, process_game
        from fantasy_sports.sports.hockey.simulator import window_engine
        from fantasy_sports.sports.hockey.simulator.division import Division
        from fantasy_sports.sports.hockey.simulator.model import CountingGame

        # ---- the boxscore parser and the storage rule -------------------------------------------------------------------------------------
        check(sync.parse_toi_seconds("17:05") == 1025 and sync.parse_toi_seconds("00:00") == 0 and sync.parse_toi_seconds(None) == 0
              and sync.parse_toi_seconds("1:01:01") == 3661, "ice time 'MM:SS' is read as seconds; junk is 0")
        payload = {"playerByGameStats": {
            "homeTeam": {"forwards": [{"playerId": 1, "toi": "12:00"}], "defense": [{"playerId": 2, "toi": "20:30"}],
                         "goalies": [{"playerId": 3, "toi": "60:00"}, {"playerId": 4, "toi": "00:00"}]},
            "awayTeam": {"forwards": [{"playerId": 5, "toi": "09:10"}], "defense": [], "goalies": []}}}
        check(sync.parse_boxscore_toi(payload) == {"1": 720, "2": 1230, "3": 3600, "4": 0, "5": 550}
              and sync.parse_boxscore_toi({}) == {} and sync.parse_boxscore_toi(None) == {},
              "the boxscore lists every dressed player of both teams (a backup goalie who never played is 0); an empty payload gives nothing")

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
            league = League(name="Toi", commissioner_user_id=commissioner.id, size=2, series_length_days=7)
            db.add(league)
            db.flush()
            db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
            fan = [Team(league_id=league.id, name=f"Fan{i + 1}", is_ai_owned=True) for i in range(2)]
            db.add_all(fan)
            db.flush()

            def roster(team, skater_team, back_team, prefix):
                """Six starters (C, LW, RW, D, D, G) plus a bench: a third winger (skaters' team), a third defenseman, two more goalies
                (the back team). Pecking order: W = LW, RW, W3; D = D1, D2, D3; G = G1, G2, G3."""
                assets, ids = {}, []
                spec = (("C", skater_team, "C1"), ("LW", skater_team, "LW"), ("RW", skater_team, "RW"), ("D", back_team, "D1"),
                        ("D", back_team, "D2"), ("G", back_team, "G1"), ("LW", skater_team, "W3"), ("D", back_team, "D3"),
                        ("G", back_team, "G2"), ("G", back_team, "G3"))
                for pos, rt, label in spec:
                    pl = Player(universe_id=nhl.id, real_team_id=real[rt].id, full_name=f"{prefix} {label}", position=pos)
                    db.add(pl)
                    db.flush()
                    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=pl.id)
                    db.add(a)
                    db.flush()
                    db.add(RosterEntry(team_id=team.id, draftable_asset_id=a.id))
                    ids.append(pl.id)
                    assets[label] = a.id
                db.flush()
                po.replace_groups(db, team, {"C": [ids[0]], "W": [ids[1], ids[2], ids[6]], "D": [ids[3], ids[4], ids[7]],
                                             "G": [ids[5], ids[8], ids[9]]})
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
            earlier = Game(universe_id=nhl.id, week_id=week.id, home_team_id=real["BOS"].id, away_team_id=real["NYR"].id, home_score=1, away_score=0,
                           kickoff_at=datetime.datetime.combine(SUN - datetime.timedelta(days=1), datetime.time(19)), status="final")
            db.add(earlier)                                  # an earlier game, outside the window, whose ice times were stored: they are being collected
            db.flush()
            db.add(PlayerGameToi(game_id=earlier.id, draftable_asset_id=f1["C1"], toi_seconds=900))
            for abbr, pts in (("BOS", 10), ("TOR", 12), ("NYR", 8), ("MTL", 6)):
                db.add(NhlStandingsSnapshot(universe_id=nhl.id, real_team_id=real[abbr].id, as_of_date=SUN, points=pts, games_played=5))

            # Best Matchup: BOS skaters play A (vs TOR 12 pts) and C (vs MTL 6): C counts. MTL's D/G play B (vs NYR 8) and C (vs BOS 10): B counts.
            # (game, shooter asset, shooter real team, goalie asset or None, xG, goal, game clock seconds)
            spec = {
                "A": [(f2["LW"], "TOR", None, 0.40, True, 1500), (f2["RW"], "TOR", None, 0.50, True, 2700)],
                "B": [(f1["D1"], "NYR", f2["G1"], 0.30, True, 400), (f2["D1"], "MTL", f1["G3"], 0.40, True, 1400),
                      (f1["D2"], "NYR", f2["G1"], 0.25, True, 2500)],
                "C": [(f1["C1"], "BOS", None, 0.30, True, 200), (f1["RW"], "BOS", None, 0.45, True, 1300), (f1["W3"], "BOS", None, 0.60, True, 2600),
                      (f1["LW"], "BOS", None, 0.30, False, 3000)],
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
            ss.generate_schedule(db, league, SUN, cycles=1)
            db.commit()
            dg_id = db.query(SeriesDailyGame).filter_by(window_index=1).first().id
            ids = {k: g.id for k, g in games.items()}
            on_team = {}                                        # real team abbreviation -> every asset id rostered there
            for label_assets in (f1, f2):
                for a in label_assets.values():
                    pl = db.get(DraftableAsset, a).player
                    on_team.setdefault(db.get(RealTeam, pl.real_team_id).external_ref, []).append(a)

        def tick(local: datetime.datetime):
            with SessionLocal() as db:
                result = window_engine.tick(db, db.get(SeriesDailyGame, dg_id), local)
                db.commit()
                return result

        def state():
            with SessionLocal() as db:
                return dict(service.get_sim(db, dg_id).provenance["window_state"])

        def published() -> int:
            with SessionLocal() as db:
                return service.get_sim(db, dg_id).published_periods or 0

        def finish(key: str, absent: list[int] = (), zero: list[int] = (), store: bool = True) -> None:
            """The real game is final; its ice times are stored for everybody on either team except `absent` (healthy scratches: no row)
            and `zero` (dressed but never played: a row with 0)."""
            with SessionLocal() as db:
                g = db.get(Game, ids[key])
                g.status = "final"
                if store:
                    abbrs = [db.get(RealTeam, g.home_team_id).external_ref, db.get(RealTeam, g.away_team_id).external_ref]
                    toi = {a: (0 if a in zero else 1100) for ab in abbrs for a in on_team[ab] if a not in absent}
                    sync.store_game_toi(db, g.id, {str(a): s for a, s in toi.items()}, {str(a): a for a in toi})
                db.commit()

        # ---- EARLY: nothing published when game B ends ------------------------------------------------------------------------------------
        tick(at(SUN, 13, 30))                               # opens and locks the lineup (players with a game on Sunday afternoon)
        tick(at(SUN, 20, 0))                                # B has kicked off: its players' counting games are stored; nothing is publishable yet
        first = state()
        check(published() == 0 and first["lineup"]["home"] and not first.get("subs"), "locked, nothing published, nobody replaced")
        check(first["selections"][str(f1["G1"])]["game_id"] == ids["B"] and first["selections"][str(f1["C1"])]["game_id"] == ids["C"],
              "(Best Matchup: Fan1's goalie counts game B, Fan1's forwards game C)")

        # Fan1's G1 and G2 never played (G1 listed with 0, G2 not even dressed); Fan2's D1 was a healthy scratch. Game A has no stored ice times.
        finish("A", store=False)
        finish("B", store=False)
        tick(at(SUN, 23, 0))
        check(published() == 0, "a counting game that is final but has no ice times yet holds publishing (the boxscore can lag)")
        with SessionLocal() as db:                           # the boxscore arrives
            g = db.get(Game, ids["B"])
            toi = {a: (0 if a == f1["G1"] else 1100) for ab in ("NYR", "MTL") for a in on_team[ab] if a not in (f1["G2"], f2["D1"])}
            sync.store_game_toi(db, g.id, {str(a): s for a, s in toi.items()}, {str(a): a for a in toi})
            db.commit()
        with SessionLocal() as db:
            rows = {a: s for a, s in db.query(PlayerGameToi.draftable_asset_id, PlayerGameToi.toi_seconds).filter_by(game_id=ids["B"])}
            check(rows[f1["G1"]] == 0 and f1["G2"] not in rows and f2["D1"] not in rows and rows[f1["G3"]] == 1100, "game B's ice times are stored")
        tick(at(SUN, 23, 59))
        after = state()
        lineup1 = after["lineup"]["home"]
        sub_out = {s["out"]: s for s in after["subs"]}
        check(f1["G1"] in sub_out and sub_out[f1["G1"]]["in"] == f1["G3"] and sub_out[f1["G1"]]["mode"] == "early",
              "the goalie who got no ice time is replaced by the next goalie on the pecking order, skipping a second goalie who sat out too")
        check(f1["G3"] in lineup1 and f1["G1"] not in lineup1 and f1["G2"] not in lineup1, "the replacement takes his place in the lineup")
        check(f2["D1"] in sub_out and sub_out[f2["D1"]]["in"] == f2["D3"] and f2["D3"] in after["lineup"]["away"],
              "a skater who was a healthy scratch is replaced by the next defenseman")
        check(all(s["mode"] == "early" for s in after["subs"]) and str(f1["G1"]) not in after["selections"],
              "an early replacement drops the old choice of game")
        check(f2["LW"] in after["lineup"]["away"] and f2["LW"] not in sub_out,
              "a game whose ice times were never stored (game A) replaces nobody")
        check(published() == 2, "the first two fantasy periods then publish with the replacements in place (and nothing was published before)")
        with SessionLocal() as db:
            table = service.get_sim(db, dg_id).provenance["division"]["periods"]
            names = {r["player"] for p in table for r in p["rows"]}
            check("One G3" in names and "One G1" not in names and "Two D3" in names, "the published division lists the replacements, not the starters who sat out")

        # ---- LATE: periods are published when game C ends -----------------------------------------------------------------------------------
        before = state()
        finish("C", absent=[f1["LW"]])                             # Fan1's left wing (counting game C) never got on the ice; W3 played and scored in C
        tick(at(TUE, 23, 59))
        end = state()
        late = [s for s in end["subs"] if s["mode"] == "late"]
        check(len(late) == 1 and late[0]["out"] == f1["LW"] and late[0]["in"] == f1["W3"], "with periods published the next winger on the order is added late")
        check(end["late"] == [f1["W3"]] and f1["LW"] in end["lineup"]["home"] and f1["W3"] in end["lineup"]["home"],
              "the starter who sat out stays (published periods never change) and the replacement is added")
        check(end["subs"][: len(before["subs"])] == before["subs"], "the earlier replacements are untouched")
        with SessionLocal() as db:
            sim = service.get_sim(db, dg_id)
            check(sim.published_periods == 3 and db.get(SeriesDailyGame, dg_id).status == "FINAL", "the last period publishes and the game is final")
            table = sim.provenance["division"]["periods"]
            w3 = [(p["fantasy_period"], r) for p in table for r in p["rows"] if r["player"] == "One W3"]
            check(len(w3) == 3 and {k for k, _ in w3} == {3} and sum(r["points"] for _, r in w3) > 0,
                  "the replacement's three real periods all sit in the last fantasy period, with his points")
            check(table[2]["real_periods"] == table[0]["real_periods"] + 3, "the last period is larger by the replacement's three real periods")

        # ---- the ingest step: a final game's boxscore is stored once, a failed fetch stores nothing -------------------------------------
        real_fetch = sync._fetch_json
        try:
            with SessionLocal() as db:
                g = db.get(Game, ids["A"])
                refs = {"901": f2["LW"], "902": f2["RW"]}
                sync._fetch_json = lambda url, timeout=6.0: None
                check(sync.ensure_game_toi(db, g, 2026020001, refs).startswith("FETCH FAILED") and not db.query(PlayerGameToi).filter_by(game_id=g.id).count(),
                      "an unreadable boxscore stores nothing, so nobody is treated as having sat out")
                sync._fetch_json = lambda url, timeout=6.0: {"playerByGameStats": {"homeTeam": {"forwards": [
                    {"playerId": 901, "toi": "15:00"}, {"playerId": 902, "toi": "00:00"}, {"playerId": 999, "toi": "10:00"}]}, "awayTeam": {}}}
                check(sync.ensure_game_toi(db, g, 2026020001, refs) == "stored 2 ice times", "a final game's boxscore is stored for the players we know")
                sync._fetch_json = lambda url, timeout=6.0: (_ for _ in ()).throw(AssertionError("fetched again"))
                check(sync.ensure_game_toi(db, g, 2026020001, refs) == "kept", "a game that already has its ice times is not fetched again")
                db.rollback()
        finally:
            sync._fetch_json = real_fetch

        # ---- the division and the live box keep late replacements apart --------------------------------------------------------------------
        games_ = [CountingGame(i, f"p{i}", "home", 100 + i, datetime.datetime(2026, 10, 11, 12 + i)) for i in range(1, 5)]
        d = Division.build(games_, late={4})
        check(d.pooled == 12 and [len(c) for c in d.chunks] == [3, 3, 6] and all(s.fantasy_period == 3 for s in d.slots if s.asset_id == 4),
              "Division.build puts a late player's three real periods into the last period only")
        print("ice-time substitution checks passed")


if __name__ == "__main__":
    main()
