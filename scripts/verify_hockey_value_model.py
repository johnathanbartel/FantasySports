"""
Checks the NHL value model (docs/hockey_simulator_scoring.md "Value model"): every action creates value, goalie saves take value from the
other side (never below zero, the excess goes to the goalie's own team), and goals follow from the net value with bounded randomness,
with no fixed total. Offline; run with .venv-api.
"""

from __future__ import annotations

import collections
import os
import random
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs-value-", ignore_cleanup_errors=True) as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tmp) / 'value').as_posix()}"
        from fantasy_sports.sports.hockey.simulator import loader, progressive
        from fantasy_sports.sports.hockey.simulator.attribution import build_value_ledger, tie_out_value_ledger
        from fantasy_sports.sports.hockey.simulator.model import (
            ASSIST_SHARE, HIT_VALUE, TAKEAWAY_VALUE, VALUE_NOISE_CAP, VALUE_SCALE, CreditLine, HockeyGameInputs, SideInput,
        )

        n = 0

        def line(asset, role, points, group=None, second=100):
            nonlocal n
            n += 1
            return CreditLine(f"l{n}", group or f"g{n}", asset, f"P{asset}", "G" if role == "goalie" else "C", role, points, second)

        def inputs(home, away):
            return HockeyGameInputs(1, "2026-10-14", SideInput(1, "Home", tuple(home)), SideInput(2, "Away", tuple(away)))

        # ---- value, saves and the overage rule -----------------------------------------------------------------------------------------
        led = build_value_ledger(inputs([line(1, "shooter", 1.0), line(9, "goalie", 0.4)], [line(2, "shooter", 0.5), line(8, "goalie", 0.3)]))
        # C_h 1.0, G_h 0.4, C_a 0.5, G_a 0.3: x = 1.0 - 0.3 = 0.7, y = 0.4 - 0.5 = -0.1
        check(abs(led.offense["home"] - 0.7) < 1e-9 and abs(led.offense["away"] - 0.1) < 1e-9,
              "a goalie's saves are taken off the other side's created value (home 1.0 - 0.3 saved = 0.7; away 0.5 - 0.4 saved = 0.1)")
        led = build_value_ledger(inputs([line(1, "shooter", 0.2), line(9, "goalie", 1.5)], [line(2, "shooter", 0.5), line(8, "goalie", 0.0)]))
        # x = 0.2 - 0 = 0.2, y = 1.5 - 0.5 = 1.0: nothing negative, the goalie's excess 1.0 is added to his own team
        check(abs(led.offense["home"] - 1.2) < 1e-9 and led.offense["away"] == 0.0,
              "value never goes negative: away created 0.5 but home's goalie saved 1.5, so away nets 0 and the excess 1.0 goes to home")
        rng = random.Random(3)
        for _ in range(500):
            h = [line(1, "shooter", rng.random() * 2), line(3, "blocker", rng.random() * 0.3), line(9, "goalie", rng.random() * 3)]
            a = [line(2, "shooter", rng.random() * 2), line(4, "takeaway", rng.random() * 0.1), line(8, "goalie", rng.random() * 3)]
            i = inputs(h, a)
            led = build_value_ledger(i)
            tie_out_value_ledger(i, led)
            assert led.offense["home"] >= 0 and led.offense["away"] >= 0
        check(True, "500 random games: net values are never negative and the net differential is always exactly the credit differential")

        # ---- goals follow the value; nothing fixes the total ----------------------------------------------------------------------------
        def play(value_home, value_away, seed):
            led = types.SimpleNamespace(offense={"home": value_home, "away": value_away}, raw={"home": value_home, "away": value_away})
            units = progressive.noise_units(seed, seed)
            prev = (0, 0)
            norm = None
            for k in (1, 2, 3):
                part = types.SimpleNamespace(offense={"home": value_home * k / 3, "away": value_away * k / 3})
                norm = progressive.normalize_value_through(part, units, k, *prev)
                prev = (norm.home_goals, norm.away_goals)
            return norm

        totals = collections.Counter()
        for seed in range(2000):
            r = random.Random(seed)
            norm = play(r.uniform(0.2, 2.2), r.uniform(0.2, 2.2), seed)
            totals[norm.home_goals + norm.away_goals] += 1
        check(len(totals) >= 7 and min(totals) <= 2 and max(totals) >= 8, f"the total goals vary with the value created, not a constant: totals seen {sorted(totals)}")
        quiet, busy = play(0.2, 0.2, 5), play(2.2, 2.2, 5)
        check(quiet.home_goals + quiet.away_goals < busy.home_goals + busy.away_goals, "a game with little value created has fewer goals than one with a lot")
        check(play(1.0, 1.0, 11) == play(1.0, 1.0, 11), "the same value and seed always give the same result")
        leader_loses = 0
        for seed in range(3000):
            r = random.Random(seed)
            hv = r.uniform(0.0, 2.5)
            av = r.uniform(0.0, 2.5)
            norm = play(hv, av, seed)
            if VALUE_SCALE * abs(hv - av) >= 2 * VALUE_NOISE_CAP and ((norm.home_goals - norm.away_goals) * (hv - av) <= 0):
                leader_loses += 1
        check(leader_loses == 0, "a side whose expected margin is at least twice the error bound is never left trailing or level")
        prev = (0, 0)
        mono = True
        for k in (1, 2, 3):
            norm = progressive.normalize_value_through(types.SimpleNamespace(offense={"home": 1.4 - 0.4 * k, "away": 0.9}), (0.5, -0.5), k, *prev)
            mono = mono and norm.home_goals >= prev[0] and norm.away_goals >= prev[1]
            prev = (norm.home_goals, norm.away_goals)
        check(mono, "a score never goes down: the ratchet holds even when the net value falls")

        # ---- credit lines from stored shots and plays -----------------------------------------------------------------------------------
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import DraftableAsset, Game, Player, RealTeam, Universe, Week
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit
        import datetime

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            teams = {k: RealTeam(universe_id=nhl.id, name=k, external_ref=k) for k in ("AAA", "BBB")}
            db.add_all(teams.values())
            db.flush()
            week = Week(universe_id=nhl.id, season_year=2026, week_number=1, start_date=datetime.date(2026, 10, 11), end_date=datetime.date(2026, 10, 17))
            db.add(week)
            db.flush()
            game = Game(universe_id=nhl.id, week_id=week.id, home_team_id=teams["AAA"].id, away_team_id=teams["BBB"].id, home_score=0, away_score=0,
                        kickoff_at=datetime.datetime(2026, 10, 14, 19), status="final")
            db.add(game)
            db.flush()
            players, assets = {}, {}
            for name, pos, team in (("Shooter", "C", "AAA"), ("Assist1", "LW", "AAA"), ("Assist2", "D", "AAA"), ("Keeper", "G", "BBB"), ("Hitter", "D", "BBB")):
                pl = Player(universe_id=nhl.id, real_team_id=teams[team].id, full_name=name, position=pos)
                db.add(pl)
                db.flush()
                a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=pl.id)
                db.add(a)
                db.flush()
                players[name], assets[name] = pl, a.id
            kw = dict(game_id=game.id, source="t", shooting_team_id=teams["AAA"].id, goalie_team_id=teams["BBB"].id, is_empty_net=False, period=1)
            goal = Shot(external_shot_id="g", clock="300", shooter_asset_id=assets["Shooter"], goalie_asset_id=assets["Keeper"], pre_shot_xg=0.30, is_goal=True, **kw)
            save = Shot(external_shot_id="s", clock="600", shooter_asset_id=assets["Shooter"], goalie_asset_id=assets["Keeper"], pre_shot_xg=0.10, is_goal=False, **kw)
            db.add_all([goal, save])
            db.flush()
            db.add_all([ShotCredit(shot_id=goal.id, draftable_asset_id=assets["Assist1"], role="primary_assist", credit_points=0.2),
                        ShotCredit(shot_id=goal.id, draftable_asset_id=assets["Assist2"], role="secondary_assist", credit_points=0.1)])
            hit = GameEvent(game_id=game.id, source="t", external_event_id="h", event_type="hit", period=1, game_seconds=700, actor_asset_id=assets["Hitter"])
            take = GameEvent(game_id=game.id, source="t", external_event_id="t", event_type="takeaway", period=2, game_seconds=1500, actor_asset_id=assets["Hitter"])
            block = GameEvent(game_id=game.id, source="t", external_event_id="b", event_type="blocked-shot", period=2, game_seconds=1600, actor_asset_id=assets["Hitter"])
            db.add_all([hit, take, block])
            db.flush()
            db.add(EventCredit(event_id=block.id, draftable_asset_id=assets["Hitter"], role="blocker", credit_points=0.07))
            db.commit()
            lines, attempts = loader._value_lines_for(db, {assets[k]: players[k] for k in assets}, [game.id], tag_games=True)
            by = collections.defaultdict(dict)
            for l in lines:
                by[l.player_name][l.role] = by[l.player_name].get(l.role, 0.0) + l.points
            check(attempts == [], "shots are line groups, so there are no separate attempts to double count")
            pool = 0.30 * ASSIST_SHARE
            check(abs(by["Shooter"]["shooter"] - ((0.30 - pool) + 0.10)) < 1e-9, "the shooter earns the xG of every shot, saved or scored (a goal minus its assists' share)")
            check(abs(by["Assist1"]["primary_assist"] - pool * 2 / 3) < 1e-9 and abs(by["Assist2"]["secondary_assist"] - pool / 3) < 1e-9,
                  "assists share a fixed part of the goal's xG, 2:1 primary to secondary")
            check(abs(by["Keeper"]["goalie"] - 0.10) < 1e-9, "the goalie earns the xG of the shot he saved, and nothing for the goal against (no negative credit)")
            check(abs(by["Hitter"]["hitter"] - HIT_VALUE) < 1e-9 and abs(by["Hitter"]["takeaway"] - TAKEAWAY_VALUE) < 1e-9 and abs(by["Hitter"]["blocker"] - 0.07) < 1e-9,
                  "hits and takeaways create value, and a block keeps the xG it prevented")
            check(len({l.group_id for l in lines if l.role in ("shooter", "primary_assist", "secondary_assist")}) == 2, "each shot is one group: the goal and the saved shot")
        print("value model checks passed")


if __name__ == "__main__":
    main()
