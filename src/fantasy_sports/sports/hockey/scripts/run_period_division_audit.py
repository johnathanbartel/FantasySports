"""
Offline audit of the chronological period division (docs/hockey_spec.md sections 5.2, 6.1 and 9).

  A. The division: each starter's counting game contributes real periods starting at kickoff, +60 and +120 minutes;
     both sides are pooled, sorted by nominal start and cut into three equal chunks (12/12/12 for a full matchup;
     an empty slot gives 11/11/11); the result never depends on input order.
  B. The shared clock and placement function: real and fantasy progress, "Period k - MM:SS left", event placement,
     overtime at the end of the fantasy period holding the player's real P3.
  C. The adapter: earned events land in the fantasy period their real period was assigned to; events with no real
     clock are drawn inside the fantasy periods; the FS-025 monotonic scoreboard validator passes; the division table
     in the provenance ties out to the box score; seeded replays are byte-identical for shuffled inputs.
  D. Best Matchup lock: a standings change after the lock cannot change the selection.
  E. The live clock: a published script that carries real kickoffs is paced by them (not by a compressed playback
     window); events are revealed when the clock reaches them; the game ends when the last real period ends.

Pure and offline: no database, no network. Exits 0 only if every check passes. From the repository root:

    .venv-api/Scripts/python.exe src/fantasy_sports/sports/hockey/scripts/run_period_division_audit.py
"""

from __future__ import annotations

import datetime
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

DAY = datetime.date(2026, 10, 11)      # a Sunday
HOUR = datetime.timedelta(hours=1)


def at(hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(DAY, datetime.time(hour, minute))


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def cg(asset: int, kickoff: datetime.datetime, side: str = "home", game_id: int | None = None):
    from fantasy_sports.sports.hockey.simulator import CountingGame
    return CountingGame(asset, f"P{asset}", side, game_id if game_id is not None else 1000 + asset, kickoff, "vs XXX")


def division(games):
    from fantasy_sports.sports.hockey.simulator.division import Division
    return Division.build(games)


def fantasy_of(div, asset: int) -> list[int]:
    """The fantasy period of the asset's real P1, P2 and P3."""
    return [next(s for s in div.slots if s.asset_id == asset and s.period == n).fantasy_period for n in (1, 2, 3)]


# ---------------------------------------------------------------------------------------------------------------
def audit_division() -> None:
    # 12 starters (6 a side), every kickoff distinct.
    games = [cg(a, at(13) + datetime.timedelta(minutes=7 * a), "home" if a <= 6 else "away") for a in range(1, 13)]
    div = division(games)
    check(div.pooled == 36 and [len(c) for c in div.chunks] == [12, 12, 12], "12 starters with distinct kickoffs: 36 real periods, chunks of 12/12/12")
    starts = [s.start for s in div.slots]
    check(starts == sorted(starts), "the pooled real periods are sorted by nominal start")
    check(all(s.start == next(g.kickoff_at for g in games if g.asset_id == s.asset_id) + (s.period - 1) * HOUR for s in div.slots),
          "a real period starts at kickoff, +60 minutes and +120 minutes")
    check(all(len({s.asset_id for s in c}) <= 12 for c in div.chunks) and sum(len(c) for c in div.chunks) == 36,
          "every real period lands in exactly one fantasy period")

    # 6 starters at 4:00 and 6 at 7:00 on the same day.
    early = [cg(a, at(16), "home") for a in range(1, 7)]
    late = [cg(a, at(19), "away") for a in range(7, 13)]
    div = division(early + late)
    check([fantasy_of(div, a) for a in range(1, 7)] == [[1, 1, 2]] * 6,
          "4:00 players: real P1 and P2 are in fantasy P1, real P3 is in fantasy P2")
    check([fantasy_of(div, a) for a in range(7, 13)] == [[2, 3, 3]] * 6,
          "7:00 players: real P1 is in fantasy P2, real P2 and P3 are in fantasy P3")
    check([len(c) for c in div.chunks] == [12, 12, 12], "...in chunks of 12/12/12 (fantasy P1 = 4:00 P1+P2, P2 = 4:00 P3 + 7:00 P1, P3 = 7:00 P2+P3)")

    # Every starter kicks off together: real Pn is fantasy Pn.
    same = division([cg(a, at(19), "home" if a <= 6 else "away") for a in range(1, 13)])
    check(all(fantasy_of(same, a) == [1, 2, 3] for a in range(1, 13)), "all starters with the same kickoff: real Pn maps to fantasy Pn")

    # One empty slot.
    short = division([cg(a, at(13) + datetime.timedelta(minutes=11 * a), "home" if a <= 6 else "away") for a in range(1, 12)])
    check(short.pooled == 33 and short.chunk_size == 11 and [len(c) for c in short.chunks] == [11, 11, 11],
          "one empty slot: N = 33, chunks of 11/11/11")
    check(division([]).pooled == 0, "an empty matchup has no real periods (and no division to draw from)")

    # A moved surplus game uses its real kickoff, so it sorts first in the target window.
    moved = cg(1, at(13) - datetime.timedelta(days=2), "home", game_id=5)        # played Friday, counted in this window
    div = division([moved] + [cg(a, at(19), "away") for a in range(2, 7)])
    check(div.slots[0].asset_id == 1 and div.slots[0].start == moved.kickoff_at and div.slots[0].fantasy_period == 1,
          "a moved surplus game keeps its real kickoff, so its periods sort first")

    # Determinism: input order never matters, and ties break by game id, then asset id.
    mixed = early + late
    rows = division(mixed).slot_rows()
    rng = random.Random(7)
    for _ in range(25):
        rng.shuffle(mixed)
        assert division(mixed).slot_rows() == rows
    check(True, "shuffled inputs give an identical division (25 shuffles)")
    ties = [cg(5, at(19), game_id=77), cg(2, at(19), game_id=77), cg(9, at(19), game_id=12)]
    order = [(s.game_id, s.asset_id, s.period) for s in division(ties).slots[:3]]
    check(order == [(12, 9, 1), (77, 2, 1), (77, 5, 1)], "equal start times break by game id, then asset id")
    from fantasy_sports.sports.hockey.simulator.division import Division
    check(Division.from_rows(div.slot_rows()).slot_rows() == div.slot_rows(), "a division rebuilds exactly from its stored rows")


# ---------------------------------------------------------------------------------------------------------------
def audit_clock() -> None:
    from fantasy_sports.sports.hockey.simulator.division import Division, is_overtime, real_period_of

    div = division([cg(a, at(16), "home") for a in range(1, 7)] + [cg(a, at(19), "away") for a in range(7, 13)])
    slot = div.chunks[0][0]                                   # a 4:00 real P1
    check(Division.real_progress(slot, at(15, 59)) == 0.0 and Division.real_progress(slot, at(16, 30)) == 0.5
          and Division.real_progress(slot, at(17)) == 1.0 and Division.real_progress(slot, at(23)) == 1.0,
          "real period progress is clamp((t - start) / 60 min, 0, 1)")
    # Fantasy P1 = 4:00 P1 (16:00-17:00) and P2 (17:00-18:00) for six players each: mean progress at 16:30 is 0.25.
    check(abs(div.progress(1, at(16, 30)) - 0.25) < 1e-12 and abs(div.progress(1, at(17, 30)) - 0.75) < 1e-12,
          "fantasy period progress is the mean progress of its real periods")
    check(div.clock_label(at(16, 30)) == "Period 1 — 15:00 left" and div.clock_label(at(16)) == "Period 1 — 20:00 left",
          "clock display: 'Period k - MM:SS left', remaining = 20:00 x (1 - progress)")
    check(div.clock(at(18)) == (2, 20 * 60 - int(round(1200 * div.progress(2, at(18))))) and div.clock(at(18))[0] == 2,
          "once every real period of fantasy P1 is finished the clock moves on to fantasy P2")
    check(div.clock(at(23)) == (3, 0) and div.clock(at(8))[0] == 1, "after the last real period the clock reads Period 3 at 0:00; before it, Period 1")
    check(div.game_second_at(at(16, 30)) == 300, "the clock reading counts from the fantasy game second 300 at 16:30")

    # Different matchups, different clocks, at the same moment.
    other = division([cg(a, at(19), "home" if a <= 6 else "away") for a in range(1, 13)])
    check(div.clock(at(17, 30))[0] == 1 and other.clock(at(17, 30))[0] == 1 and div.clock(at(18, 30))[0] == 2
          and other.clock_label(at(19, 30)) == "Period 1 — 10:00 left" and div.clock(at(19, 30))[0] == 2,
          "each matchup has its own clock: at the same moment two matchups can be in different periods")

    # Event placement.
    check(real_period_of(0) == 1 and real_period_of(1199) == 1 and real_period_of(1200) == 2 and real_period_of(2400) == 3
          and real_period_of(3599) == 3 and real_period_of(3600) == 3 and real_period_of(3700) == 3,
          "real period of a game second: overtime belongs to period 3")
    check(is_overtime(3600) and not is_overtime(3599), "overtime starts at game second 3600")
    # Asset 1 (4:00): real P1 -> fantasy P1. At 10:00 into the real period the wall time is 16:30 -> progress 0.25 -> 300.
    check(div.place(1, 600) == 300, "an event 10:00 into a 4:00 player's real P1 sits at fantasy second 300 (mean progress 0.25)")
    p2_late = div.place(1, 1200 + 600)                        # real P2: wall 17:30 -> fantasy P1 progress 0.75
    check(p2_late == 900, "...and 10:00 into his real P2 sits at fantasy second 900 of fantasy P1")
    check(div.place(1, 2400 + 600) // 1200 == 1 and div.place(1, 2400 + 600) % 1200 not in (0,),
          "his real P3 is in fantasy P2")
    check(div.place(1, 0) >= 1 and div.place(1, 1199) <= 1199, "an event is kept strictly inside its fantasy period (1..1199)")
    check(div.place(1, 3650) == 2 * 1200 - 1 and div.place(7, 3650) == 3 * 1200 - 1,
          "overtime sits at the end of the fantasy period holding that player's real P3 (P2 for 4:00, P3 for 7:00)")
    seconds = [div.place(1, s) for s in range(0, 2400, 5)]          # a 4:00 player's real P1 and P2: both in fantasy P1
    check(all(a <= b for a, b in zip(seconds, seconds[1:])) and seconds[0] < seconds[-1] < 1200,
          "placement never runs backwards across a player's real periods that share a fantasy period")
    inside = [div.place(a, s) for a in range(1, 13) for s in (0, 1, 599, 1199, 1200, 2399, 2400, 3599, 3600, 3900)]
    check(all(1 <= x % 1200 <= 1199 and 0 < x < 3600 for x in inside), "every placed event is strictly inside a fantasy period")
    rng = random.Random(3)
    drawn = [div.draw(rng) for _ in range(5000)]
    check(all(0 < x < 3600 and x % 1200 != 0 for x in drawn) and {x // 1200 for x in drawn} == {0, 1, 2},
          "draws for events with no real clock land inside the fantasy periods, all three of them")
    check(raises_input(lambda: div.place(99, 10)), "an event for a player with no counting game is refused")


def raises_input(fn) -> bool:
    from fantasy_sports.sports.hockey.simulator import InputError
    try:
        fn()
    except InputError:
        return True
    return False


# ---------------------------------------------------------------------------------------------------------------
def line(i: int, asset: int, second: int, points: float = 1.0, role: str = "shooter", pos: str = "W"):
    from fantasy_sports.sports.hockey.simulator import CreditLine
    return CreditLine(f"l{i}", f"g{i}", asset, f"P{asset}", pos, role, points, second, game_id=1000 + asset)


def build(lines_home, lines_away, games, seed=21, starters=True):
    from fantasy_sports.sports.hockey.simulator import HockeyGameInputs, HockeySimulatorAdapter, SideInput
    names = lambda side: tuple(sorted((g.asset_id, g.player_name) for g in games if g.side == side))
    inputs = HockeyGameInputs(
        1, DAY.isoformat(), SideInput(1, "Home", tuple(lines_home), starters=names("home") if starters else ()),
        SideInput(2, "Away", tuple(lines_away), starters=names("away") if starters else ()), counting_games=tuple(games))
    return inputs, HockeySimulatorAdapter().build_script(inputs, seed)


def earned_event(script, asset: int, line_id: str | None = None):
    for e in script.events:
        for c in e.detail.get("credits", []) or []:
            if c["asset_id"] == asset:
                return e
    return None


def audit_adapter() -> None:
    from fantasy_sports.platform.simulator.contract import validate_script
    from fantasy_sports.sports.hockey.simulator import HockeySimulatorAdapter

    early = [cg(a, at(16), "home") for a in range(1, 7)]
    late = [cg(a, at(19), "away") for a in range(7, 13)]
    games = early + late
    # Every starter scores once, at a distinct moment: home (4:00 players) in their real P3; away (7:00 players) in real P1/P2.
    home_lines = [line(a, a, 2500 + 10 * a) for a in range(1, 7)]
    away_lines = [line(a, a, 300 + 10 * a) for a in range(7, 10)] + [line(a, a, 1500 + 10 * a) for a in range(10, 13)]
    inputs, script = build(home_lines, away_lines, games)
    validate_script(script)
    check(True, "the FS-025 contract validator passes")
    ev = script.events
    check(all(a.home_score <= b.home_score and a.away_score <= b.away_score and a.home_shots <= b.home_shots and a.away_shots <= b.away_shots
              for a, b in zip(ev, ev[1:])), "the scoreboard and shot totals only climb (monotonic)")
    div = script.provenance["division"]
    check([p["real_periods"] for p in div["periods"]] == [12, 12, 12], "the stored division table has 12/12/12 real periods")
    check(all(earned_event(script, a).period == 2 for a in range(1, 7)), "4:00 players' real-P3 events land in fantasy P2")
    check(all(earned_event(script, a).period == 2 for a in range(7, 10)), "7:00 players' real-P1 events land in fantasy P2")
    check(all(earned_event(script, a).period == 3 for a in range(10, 13)), "7:00 players' real-P2 events land in fantasy P3")
    inside = [e for e in ev if e.kind not in ("period_start", "period_end", "game_end")]
    check(all(e.game_second % 1200 != 0 and (e.period - 1) * 1200 < e.game_second < e.period * 1200 for e in inside),
          "every event sits strictly inside its fantasy period")

    # The old behaviour (a player's own game clock) is gone: a 7:00 player's real P1 is NOT in fantasy P1.
    p1 = [e for e in ev if e.period == 1 and e.detail.get("credits")]
    check(not p1, "no starter's real P1 or P2 event lands in fantasy P1 just because it happened in his real P1 (no game-clock overlay)")

    # Degenerate: same kickoff -> real Pn is fantasy Pn, through the whole adapter.
    same = [cg(a, at(19), "home" if a <= 6 else "away") for a in range(1, 13)]
    lines_h = [line(a, a, 600 + 1200 * ((a - 1) % 3)) for a in range(1, 7)]
    lines_a = [line(a, a, 600 + 1200 * ((a - 1) % 3)) for a in range(7, 13)]
    _, s_same = build(lines_h, lines_a, same)
    check(all(earned_event(s_same, a).period == ((a - 1) % 3) + 1 for a in range(1, 13)),
          "with a common kickoff each real period n produces events in fantasy period n")

    # Inverted, unattributed and tie-break goals are drawn inside the fantasy periods, over many seeds.
    from fantasy_sports.sports.hockey.simulator import CreditLine
    bad_goalie = [CreditLine("n1", "n1", 1, "P1", "G", "goalie", -1.4, 900, game_id=1001, xg=0.4, is_goal=True)]
    rival = [line(50 + i, 7 + i, 300 + 100 * i) for i in range(4)]
    seen = set()
    for seed in range(60):
        _, s = build(bad_goalie + [line(99, 2, 1000, 0.2)], rival + [line(98, 8, 2000, 1.3)], games, seed=seed)
        validate_script(s)
        assert all(a.home_score <= b.home_score and a.away_score <= b.away_score for a, b in zip(s.events, s.events[1:]))
        for e in s.events:
            if e.detail.get("origin") in ("inverted", "team") or e.detail.get("tiebreak"):
                assert 0 < e.game_second < 3600 and e.game_second % 1200 != 0
                seen.add(e.period)
    check(seen == {1, 2, 3}, "inverted, unattributed and tie-break events are drawn inside the fantasy periods (all three used over 60 seeds)")

    # Overtime: a 4:00 player's OT goal lands at the end of the fantasy period holding his real P3 (fantasy P2).
    ot_lines = [line(1, 1, 3650, 1.0), line(2, 1, 3700, 0.9), line(3, 2, 2400 + 100, 0.4)]
    _, s_ot = build(ot_lines, [line(4, 7, 100, 0.5)], games, seed=4)
    ot_events = [e for e in s_ot.events if any(c["asset_id"] == 1 for c in e.detail.get("credits", []))]
    check(len(ot_events) == 2 and all(e.period == 2 and e.game_second == 2399 for e in ot_events),
          "OT events land at the end of fantasy P2, the period holding that player's real P3")
    check(ot_events[0].seq < ot_events[1].seq, "OT events keep their relative order")
    row = next(r for r in s_ot.provenance["division"]["periods"][1]["rows"] if r["asset_id"] == 1 and r["real_period"] == 3)
    check(row["ot"] is True and abs(row["points"] - 1.9) < 1e-9, "the division table flags overtime and counts OT points in the real P3")

    # Division table ties out to the box score.
    total = {"home": 0.0, "away": 0.0}
    for p in div["periods"]:
        for r in p["rows"]:
            total[r["side"]] += r["points"]
    check(abs(total["home"] - script.provenance["raw"]["home"]) < 1e-9 and abs(total["away"] - script.provenance["raw"]["away"]) < 1e-9
          and abs(total["home"] - sum(p["points"] for p in script.provenance["box_score"]["home"])) < 1e-9,
          "the division table's points tie out to the box score")
    row = div["periods"][0]["rows"][0]
    check(set(row) >= {"player", "fantasy_team", "game_id", "opponent", "game_date", "real_period", "nominal_start", "ot", "points"},
          "each table row has player, fantasy team, real game, real period, nominal start, OT flag and points")
    check([r["nominal_start"] for r in div["periods"][1]["rows"]] == sorted(r["nominal_start"] for r in div["periods"][1]["rows"]),
          "rows read chronologically")

    # A starter with no credit still plays three real periods and is in the division.
    _, s_quiet = build([line(1, 1, 700, 0.8)], [], games)
    check(s_quiet.provenance["division"]["pooled"] == 36 and sum(1 for p in s_quiet.provenance["division"]["periods"] for r in p["rows"] if r["points"] == 0.0) == 35,
          "starters with no credit are still in the division (36 real periods, a zero in their rows)")

    # Determinism: shuffled inputs give an identical script.
    rng = random.Random(11)
    _, base = build(home_lines, away_lines, games, seed=5)
    for _ in range(10):
        h, a, g = list(home_lines), list(away_lines), list(games)
        rng.shuffle(h), rng.shuffle(a), rng.shuffle(g)
        _, again = build(h, a, g, seed=5)
        assert again.digest() == base.digest()
    check(True, "shuffled lines and counting games give a byte-identical script (10 shuffles)")
    _, other = build(home_lines, away_lines, games, seed=6)
    check(other.digest() != base.digest() and other.provenance["division"] == base.provenance["division"],
          "a different seed changes the story, never the division")
    check(base.provenance["adapter_version"] == "1.1" and HockeySimulatorAdapter().sport == "hockey", "the adapter version was bumped with the change")


# ---------------------------------------------------------------------------------------------------------------
def audit_lock() -> None:
    from fantasy_sports.sports.hockey import pipeline as p

    def game(game_id, day, hour, opponent, points=1.0):
        d = DAY + datetime.timedelta(days=day)
        return p.PlayerGame(1, game_id, d, points, kickoff_at=datetime.datetime.combine(d, datetime.time(hour)), opponent=opponent)

    sun, tue = game(100, 0, 19, "BOS"), game(101, 2, 19, "SJS")
    snapshots = {DAY: {"BOS": 12, "SJS": 18}, DAY + datetime.timedelta(days=2): {"BOS": 30, "SJS": 4}}   # Tuesday's table flips them

    def standings(team, lock_at):
        return snapshots[lock_at.date()][team]
    pick = p.resolve_player_week([sun, tue], standings=standings)[0]
    check(pick.game_id == 100 and pick.lock_at == sun.kickoff_at and pick.opponent_points == ((100, 12), (101, 18)),
          "Best Matchup reads the standings dated the lock (Sunday): Boston (12) beats San Jose (18)")
    check(pick.source == "best_matchup", "the selection is labelled Best Matchup")
    reread = p.resolve_player_week([tue, sun], standings=standings)[0]
    check(reread == pick, "a standings change after lock (Tuesday's table flips the order) does not change the selection, however often it is recomputed")
    check(p.resolve_player_week([sun, tue], standings=lambda t, l: {"BOS": 20, "SJS": 20}[t])[0].game_id == 100,
          "equal standings points: the earlier kickoff counts")
    moved = p.resolve_player_week([sun, tue], [p.WindowAssignment(1, 100, 2, sun.kickoff_at - datetime.timedelta(seconds=1))], standings)
    check(moved[0].game_id == 101 and moved[1].game_id == 100 and moved[1].source == "reassigned",
          "a surplus-game move submitted before the same lock is accepted")
    try:
        p.resolve_player_week([sun, tue], [p.WindowAssignment(1, 100, 2, sun.kickoff_at)], standings)
        check(False, "a move at the lock is refused")
    except p.WindowError as e:
        check("before the lock" in str(e), "a move submitted at the lock is refused")


def audit_live_clock() -> None:
    from fantasy_sports.platform.simulator import state as st
    from fantasy_sports.platform.simulator import wallclock
    from fantasy_sports.platform.simulator.contract import SimScript
    from fantasy_sports.sports.hockey.simulator.division import Division

    early = [cg(a, at(16), "home") for a in range(1, 7)]
    late = [cg(a, at(19), "away") for a in range(7, 13)]
    games = early + late
    home_lines = [line(a, a, 2500 + 10 * a) for a in range(1, 7)]
    away_lines = [line(a, a, 300 + 10 * a) for a in range(7, 13)]
    _, script = build(home_lines, away_lines, games)
    clock = wallclock.from_provenance(script.provenance)
    check(clock is not None and clock.timezone == "America/New_York" and clock.segment_seconds == 3600,
          "a script with real kickoffs asks to be paced by its wall clock (America/New_York)")
    check(wallclock.from_provenance(build(home_lines, away_lines, [], starters=False)[1].provenance) is None,
          "a script without kickoff data keeps compressed playback")
    div = Division.build(games)
    check(clock.to_dict() == div.wall_clock().to_dict(), "the stored wall clock is the division's own (one implementation)")

    shape = st.GameShape(3, 1200, 10800, clock)
    started = datetime.datetime(2026, 10, 11, 12, 0)        # when the sim row went live (UTC); irrelevant to the clock

    def utc(hour, minute=0):                                  # an Eastern wall time on the game day, as naive UTC (EDT = UTC-4)
        return datetime.datetime.combine(DAY, datetime.time(hour, minute)) + datetime.timedelta(hours=4)

    el = lambda t: st.public_elapsed(shape, st.LIVE, started, t)
    check(el(utc(15, 59)) == 0 and st.clock_at(shape, el(utc(15, 59))) == (1, 1200), "before the first kickoff the clock reads Period 1 20:00")
    check(el(utc(16, 30)) == 300 and st.clock_at(shape, 300) == (1, 900), "at 4:30 the clock reads Period 1 15:00 left (mean progress 0.25)")
    check(el(utc(18)) == 1200, "at 6:00 every 4:00 real P1 and P2 is over: fantasy P1 is complete")
    check(el(utc(19, 30)) > 1200 and st.clock_at(shape, el(utc(19, 30)))[0] == 2, "at 7:30 the clock is in Period 2")
    check(el(utc(23)) == 3600 and not st.is_due(shape, started, utc(21, 59)) and st.is_due(shape, started, utc(22)),
          "the game is due when the last real period ends (7:00 kickoff + 3 hours = 10:00 PM)")
    check(st.finished_at(shape, started) == utc(22), "finished_at is the last real period's end")
    check(el(utc(16, 30)) == div.game_second_at(datetime.datetime.combine(DAY, datetime.time(16, 30))),
          "the service's clock and the division's clock agree")
    seconds = [e.game_second for e in script.events]
    revealed = [st.count_through(seconds, el(utc(h, m))) for h, m in ((15, 0), (17, 0), (19, 0), (20, 30), (23, 0))]
    check(revealed == sorted(revealed) and revealed[0] == 1 and revealed[-1] == len(script.events),
          "events are revealed as the clock reaches them, none early, all by the end")
    # An event is revealed no earlier than the wall time it happened: a 4:00 player's real-P3 event (18:00-19:00 wall).
    e_p3 = next(e for e in script.events if e.detail.get("credits") and e.detail["credits"][0]["asset_id"] == 1)
    check(el(utc(17, 59)) < e_p3.game_second <= el(utc(18, 6)),
          "a 4:00 player's real-P3 event (about 18:05 on the wall) is not revealed before 18:00 and is revealed by 18:06")
    # Forced finalization (every real game already final) does not need back-dating the start.
    check(st.public_elapsed(shape, st.FINAL, started, utc(15)) == 3600, "a FINAL game shows the whole game whatever the time")


def main() -> None:
    print("A. the division")
    audit_division()
    print("B. the shared clock and placement function")
    audit_clock()
    print("C. the adapter")
    audit_adapter()
    print("D. Best Matchup lock")
    audit_lock()
    print("E. the live clock")
    audit_live_clock()
    print("\nAll period division audits passed.")


if __name__ == "__main__":
    main()
