"""
Offline audit of NHL 3-window scoring (FS-035; docs/hockey_spec.md section 5).

  A. The calendar: Sunday-Saturday weeks, Window 1 = Sun-Mon-Tue, 2 = Wed-Thu, 3 = Fri-Sat.
  B. Window resolution (sports/hockey/pipeline.py, pure): a single game counts; several games resolve to the Best Game
     (highest points; ties go to the earlier game); a surplus game can move to a LATER open window; every rule's
     refusal; and the one-game-per-window invariant over thousands of seeded random player-weeks.
  C. The adapter (sports/hockey/simulator): one game per player per window is enforced at its boundary; window games
     are resolved before scoring; negative goalie credit is inverted; scoreboards are monotonic and NHL-shaped;
     seeded replays are byte-identical.
  D. The database path (loader.build_window_inputs) on a throwaway SQLite database with real pipeline credits.
  E. Real data: tie-outs against user_run.db, opened READ-ONLY. That database stores no game dates (every NHL game is in
     one dateless week row), so real games are placed on a SYNTHETIC calendar here, and said so below; the credit
     totals themselves are real and must tie out exactly.

Exits 0 only if every check passes. Run from the repository root:

    .venv-api/Scripts/python.exe src/fantasy_sports/sports/hockey/scripts/run_window_scoring_audit.py
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import math
import os
import random
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

D = datetime.date


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def raises(fn, exc, fragment: str = "") -> bool:
    try:
        fn()
    except exc as e:
        return fragment in str(e)
    return False


# ---------------------------------------------------------------------------------------------------------------
# A. Calendar
# ---------------------------------------------------------------------------------------------------------------
def audit_calendar() -> None:
    from fantasy_sports.sports.hockey import pipeline as p

    sunday = D(2026, 10, 11)
    check(sunday.weekday() == 6, "2026-10-11 is a Sunday (the fixtures below rely on it)")
    expected = [1, 1, 1, 2, 2, 3, 3]   # Sun Mon Tue Wed Thu Fri Sat
    got = [p.window_of(sunday + datetime.timedelta(days=i)) for i in range(7)]
    check([w for _, w in got] == expected and all(s == sunday for s, _ in got),
          "Sun-Mon-Tue is Window 1, Wed-Thu Window 2, Fri-Sat Window 3, all in the week starting that Sunday")
    check(p.window_of(sunday + datetime.timedelta(days=7)) == (sunday + datetime.timedelta(days=7), 1),
          "the next Sunday starts the next week's Window 1")
    check(p.week_start(sunday + datetime.timedelta(days=6)) == sunday, "Saturday belongs to the week that began the Sunday before")
    check(p.window_dates(sunday, 1) == [sunday, sunday + datetime.timedelta(days=1), sunday + datetime.timedelta(days=2)]
          and len(p.window_dates(sunday, 2)) == 2 and len(p.window_dates(sunday, 3)) == 2,
          "window_dates lists 3, 2 and 2 days")
    every = {(w, d.weekday()) for w in p.WINDOWS for d in p.window_dates(sunday, w)}
    check(len(every) == 7 and sorted(d for _, d in every) == list(range(7)), "the three windows partition the week exactly")
    check(raises(lambda: p.window_dates(sunday, 4), p.WindowError, "1, 2 or 3"), "a fourth window is refused")


# ---------------------------------------------------------------------------------------------------------------
# B. Resolution
# ---------------------------------------------------------------------------------------------------------------
SUN = D(2026, 10, 11)


def pg(asset, game, offset, points):
    from fantasy_sports.sports.hockey.pipeline import PlayerGame
    return PlayerGame(asset, game, SUN + datetime.timedelta(days=offset), points)


def audit_resolution() -> None:
    from fantasy_sports.sports.hockey import pipeline as p

    def week(games, moves=()):
        return p.resolve_player_week(games, [p.WindowAssignment(*m) for m in moves])

    # Credits -> per-game points (exact sums).
    rows = [(1, 10, SUN, 0.1), (1, 10, SUN, 0.2), (1, 10, SUN, 0.3), (1, 11, SUN + datetime.timedelta(days=2), 1.0)]
    games = p.player_game_points(rows)
    check([(g.game_id, g.points) for g in games] == [(10, math.fsum([0.1, 0.2, 0.3])), (11, 1.0)],
          "credit lines sum into per-game points (exact fsum, sorted)")

    # One game per window.
    w = week([pg(1, 100, 0, 0.7), pg(1, 101, 3, 0.2), pg(1, 102, 5, 1.4)])
    check([(s.game_id, s.points, s.source) for s in w] == [(100, 0.7, "single"), (101, 0.2, "single"), (102, 1.4, "single")],
          "one game in each window: each counts, no choice to make")
    w = week([pg(1, 100, 1, 0.7)])
    check([s.source for s in w] == ["single", "none", "none"] and w[1].points == 0.0 and w[1].game_id is None,
          "a window with no game scores nothing")

    # Best Game.
    w = week([pg(1, 100, 0, 0.4), pg(1, 101, 2, 1.1)])
    check(w[0].game_id == 101 and w[0].source == "best_game" and w[0].dropped == (100,) and w[0].candidates == (100, 101),
          "two games in a window: the higher score is the Best Game and the other is dropped")
    w = week([pg(1, 100, 0, 1.1), pg(1, 101, 2, 1.1)])
    check(w[0].game_id == 100, "a tie goes to the earlier game")
    w = week([pg(1, 105, 0, 1.0), pg(1, 100, 0, 1.0)])
    check(w[0].game_id == 100, "a tie on the same day goes to the lower game id")
    w = week([pg(9, 100, 0, -0.8), pg(9, 101, 2, -0.1)])
    check(w[0].game_id == 101 and close(w[0].points, -0.1), "goalie: the least negative game is the Best Game")
    w = week([pg(9, 100, 0, -0.8)])
    check(close(w[0].points, -0.8) and w[0].source == "single", "a lone negative goalie game still counts (it is inverted later, not hidden)")
    w = week([pg(1, 100, 0, 0.5), pg(1, 101, 1, 0.2), pg(1, 102, 2, 0.9)])
    check(w[0].game_id == 102 and w[0].dropped == (100, 101), "three games in a window: still exactly one counts")
    w = week([pg(9, 100, 0, -0.8), dataclasses.replace(pg(9, 101, 2, 0.0), scheduled_only=True)])
    check(w[0].game_id == 100 and close(w[0].points, -0.8) and w[0].dropped == (101,),
          "a game his team played but he earned nothing in never beats his credited game (no dodging a bad night)")
    w = week([dataclasses.replace(pg(9, 100, 0, 0.0), scheduled_only=True), dataclasses.replace(pg(9, 101, 2, 0.0), scheduled_only=True)])
    check(w[0].game_id == 100 and w[0].points == 0.0, "two scheduled-only games: the earlier one stands for the window at 0.0")
    check(raises(lambda: week([pg(1, 100, 0, 1.0), pg(1, 101, 3, 0.5)], [(1, 100, 2)]), p.WindowError, "no surplus"),
          "refused: no surplus game (control for the scheduled-only case below)")
    check(raises(lambda: week([pg(1, 100, 0, 1.0), dataclasses.replace(pg(1, 101, 3, 0.0), scheduled_only=True)]
                              + [pg(1, 102, 2, 0.4)], [(1, 100, 2)]), p.WindowError, "already has a game"),
          "refused: a window where his team plays is occupied even if he earned nothing in it")
    order_a = week([pg(1, 100, 0, 0.4), pg(1, 101, 2, 1.1), pg(1, 102, 3, 0.3)])
    order_b = week([pg(1, 102, 3, 0.3), pg(1, 101, 2, 1.1), pg(1, 100, 0, 0.4)])
    check(order_a == order_b, "the input order never changes the result (deterministic)")

    # Surplus game flex.
    surplus = [pg(1, 100, 0, 1.0), pg(1, 101, 2, 0.5)]            # Sunday + Tuesday, idle Wed-Thu and Fri-Sat
    default = week(surplus)
    moved = week(surplus, [(1, 100, 2)])
    check(default[0].game_id == 100 and default[1].game_id is None and sum(s.points for s in default) == 1.0,
          "default: Best Game (Sunday, 1.0) counts and Window 2 stays empty")
    check(moved[0].game_id == 101 and moved[0].source == "single" and moved[1].game_id == 100 and moved[1].source == "reassigned"
          and close(sum(s.points for s in moved), 1.5), "moving Sunday to idle Window 2: Tuesday keeps Window 1, total 1.5 instead of 1.0")
    check(week(surplus, [(1, 101, 3)])[2].game_id == 101, "a surplus game can go to Window 3 as well")
    thursday = [pg(1, 100, 3, 0.9), pg(1, 101, 4, 0.2)]
    check(week(thursday, [(1, 100, 3)])[2].game_id == 100, "Window 2's surplus game can move on to Window 3")
    three = [pg(1, 100, 0, 1.0), pg(1, 101, 1, 0.6), pg(1, 102, 2, 0.4)]
    both = week(three, [(1, 100, 2), (1, 101, 3)])
    check([s.game_id for s in both] == [102, 100, 101], "three games in Window 1: one stays, one to each later window")

    # Refusals (each message is meant for the manager).
    bad = [
        ("target window already has a game", [pg(1, 100, 0, 1.0), pg(1, 101, 2, 0.5), pg(1, 102, 3, 0.4)], [(1, 100, 2)], "already has a game"),
        ("no surplus game to move", [pg(1, 100, 0, 1.0), pg(1, 101, 3, 0.5)], [(1, 100, 3)], "no surplus game"),
        ("cannot move into Window 1", [pg(1, 100, 3, 1.0), pg(1, 101, 4, 0.5)], [(1, 101, 1)], "Window 2 or Window 3"),
        ("cannot move backwards", [pg(1, 100, 5, 1.0), pg(1, 101, 6, 0.5)], [(1, 100, 2)], "later window"),
        ("cannot stay in the same window", [pg(1, 100, 3, 1.0), pg(1, 101, 4, 0.5)], [(1, 100, 2)], "later window"),
        ("two games into one window", three, [(1, 100, 2), (1, 101, 2)], "one game per window"),
        ("the same game twice", three, [(1, 100, 2), (1, 100, 3)], "already moved"),
        ("a game the player does not have", surplus, [(1, 999, 2)], "not one of this player"),
    ]
    for label, games, moves, fragment in bad:
        check(raises(lambda g=games, m=moves: week(g, m), p.WindowError, fragment), f"refused: {label}")
    check(raises(lambda: p.resolve_windows(p.player_game_points([(1, 10, SUN, 1.0)]), [p.WindowAssignment(2, 10, 2)]), p.WindowError, "no game"),
          "refused: a move for a player with no games")
    check(raises(lambda: p.resolve_player_week([pg(1, 100, 0, 1.0), pg(2, 101, 0, 1.0)]), p.WindowError, "one player"),
          "refused: two players' games in one resolution")
    check(raises(lambda: p.resolve_player_week([pg(1, 100, 0, 1.0), pg(1, 101, 8, 1.0)]), p.WindowError, "one fantasy week"),
          "refused: games from two different weeks")
    try:
        p.assert_one_game_per_window([
            p.WindowScore(1, SUN, 1, 5, 1.0, "single", (5,), ()), p.WindowScore(1, SUN, 2, 5, 1.0, "single", (5,), ()),
            p.WindowScore(1, SUN, 3, None, 0.0, "none", (), ())])
        raise AssertionError("the invariant guard did not fire")
    except p.WindowError:
        check(True, "the invariant guard catches one game counting in two windows")

    # Fuzz: thousands of seeded player-weeks, random games, random valid and invalid moves.
    rng = random.Random(35)
    weeks = games_total = moved_total = multi_windows = 0
    for n in range(4000):
        games = []
        gid = 1000 * (n + 1)
        for day in range(7):
            for _ in range(rng.choice([0, 0, 1, 1, 2] if day in (0, 2, 4) else [0, 0, 0, 1])):
                gid += 1
                games.append(pg(n, gid, day, round(rng.uniform(-1.0, 2.0), 3)))
        if not games:
            continue
        weeks += 1
        games_total += len(games)
        moves = []
        taken: set[int] = set()
        for source in (1, 2):                                       # mostly legal moves: a surplus game to a later open window
            in_window = [g for g in games if g.window[1] == source]
            if len(in_window) >= 2 and rng.random() < 0.7:
                open_targets = [t for t in (2, 3) if t > source and t not in taken and not any(g.window[1] == t for g in games)]
                if open_targets:
                    target = rng.choice(open_targets)
                    taken.add(target)
                    moves.append((n, rng.choice(in_window).game_id, target))
        if rng.random() < 0.15:                                      # ...and some arbitrary, usually illegal, ones
            moves.append((n, rng.choice(games).game_id, rng.choice([1, 2, 3])))
        try:
            scores = p.resolve_player_week(games, [p.WindowAssignment(*m) for m in moves])
        except p.WindowError:
            scores = p.resolve_player_week(games)                      # an invalid set of moves falls back to no moves
            moves = []
        moved_total += len(moves)
        p.assert_one_game_per_window(scores)
        by_id = {g.game_id: g for g in games}
        moved_ids = {m[1] for m in moves}
        for s in scores:
            native = [g for g in games if g.window[1] == s.window and g.game_id not in moved_ids]
            if s.game_id is None:
                assert not native and s.points == 0.0 and s.source == "none"
                continue
            assert s.game_id in by_id and by_id[s.game_id].points == s.points
            if s.source == "reassigned":
                assert s.game_id in moved_ids and not [g for g in games if g.window[1] == s.window]
            else:
                assert s.points == max(g.points for g in native), "Best Game must be the maximum"
                multi_windows += len(native) > 1
        counted = [s.game_id for s in scores if s.game_id is not None]
        dropped = [g for s in scores for g in s.dropped]
        assert len(counted) == len(set(counted)) and not set(counted) & set(dropped), "a game counted twice or counted and dropped"
        assert set(counted) | set(dropped) == set(by_id), "every game is counted or dropped exactly once"
    check(weeks > 3000 and multi_windows > 500 and moved_total > 500,
          f"fuzz: {weeks} random player-weeks ({games_total} games, {multi_windows} Best Game windows, {moved_total} moves): "
          "one game per window, Best Game is the maximum, every game counted or dropped exactly once")


# ---------------------------------------------------------------------------------------------------------------
# C. Adapter
# ---------------------------------------------------------------------------------------------------------------
GAME_DATES = {101: SUN, 102: SUN + datetime.timedelta(days=2), 103: SUN + datetime.timedelta(days=3)}


def L(line_id, asset, name, pos, role, points, game, sec=600, **kw):
    from fantasy_sports.sports.hockey.simulator import CreditLine
    return CreditLine(line_id, f"g:{line_id}", asset, name, pos, role, points, sec, game_id=game, **kw)


def window_inputs(home_lines, away_lines, window=1, home_attempts=(), away_attempts=()):
    from fantasy_sports.sports.hockey.simulator import HockeyGameInputs, SideInput
    return HockeyGameInputs(7, "2026-10-11", SideInput(1, "Home", tuple(home_lines), tuple(home_attempts)),
                            SideInput(2, "Away", tuple(away_lines), tuple(away_attempts)), window_index=window,
                            week_start=SUN.isoformat())


def audit_adapter() -> None:
    from fantasy_sports.sports.hockey import pipeline as p
    from fantasy_sports.sports.hockey.simulator import HockeySimulatorAdapter, InputError, ShotAttempt
    from fantasy_sports.sports.hockey.simulator import windows

    adapter = HockeySimulatorAdapter()
    # Home skater 1 scores 1.0 on Sunday (101) and 0.3 on Tuesday (102); home goalie 9: -0.8 Sunday, +0.4 Tuesday.
    # Away skater 21: 0.6 Sunday, nothing Tuesday; away goalie 29: +0.2 Sunday.
    home = [L("a", 1, "Skater", "W", "shooter", 1.0, 101), L("b", 1, "Skater", "W", "shooter", 0.3, 102, 900),
            L("c", 9, "Goalie", "G", "goalie", -0.8, 101, 300, xg=0.2, is_goal=True),
            L("d", 9, "Goalie", "G", "goalie", 0.4, 102, 900, xg=0.4)]
    away = [L("e", 21, "Rival", "C", "shooter", 0.6, 101, 800), L("f", 29, "Rival G", "G", "goalie", 0.2, 101, 1200, xg=0.2)]
    inputs = window_inputs(home, away)

    check(raises(lambda: adapter.build_script(inputs, 1), InputError, "one game per player per window"),
          "the adapter refuses a game whose player has credit from two real games (one game per window)")
    resolved, report = windows.resolve_window_inputs(inputs, GAME_DATES)
    kept = {l.line_id for l in resolved.home.lines + resolved.away.lines}
    check(kept == {"a", "d", "e", "f"}, "Best Game keeps the skater's Sunday goal (1.0) and the goalie's Tuesday save (+0.4)")
    check({k: v["game_id"] for k, v in report["chosen"].items()} == {"1": 101, "9": 102, "21": 101, "29": 101},
          "the report names the counting game for every player")
    script = adapter.build_window_script(inputs, GAME_DATES, seed=11)
    check(script.provenance["window"]["chosen"]["9"]["source"] == "best_game" and script.provenance["window"]["chosen"]["9"]["dropped"] == [101],
          "script provenance records what was chosen and what was dropped")
    check(close(script.provenance["raw"]["home"], 1.4) and close(script.provenance["raw"]["away"], 0.8),
          "tie-out: raw team totals equal the counting games exactly (home 1.0 + 0.4, away 0.6 + 0.2)")
    check(close(sum(x["points"] for x in script.provenance["box_score"]["home"]), 1.4), "box score ties out to the counting games")
    again = adapter.build_window_script(inputs, GAME_DATES, seed=11)
    check(json.dumps(dataclasses.asdict(script), sort_keys=True, default=str) == json.dumps(dataclasses.asdict(again), sort_keys=True, default=str)
          and script.provenance["inputs_digest"] == again.provenance["inputs_digest"],
          "the same window and seed replay to a byte-identical script")
    other = adapter.build_window_script(window_inputs(home, away, window=1), GAME_DATES, seed=12)
    check(other.provenance["raw"] == script.provenance["raw"], "a different seed changes the story, never the credit that counts")

    # Scoreboard shape: monotonic, NHL-sized, ordered.
    last = script.events[-1]
    check(all(a.home_score <= b.home_score and a.away_score <= b.away_score for a, b in zip(script.events, script.events[1:])),
          "the scoreboard only climbs (monotonic)")
    check(0 <= last.home_score <= 8 and 0 <= last.away_score <= 8 and last.home_score != last.away_score,
          f"the window game normalizes to an NHL-shaped scoreboard ({last.home_score}-{last.away_score})")

    # Negative GSAx inversion on a window's only goalie game.
    sad = window_inputs([L("n", 9, "Goalie", "G", "goalie", -0.9, 101, 400, xg=0.1, is_goal=True)], [L("o", 21, "Rival", "C", "shooter", 0.4, 101)])
    s = adapter.build_window_script(sad, GAME_DATES, seed=3)
    check(close(s.provenance["inverted_value"]["away"], 0.9) and any(e.detail.get("origin") == "inverted" for e in s.events),
          "a goalie's lone bad game (-0.9 GSAx) is inverted into the opponent's chances")
    check(close(s.provenance["offense"]["away"] - s.provenance["offense"]["home"], s.provenance["raw"]["away"] - s.provenance["raw"]["home"]),
          "inversion keeps the net competitive margin exactly")

    # Surplus flex through the adapter: skater 1 moves his Sunday game to Window 2 (idle for him).
    flex_lines = [L("a", 1, "Skater", "W", "shooter", 1.0, 101), L("b", 1, "Skater", "W", "shooter", 0.3, 102, 900)]
    w2 = window_inputs(flex_lines, [L("e", 21, "Rival", "C", "shooter", 0.6, 103, 800)], window=2)
    sched = {1: [101, 102], 21: [103]}
    base = adapter.build_window_script(w2, GAME_DATES, 5, scheduled_games=sched)
    check(base.provenance["window"]["chosen"].get("1") is None and close(base.provenance["raw"]["home"], 0.0),
          "without a move, the skater has nothing in Window 2")
    flexed = adapter.build_window_script(w2, GAME_DATES, 5, [p.WindowAssignment(1, 101, 2)], sched)
    check(flexed.provenance["window"]["chosen"]["1"] == {"game_id": 101, "points": 1.0, "source": "reassigned", "dropped": []}
          and close(flexed.provenance["raw"]["home"], 1.0), "the moved Sunday game scores in Window 2 (reassigned, 1.0)")
    w1 = adapter.build_window_script(window_inputs(flex_lines, [], window=1), GAME_DATES, 5, [p.WindowAssignment(1, 101, 2)], sched)
    check(w1.provenance["window"]["chosen"]["1"]["game_id"] == 102 and close(w1.provenance["raw"]["home"], 0.3),
          "...and Window 1 keeps his other game (Tuesday, 0.3): a game never counts twice")
    check(raises(lambda: adapter.build_window_script(window_inputs(flex_lines, [], window=2), GAME_DATES, 5,
                                                     [p.WindowAssignment(1, 101, 2)], {1: [101, 102, 103]}), InputError, "already has a game"),
          "a game played but unscored in the target window still blocks the move (schedule, not credit, decides)")
    check(raises(lambda: adapter.build_window_script(window_inputs(flex_lines, [], window=1), GAME_DATES, 5, [p.WindowAssignment(1, 102, 1)], sched),
                 InputError, "Window 2 or Window 3"), "an illegal move surfaces as an InputError, scoring nothing")
    no_ids = window_inputs([dataclasses.replace(flex_lines[0], game_id=None)], [])
    check(raises(lambda: adapter.build_window_script(no_ids, GAME_DATES, 1), InputError, "real game id"), "window scoring needs every line's real game id")
    check(raises(lambda: adapter.build_window_script(dataclasses.replace(inputs, window_index=None), GAME_DATES, 1), InputError, "name their window"),
          "window scoring needs a window")
    # A plain (non-window) game is untouched: same digest as before game ids existed.
    from fantasy_sports.sports.hockey.simulator import CreditLine, HockeyGameInputs, SideInput
    plain = HockeyGameInputs(1, "2026-10-14", SideInput(1, "H", (CreditLine("x", "x", 1, "P", "W", "shooter", 1.0),)), SideInput(2, "A"))
    sp = adapter.build_script(plain, 4)
    check(sp.provenance["adapter_version"] == "1.0" and "window" not in sp.provenance, "a plain single-date game builds exactly as before")


# ---------------------------------------------------------------------------------------------------------------
# D. Database path on a throwaway database with real pipeline credits
# ---------------------------------------------------------------------------------------------------------------
def audit_database_path() -> None:
    with tempfile.TemporaryDirectory(prefix="fs035-audit-", ignore_cleanup_errors=True) as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tmp) / 'audit').as_posix()}"
        from fantasy_sports.platform import pecking_order as po
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, Game, League, LeagueUniverse, Player, RealTeam, RosterEntry, Team, Universe, User, Week,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.sports.hockey import pipeline as p
        from fantasy_sports.sports.hockey.models import Shot, ShotCredit
        from fantasy_sports.sports.hockey.pipeline import ShotRecord, process_game
        from fantasy_sports.sports.hockey.simulator import HockeySimulatorAdapter, loader

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            commissioner = User(email="c@example.com", display_name="C")
            db.add(commissioner)
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            bos, tor = RealTeam(universe_id=nhl.id, name="BOS", external_ref="BOS"), RealTeam(universe_id=nhl.id, name="TOR", external_ref="TOR")
            db.add_all([bos, tor])
            db.flush()
            league = League(name="AuditLeague", commissioner_user_id=commissioner.id, size=2, series_length_days=7)
            db.add(league)
            db.flush()
            db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
            fan = [Team(league_id=league.id, name=f"Fan{i + 1}", is_ai_owned=True) for i in range(2)]
            db.add_all(fan)
            db.flush()

            def roster(team, real, prefix):
                ids, assets = {}, {}
                for pos in ("C", "LW", "RW", "D", "D", "G"):
                    pl = Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"{prefix} {pos}{len(ids) + 1}", position=pos)
                    db.add(pl)
                    db.flush()
                    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=pl.id)
                    db.add(a)
                    db.flush()
                    db.add(RosterEntry(team_id=team.id, draftable_asset_id=a.id))
                    ids[len(ids) + 1] = pl.id
                    assets[len(assets) + 1] = (pos, a.id)
                db.flush()
                pids = list(ids.values())
                po.replace_groups(db, team, {"C": [pids[0]], "W": pids[1:3], "D": pids[3:5], "G": [pids[5]]})
                return assets
            hb, ta = roster(fan[0], bos, "Bruin"), roster(fan[1], tor, "Leaf")      # 1 C, 2 W, 3 W, 4 D, 5 D, 6 G
            week = Week(universe_id=nhl.id, season_year=2026, week_number=1, start_date=SUN, end_date=SUN)
            db.add(week)
            db.flush()
            games = {}
            for key, offset, home, away in (("sun", 0, bos, tor), ("tue", 2, tor, bos), ("thu", 4 - 1, bos, tor)):
                g = Game(universe_id=nhl.id, week_id=week.id, home_team_id=home.id, away_team_id=away.id, home_score=1, away_score=1,
                         kickoff_at=datetime.datetime.combine(SUN + datetime.timedelta(days=offset), datetime.time(19)), status="final")
                db.add(g)
                db.flush()
                games[key] = g.id
            # (game, shooting real team, shooter asset, xg, goal, clock)
            hb_a, ta_a = {k: v[1] for k, v in hb.items()}, {k: v[1] for k, v in ta.items()}
            spec = {"sun": [("h", hb_a[2], 0.30, True, 300), ("a", ta_a[1], 0.20, True, 900)],
                    "tue": [("a", ta_a[3], 0.50, True, 400), ("h", hb_a[1], 0.40, False, 800), ("h", hb_a[2], 0.60, True, 1500)],
                    "thu": [("h", hb_a[1], 0.25, True, 600)]}
            n = 0
            all_credits = []
            for key, shots in spec.items():
                records = []
                for side, shooter, xg, goal, clock in shots:
                    n += 1
                    sh = Shot(game_id=games[key], source="t", external_shot_id=f"s{n}", period=clock // 1200 + 1, clock=str(clock),
                              shooting_team_id=bos.id if side == "h" else tor.id, goalie_team_id=tor.id if side == "h" else bos.id,
                              shooter_asset_id=shooter, goalie_asset_id=ta_a[6] if side == "h" else hb_a[6], pre_shot_xg=xg, is_goal=goal, is_empty_net=False)
                    db.add(sh)
                    db.flush()
                    records.append(ShotRecord(sh.id, sh.shooting_team_id, sh.goalie_team_id, shooter, sh.goalie_asset_id, xg, goal, False, clock))
                for c in process_game(records).credits:
                    db.add(ShotCredit(shot_id=c.shot_id, draftable_asset_id=c.asset_id, role=c.role, credit_points=c.points))
                    all_credits.append((c.asset_id, games[key], c.points))
            db.commit()
            teams = [db.get(Team, fan[0].id), db.get(Team, fan[1].id)]

            # Expected from the stored credits, independently of the loader.
            dates = {games["sun"]: SUN, games["tue"]: SUN + datetime.timedelta(days=2), games["thu"]: SUN + datetime.timedelta(days=3)}
            expected = p.resolve_windows(p.player_game_points((a, g, dates[g], pts) for a, g, pts in all_credits))   # credits only
            lw = hb_a[2]
            lw_games = {g: pts for a, g, pts in all_credits if a == lw}
            check(close(lw_games[games["sun"]], 1.0) and close(lw_games[games["tue"]], 1.0),
                  "fixture: Bruin LW scores 1.0 in both Window 1 games, so Best Game is a tie")

            load = loader.build_window_inputs(db, 501, teams[0], teams[1], SUN, 1)
            check(set(load.game_dates) == set(games.values()) and load.inputs.window_index == 1,
                  "the loader gathers the whole week's real games for the window")
            home_assets = {l.asset_id for l in load.inputs.home.lines}
            check(all(l.game_id is not None for l in load.inputs.home.lines + load.inputs.away.lines), "every loaded credit line carries its real game id")
            adapter = HockeySimulatorAdapter()
            script = adapter.build_window_script(load.inputs, load.game_dates, 2026, scheduled_games=load.scheduled_games)
            chosen = script.provenance["window"]["chosen"]
            want = {a: s[0].game_id for (a, _), s in expected.items() if s[0].game_id is not None}
            check(want and all(chosen[str(a)]["game_id"] == g for a, g in want.items()),
                  f"the database path picks the same counting game as the pure resolver for all {len(want)} credited players")
            check(chosen[str(lw)]["game_id"] == games["sun"] and chosen[str(lw)]["dropped"] == [games["tue"]],
                  "a tie between two Window 1 games goes to the earlier game (Sunday)")
            home_total = sum(v["points"] for k, v in chosen.items() if int(k) in {a for _, a in hb.values()})
            check(close(script.provenance["raw"]["home"], home_total, 1e-6), "tie-out: the script's raw home total equals the counting games' credit")
            check(script.events[-1].home_score != script.events[-1].away_score, "the window game has a decisive NHL scoreboard")

            # Window 2 (Thursday): Bruin C scores; flex a Window 1 game of the LW to Window 3 (idle for BOS).
            l2 = loader.build_window_inputs(db, 502, teams[0], teams[1], SUN, 2)
            s2 = adapter.build_window_script(l2.inputs, l2.game_dates, 2026, scheduled_games=l2.scheduled_games)
            check(close(s2.provenance["raw"]["home"], 1.0), "Window 2: the Thursday game is each player's only game, Bruin C scores 1.0")
            move = [p.WindowAssignment(lw, games["sun"], 3)]
            l3 = loader.build_window_inputs(db, 503, teams[0], teams[1], SUN, 3, move)
            s3 = adapter.build_window_script(l3.inputs, l3.game_dates, 2026, move, l3.scheduled_games)
            check({int(k) for k in s3.provenance["window"]["chosen"]} == {lw} and s3.provenance["window"]["chosen"][str(lw)]["source"] == "reassigned"
                  and close(s3.provenance["raw"]["home"], 1.0),
                  "Window 3 has no real games, but the moved Sunday game makes the LW active there and scores 1.0")
            l1 = loader.build_window_inputs(db, 504, teams[0], teams[1], SUN, 1, move)
            s1 = adapter.build_window_script(l1.inputs, l1.game_dates, 2026, move, l1.scheduled_games)
            check(s1.provenance["window"]["chosen"][str(lw)]["game_id"] == games["tue"], "after the move Window 1 counts the LW's Tuesday game instead")
            try:
                bad = [p.WindowAssignment(lw, games["sun"], 2)]
                l = loader.build_window_inputs(db, 505, teams[0], teams[1], SUN, 2, bad)
                adapter.build_window_script(l.inputs, l.game_dates, 2026, bad, l.scheduled_games)
                raise AssertionError("moving a game into a window where the team plays was allowed")
            except Exception as e:
                check(type(e).__name__ == "InputError" and "already has a game" in str(e),
                      "moving a surplus game into Window 2 is refused: the LW's team plays Thursday")


# ---------------------------------------------------------------------------------------------------------------
# E. Real data (read-only)
# ---------------------------------------------------------------------------------------------------------------
def audit_real_data() -> None:
    path = ROOT / "user_run.db"
    if not path.exists():
        print("skip  user_run.db is not present: real-data tie-outs skipped")
        return
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)       # read-only: nothing here can write
    try:
        nhl = con.execute("select id from universes where code='nhl'").fetchone()
        if nhl is None or con.execute("select count(*) from shot_credits").fetchone()[0] == 0:
            print("skip  user_run.db holds no ingested NHL credits: real-data tie-outs skipped")
            return
        nhl = nhl[0]
        n_games = con.execute("select count(*) from games where universe_id=?", (nhl,)).fetchone()[0]
        dated = con.execute("select count(kickoff_at) from games where universe_id=?", (nhl,)).fetchone()[0]
        print(f"info  user_run.db: {n_games} NHL games, {dated} with a kickoff date -> no real calendar exists; using a SYNTHETIC one")
        check(dated == 0 or dated == n_games, "the stored game dates are all present or all absent (no half-dated data)")

        rows = con.execute(
            "select sc.draftable_asset_id, s.game_id, sc.credit_points from shot_credits sc join shots s on s.id=sc.shot_id "
            "join games g on g.id=s.game_id where g.universe_id=? union all "
            "select ec.draftable_asset_id, e.game_id, ec.credit_points from event_credits ec join game_events e on e.id=ec.event_id "
            "join games g on g.id=e.game_id where g.universe_id=?", (nhl, nhl)).fetchall()
        sql_total = con.execute(
            "select (select coalesce(sum(sc.credit_points),0) from shot_credits sc join shots s on s.id=sc.shot_id join games g on g.id=s.game_id where g.universe_id=?)"
            " + (select coalesce(sum(ec.credit_points),0) from event_credits ec join game_events e on e.id=ec.event_id join games g on g.id=e.game_id where g.universe_id=?)",
            (nhl, nhl)).fetchone()[0]
        check(len(rows) > 100000, f"read {len(rows):,} real credit lines across {len({r[1] for r in rows}):,} real games")

        from fantasy_sports.sports.hockey import pipeline as p
        game_ids = sorted(r[0] for r in con.execute("select id from games where universe_id=?", (nhl,)))
        # SYNTHETIC calendar: game ids in order, nine games a day from Sunday 2024-10-06.
        base = D(2024, 10, 6)
        date_of = {gid: base + datetime.timedelta(days=i // 9) for i, gid in enumerate(game_ids)}
        player_games = p.player_game_points((a, g, date_of[g], pts) for a, g, pts in rows if a is not None)
        check(close(math.fsum(g.points for g in player_games), math.fsum(r[2] for r in rows if r[0] is not None), 1e-6),
              "tie-out: per-game player scores sum to the stored credits exactly")
        unnamed = sum(1 for r in rows if r[0] is None)
        check(close(math.fsum(r[2] for r in rows), sql_total, 1e-6) and unnamed == 0, "tie-out: the stored credits equal the database's own SUM()")
        per_asset_sql = defaultdict(float)
        for a, _, pts in rows:
            per_asset_sql[a] += pts
        per_asset_pg = defaultdict(float)
        for g in player_games:
            per_asset_pg[g.asset_id] += g.points
        check(len(per_asset_sql) == len(per_asset_pg) and all(close(per_asset_pg[a], v, 1e-6) for a, v in per_asset_sql.items()),
              f"tie-out: every one of {len(per_asset_sql):,} players' season totals match game by game")

        resolved = p.resolve_windows(player_games)
        multi = counted_points = dropped_points = 0.0
        n_multi = n_windows = 0
        by_key = defaultdict(dict)
        for g in player_games:
            by_key[(g.asset_id, g.window[0])][g.game_id] = g.points
        for (asset, start), scores in resolved.items():
            p.assert_one_game_per_window(scores)
            games = by_key[(asset, start)]
            counted = {s.game_id for s in scores if s.game_id is not None}
            dropped = {g for s in scores for g in s.dropped}
            assert counted | dropped == set(games) and not counted & dropped
            for s in scores:
                n_windows += s.game_id is not None
                if len(s.candidates) > 1:
                    n_multi += 1
                    assert s.points == max(games[c] for c in s.candidates)
            counted_points += sum(games[g] for g in counted)
            dropped_points += sum(games[g] for g in dropped)
        check(close(counted_points + dropped_points, math.fsum(g.points for g in player_games), 1e-6),
              "tie-out: points that count + points dropped by Best Game = all stored credit")
        print(f"info  real credits on the synthetic calendar: {len(resolved):,} player-weeks, {n_windows:,} counted player-windows, "
              f"{n_multi:,} resolved by Best Game, {dropped_points:.1f} points dropped of {counted_points + dropped_points:.1f}")
        check(n_multi > 0, "the real data exercises Best Game (some players have two games in one window)")

        # The real adapter path: two real teams' credited players as the two sides, a window with two games for some.
        from fantasy_sports.platform.pecking_order import GROUP_BY_POSITION
        from fantasy_sports.sports.hockey.simulator import CreditLine, HockeyGameInputs, HockeySimulatorAdapter, SideInput
        player_info = {r[0]: (r[1], r[2]) for r in con.execute(
            "select a.id, p.full_name, p.position from draftable_assets a join players p on p.id=a.player_id where a.universe_id=?", (nhl,))}
        real_team_of = {r[0]: r[1] for r in con.execute(
            "select a.id, p.real_team_id from draftable_assets a join players p on p.id=a.player_id where a.universe_id=?", (nhl,))}
        team_games = defaultdict(set)
        for gid, home, away in con.execute("select id, home_team_id, away_team_id from games where universe_id=?", (nhl,)):
            team_games[home].add(gid)
            team_games[away].add(gid)
        crowded = defaultdict(list)       # (week, window) -> real teams with two games in it
        for team, gids in team_games.items():
            by_window = defaultdict(list)
            for g in gids:
                by_window[p.window_of(date_of[g])].append(g)
            for window, gs in by_window.items():
                if len(gs) >= 2:
                    crowded[window].append((team, sorted(gs)))
        window, entries = next(((w, e) for w, e in sorted(crowded.items()) if len(e) >= 2), (None, None))
        check(window is not None, "found a real window in which two real teams each play twice")
        (team_a, games_a), (team_b, games_b) = entries[0], entries[1]
        wanted = set(games_a) | set(games_b)
        lines_by_side = {"home": [], "away": []}
        for a, g, pts, line_id, role, sec in con.execute(
                "select sc.draftable_asset_id, s.game_id, sc.credit_points, 'sc:'||sc.id, sc.role, 600 from shot_credits sc join shots s on s.id=sc.shot_id "
                f"where s.game_id in ({','.join(str(g) for g in wanted)}) and sc.draftable_asset_id is not null"):
            team = real_team_of.get(a)
            if team in (team_a, team_b) and role in ("shooter", "primary_assist", "secondary_assist", "goalie"):
                name, pos = player_info[a]
                lines_by_side["home" if team == team_a else "away"].append(CreditLine(
                    line_id, f"{line_id}", a, name, GROUP_BY_POSITION.get(pos, pos), role, pts, sec, game_id=g))
        sides = {k: SideInput(1 if k == "home" else 2, k, tuple(v)) for k, v in lines_by_side.items()}
        inputs = HockeyGameInputs(1, p.window_dates(window[0], window[1])[0].isoformat(), sides["home"], sides["away"],
                                  window_index=window[1], week_start=window[0].isoformat())
        script = HockeySimulatorAdapter().build_window_script(inputs, {g: date_of[g] for g in wanted}, 35)
        again = HockeySimulatorAdapter().build_window_script(inputs, {g: date_of[g] for g in wanted}, 35)
        chosen = script.provenance["window"]["chosen"]
        raw = script.provenance["raw"]
        side_total = {s: 0.0 for s in ("home", "away")}
        for k, v in chosen.items():
            side = "home" if real_team_of[int(k)] == team_a else "away"
            side_total[side] += v["points"]
        check(close(raw["home"], side_total["home"], 1e-6) and close(raw["away"], side_total["away"], 1e-6),
              f"real window game: raw totals tie out to each player's counting game (home {raw['home']:.3f}, away {raw['away']:.3f})")
        check(any(v["source"] == "best_game" for v in chosen.values()), "...and it exercised Best Game on real credits")
        check(script.provenance["inputs_digest"] == again.provenance["inputs_digest"] and script.events == again.events,
              "...and replays identically for the same seed")
        last = script.events[-1]
        check(all(a.home_score <= b.home_score and a.away_score <= b.away_score for a, b in zip(script.events, script.events[1:])),
              f"...with a monotonic scoreboard ending {last.home_score}-{last.away_score}")
    finally:
        con.close()


def main() -> None:
    print("A. calendar")
    audit_calendar()
    print("B. window resolution")
    audit_resolution()
    print("C. simulator adapter")
    audit_adapter()
    print("D. database path (throwaway database)")
    audit_database_path()
    print("E. real data (user_run.db, read-only)")
    audit_real_data()
    print("\nAll window scoring audits passed.")


if __name__ == "__main__":
    main()
