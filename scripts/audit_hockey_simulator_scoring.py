"""
Offline audit of the NHL simulator scoring adapter (FS-026).

Three layers, all synthetic and offline (no network, no external data, throwaway SQLite):

  A. Attribution tie-outs: goals total 1.0, the assist share splits 2:1, blocked shots credit prevented xG,
     goalie GSAx = pre-shot xG - goal, empty-net shots excluded, unnamed players tie out.
  B. Adapter invariants: inversion keeps the net margin, normalization preserves ordering outside the bounded
     upset window, scoreboards only climb, seeded replays are byte-identical, edge cases (ties, outliers,
     empty and one-sided games) behave.
  C. End to end: pipeline credits -> stored shot_credits -> pecking-order lineups -> published script ->
     reveal API, with the stored provenance box score tying to the stored credits.

Passing requires exact stated tie-outs and deterministic seeded replays. Run from the repository root:

    .venv-api/Scripts/python.exe scripts/audit_hockey_simulator_scoring.py
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


# --------------------------------------------------------------------------------------------------------
# A. Attribution (sports/hockey/pipeline.py)
# --------------------------------------------------------------------------------------------------------

def audit_attribution() -> None:
    from fantasy_sports.sports.hockey.pipeline import BlockXgEstimator, EventRecord, ShotRecord, process_game

    HOME, AWAY = 1, 2
    def shot(i, team, shooter, goalie, xg, goal, empty=False, sec=100):
        return ShotRecord(i, team, AWAY if team == HOME else HOME, shooter, goalie, xg, goal, empty, sec)
    def goal_event(i, team, scorer, a1=None, a2=None, sec=100):
        return EventRecord(i, "goal", team, sec, f"p{scorer}", f"p{a1}" if a1 else None, f"p{a2}" if a2 else None,
                           scorer, a1, a2)

    # Unassisted goal: the shooter takes the whole 1.0.
    r = process_game([shot(1, HOME, 10, 90, 0.30, True)])
    check(close(r.shooter_credit_by_asset[10], 1.0) and not r.assist_credit_by_asset, "unassisted goal: shooter gets 1.0")

    # One assist: 1 - xG to the shooter, the xG share to the assist.
    r = process_game([shot(1, HOME, 10, 90, 0.30, True)], [goal_event(1, HOME, 10, 11)])
    check(close(r.shooter_credit_by_asset[10], 0.70) and close(r.assist_credit_by_asset[11], 0.30),
          "one assist: shooter 1 - xG, assist the full xG share")

    # Two assists: the xG share splits 2:1 primary:secondary, and the goal still totals exactly 1.0.
    r = process_game([shot(1, HOME, 10, 90, 0.30, True)], [goal_event(1, HOME, 10, 11, 12)])
    check(close(r.shooter_credit_by_asset[10], 0.70) and close(r.assist_credit_by_asset[11], 0.20)
          and close(r.assist_credit_by_asset[12], 0.10), "two assists: xG 0.30 splits 0.20 / 0.10 (2:1)")
    total = sum(c.points for c in r.credits if c.role in ("shooter", "primary_assist", "secondary_assist"))
    check(close(total, 1.0), "an assisted goal's credits sum to exactly 1.0")

    # Goalie: GSAx = pre-shot xG - goal, per shot faced; the goal against is negative.
    shots = [shot(1, HOME, 10, 90, 0.10, False), shot(2, HOME, 10, 90, 0.30, False), shot(3, HOME, 10, 90, 0.40, True)]
    r = process_game(shots)
    check(close(r.gsax_by_asset[90], 0.10 + 0.30 + (0.40 - 1.0)), "goalie GSAx = sum(xG) - goals allowed (-0.20)")
    goalie_goal = [c for c in r.credits if c.role == "goalie" and c.shot_id == 3][0]
    check(close(goalie_goal.points, -0.60), "a goal against is a negative goalie credit of xG - 1")

    # Empty-net shots never touch the goalie, but the goal still credits the shooter.
    shots = [shot(1, HOME, 10, None, 0.60, True, empty=True), shot(2, HOME, 10, 90, 0.20, False)]
    r = process_game(shots)
    check(close(r.gsax_by_asset[90], 0.20) and r.empty_net_goals_by_team[AWAY] == 1
          and close(r.shooter_credit_by_asset[10], 1.0), "empty-net goal: excluded from GSAx, shooter still credited")
    check(all(c.role != "goalie" or c.shot_id != 1 for c in r.credits), "no goalie credit row exists for an empty-net shot")

    # Blocked shots credit the prevented xG (extra credit on top of the goal total).
    est = BlockXgEstimator({7: 0.04}, {7: 100})
    ev = EventRecord(5, "blocked-shot", HOME, 200, "p10", "p50", None, 10, 50, None, x=70.0, y=0.0)
    r = process_game([], [ev], est, teams=(HOME, AWAY))
    check(close(r.block_credit_by_asset[10], 0.04) and len(r.block_credit_by_asset) == 1,
          "a blocked shot credits the estimated xG of the attempt (0.04)")

    # Whole-game tie-outs on a mixed game, unnamed players included.
    shots = [shot(1, HOME, 10, 91, 0.25, True), shot(2, HOME, None, 91, 0.15, True), shot(3, AWAY, 20, 90, 0.35, True),
             shot(4, AWAY, 21, 90, 0.05, False), shot(5, HOME, 11, 91, 0.50, False), shot(6, HOME, 10, None, 0.10, True),
             shot(7, AWAY, 20, 90, 0.9, True, empty=True)]
    r = process_game(shots, [goal_event(1, HOME, 10, 11, 12, sec=100)])
    skater = sum(c.points for c in r.credits if c.role != "goalie") + r.unnamed_shooter_points + r.unnamed_assist_points
    check(close(skater, sum(1 for s in shots if s.is_goal)), "shooter + assist credit (incl. unnamed) equals the goals scored")
    goalie = sum(c.points for c in r.credits if c.role == "goalie") + r.unnamed_goalie_points
    expected = sum(s.pre_shot_xg - (1.0 if s.is_goal else 0.0) for s in shots if not s.is_empty_net)
    check(close(goalie, expected), "goalie credit (incl. unnamed) equals xG faced minus goals allowed, empty net excluded")


# --------------------------------------------------------------------------------------------------------
# B. Adapter (sports/hockey/simulator)
# --------------------------------------------------------------------------------------------------------

def make_inputs(home_lines, away_lines, home_attempts=(), away_attempts=(), game_id=1):
    from fantasy_sports.sports.hockey.simulator import HockeyGameInputs, SideInput
    return HockeyGameInputs(game_id, "2026-10-14", SideInput(1, "Home", tuple(home_lines), tuple(home_attempts)),
                            SideInput(2, "Away", tuple(away_lines), tuple(away_attempts)))


def line(line_id, group, asset, name, pos, role, points, sec=600, **kw):
    from fantasy_sports.sports.hockey.simulator import CreditLine
    return CreditLine(line_id, group, asset, name, pos, role, points, sec, **kw)


def offense_lines(prefix, base_asset, total):
    """A side whose positive credit is `total` goal-equivalents, as whole goals plus one fractional chance."""
    lines, k, remaining = [], 0, total
    while remaining > 1e-12:
        value = min(1.0, remaining)
        k += 1
        gid = f"{prefix}g{k}"
        if value > 0.3:
            xg = round(value * 0.3, 6)
            lines += [line(f"{gid}s", gid, base_asset, f"{prefix}Sniper", "W", "shooter", value - xg, sec=300 * k),
                      line(f"{gid}a", gid, base_asset + 1, f"{prefix}Playmaker", "C", "primary_assist", xg, sec=300 * k)]
        else:
            lines.append(line(f"{gid}s", gid, base_asset, f"{prefix}Sniper", "W", "shooter", value, sec=300 * k))
        remaining -= value
    return lines


def audit_adapter() -> None:
    from fantasy_sports.platform.simulator.contract import validate_script
    from fantasy_sports.sports.hockey.simulator import HockeySimulatorAdapter, InputError, TieOutError
    from fantasy_sports.sports.hockey.simulator import model
    from fantasy_sports.sports.hockey.simulator.attribution import build_ledger, tie_out_ledger
    from fantasy_sports.sports.hockey.simulator.normalize import (
        check_ordering, derive_rng, expected_margin, normalize, truncated_normal,
    )

    adapter = HockeySimulatorAdapter()

    # ---- Ledger: inversion keeps the net margin ----------------------------------------------------------
    home = offense_lines("H", 100, 3.4) + [line("hb", "hb", 110, "HBlocker", "D", "blocker", 0.05),
                                           line("hs1", "hs1", 120, "HGoalie", "G", "goalie", 0.30),
                                           line("hga", "hga", 120, "HGoalie", "G", "goalie", -0.80)]
    away = offense_lines("A", 200, 2.0) + [line("ags", "ags", 220, "AGoalie", "G", "goalie", -0.45)]
    inputs = make_inputs(home, away)
    ledger = build_ledger(inputs)
    tie_out_ledger(inputs, ledger)
    check(close(ledger.raw["home"], 3.4 + 0.05 + 0.30 - 0.80) and close(ledger.raw["away"], 2.0 - 0.45),
          "team totals S are the signed sums of their lines")
    check(close(ledger.offense["home"], (3.4 + 0.05 + 0.30) + 0.45) and close(ledger.offense["away"], 2.0 + 0.80),
          "negative goalie credit is inverted into the opponent's offense (0.80 -> away, 0.45 -> home)")
    check(close(ledger.offense_margin, ledger.raw_margin), "inversion leaves the net margin unchanged")
    check(close(sum(ledger.offense.values()), ledger.positive["home"] + ledger.positive["away"]
                + ledger.negative["home"] + ledger.negative["away"]), "inversion conserves total value")
    inv = [c for c in ledger.items["away"] if c.kind == "inverted"]
    check(len(inv) == 1 and close(inv[0].value, 0.80) and inv[0].source.player_name == "HGoalie",
          "the inverted item remembers the goalie it came from")

    # ---- Input rejection ---------------------------------------------------------------------------------
    def rejects(bad_inputs, fragment, label):
        try:
            adapter.build_script(bad_inputs, 1)
        except InputError as e:
            check(fragment in str(e), f"{label} rejected ({e})")
            return
        raise AssertionError(f"{label} was accepted")
    rejects(make_inputs([line("x", "x", 1, "N", "W", "shooter", -0.1)], []), "only goalie credit", "negative skater credit")
    rejects(make_inputs([line("x", "x", 1, "G", "G", "goalie", 0.2, empty_net=True)], []), "empty-net", "goalie credit on an empty-net shot")
    rejects(make_inputs([line("x", "x", 1, "N", "W", "shooter", float("nan"))], []), "finite", "NaN credit")
    rejects(make_inputs([line("x", "x", 1, "N", "W", "shooter", 0.5), line("x", "y", 2, "M", "W", "shooter", 0.5)], []),
            "duplicate", "duplicate line id")
    rejects(make_inputs([line("x", "x", 1, "N", "W", "shooter", 0.5)], [line("y", "y", 1, "N", "W", "shooter", 0.5)]),
            "both sides", "one player on both sides")
    rejects(make_inputs([line("x", "x", 1, "N", "W", "hitter", 0.5)], []), "unknown role", "unsupported role")

    # ---- Reproducibility ---------------------------------------------------------------------------------
    s1, s2 = adapter.build_script(inputs, 42), adapter.build_script(inputs, 42)
    check(s1 == s2 and s1.digest() == s2.digest(), "same inputs + seed give a byte-identical script")
    check(adapter.build_script(inputs, 43).digest() != s1.digest(), "a different seed gives a different script")
    shuffled = make_inputs(list(reversed(home)), list(reversed(away)))
    check(adapter.build_script(shuffled, 42).digest() != "" and
          adapter.build_script(shuffled, 42).events[-1].headline == s1.events[-1].headline,
          "line order in the input does not change the scoreboard")
    other_game = dataclasses.replace(inputs, daily_game_id=2)
    check(adapter.build_script(other_game, 42).digest() != s1.digest(), "the stream is keyed by game as well as seed")
    check(HockeySimulatorAdapter(600).build_script(inputs, 42).events == s1.events, "the real-time window does not change the story")
    check(s1.provenance["seed"] == 42 and s1.provenance["adapter"] == "hockey-xg"
          and s1.provenance["inputs_digest"] == adapter.build_script(shuffled, 42).provenance["inputs_digest"],
          "provenance records the seed and a stable input digest")
    rng_a, rng_b = derive_rng(7, 3, "noise"), derive_rng(7, 3, "noise")
    check([rng_a.random() for _ in range(5)] == [rng_b.random() for _ in range(5)], "derived random streams replay exactly")
    check(derive_rng(7, 3, "noise").random() != derive_rng(7, 3, "story").random(), "noise and story streams are independent")

    # ---- Script shape and tie-outs ----------------------------------------------------------------------
    validate_script(s1)
    scores = [(e.home_score, e.away_score) for e in s1.events]
    check(all(b[0] >= a[0] and b[1] >= a[1] for a, b in zip(scores, scores[1:])), "displayed scores never decrease")
    shots = [(e.home_shots, e.away_shots) for e in s1.events]
    check(all(b[0] >= a[0] and b[1] >= a[1] for a, b in zip(shots, shots[1:])), "shot counters never decrease")
    final = s1.events[-1]
    check(final.kind == "game_end" and sum(1 for e in s1.events if e.kind == "score" and e.team_side == "home") == final.home_score
          and sum(1 for e in s1.events if e.kind == "score" and e.team_side == "away") == final.away_score,
          "goal events add up to the final scoreboard")
    check(final.home_shots >= final.home_score and final.away_shots >= final.away_score, "a team never has fewer shots than goals")
    check(s1.provenance["raw"]["margin"] == round(ledger.raw_margin, 6) and
          close(s1.provenance["offense"]["home"] - s1.provenance["offense"]["away"], ledger.raw_margin, 1e-5),
          "provenance: offense margin equals raw margin")
    box = s1.provenance["box_score"]
    check(close(sum(p["points"] for p in box["home"]), ledger.raw["home"], 1e-5)
          and close(sum(p["points"] for p in box["away"]), ledger.raw["away"], 1e-5),
          "box score player points tie out to the team totals")
    inverted_goals = [e for e in s1.events if e.detail.get("origin") == "inverted" and e.kind == "score"]
    check(all("caused_by" in e.detail and e.detail["caused_by"]["points"] < 0 for e in inverted_goals),
          "every inverted goal names the negative credit it came from")
    check(any(e.kind in ("save",) for e in s1.events) and any(e.detail.get("origin") == "earned" for e in s1.events),
          "earned credit appears in the ticker (goalie saves, goals)")

    # ---- Normalization properties -----------------------------------------------------------------------
    check(close(expected_margin(0, 0), 0.0) and expected_margin(5, 1) > expected_margin(3, 1) > expected_margin(2, 1) > 0,
          "expected margin grows with the offense gap and is zero for a level game")
    check(close(expected_margin(4, 1), -expected_margin(1, 4)), "expected margin is antisymmetric")
    check(close(expected_margin(5.6, 3.4), 6.0 * 2.2 / (9.0 + 2.0)), "spec example 5.60 vs 3.40 gives a 1.2-goal expected margin")
    rng = random.Random(1)
    draws = [truncated_normal(rng, model.NOISE_SIGMA, model.NOISE_CAP) for _ in range(20000)]
    check(max(abs(x) for x in draws) <= model.NOISE_CAP, "the error term never exceeds its bound")
    check(abs(sum(draws) / len(draws)) < 0.02 and 0.35 < (sum(x * x for x in draws) / len(draws)) ** 0.5 < 0.5,
          "the error term is centred, with the truncated standard deviation expected for sigma 0.5 / cap 1.0")
    check(truncated_normal(random.Random(1), 0.0, 1.0) == 0.0, "sigma 0 disables the error term")

    def result(o_home, o_away, seed, game_id=1):
        i = make_inputs(offense_lines("H", 100, o_home), offense_lines("A", 200, o_away), game_id=game_id)
        led = build_ledger(i)
        return led, normalize(led, derive_rng(seed, game_id, "noise"))

    upsets_inside = games_inside = 0
    outside_winners = []
    for o_home, o_away in [(5.6, 3.4), (4.0, 1.0), (3.0, 0.5), (2.0, 1.4), (1.8, 1.5), (1.5, 1.5), (0.6, 2.4), (0.0, 3.0), (3.4, 3.4),
                           (2.5, 1.0), (1.2, 2.7), (6.0, 0.2), (0.1, 0.1), (0.0, 0.0)]:
        for seed in range(300):
            led, n = result(o_home, o_away, seed)
            check_ordering(n)
            leader = "home" if o_home > o_away else "away" if o_away > o_home else None
            winner = "home" if n.home_goals > n.away_goals else "away" if n.away_goals > n.home_goals else None
            if leader and abs(n.expected_margin) >= model.NOISE_CAP:
                outside_winners.append(winner == leader)
            if leader and abs(n.expected_margin) < model.NOISE_CAP:
                games_inside += 1
                upsets_inside += winner not in (leader, None)
            assert n.home_goals >= 0 and n.away_goals >= 0 and n.home_goals + n.away_goals <= 14, "goals stay realistic"
            assert winner is not None, "no level scoreboards"
    check(all(outside_winners) and len(outside_winners) > 1000,
          f"outside the 1-goal upset window the raw leader always wins ({len(outside_winners)} seeded games)")
    check(0 < upsets_inside < games_inside * 0.45,
          f"inside the window upsets happen but stay bounded ({upsets_inside} of {games_inside} close games)")

    # For a fixed seed, more offense never hurts the margin (same error term, monotone model).
    for seed in range(100):
        margins = []
        for o_home in [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.5, 6.0]:
            _, n = result(o_home, 1.5, seed)
            margins.append(n.home_goals - n.away_goals)
        assert margins == sorted(margins), f"margin not monotone in offense for seed {seed}: {margins}"
    check(True, "scoreboard margin is monotone in the raw margin for any fixed seed (100 seeds)")

    # Ties: level offense is decided by the seeded error term and never leaves a level scoreboard.
    winners = set()
    for seed in range(200):
        led, n = result(2.0, 2.0, seed)
        assert n.home_goals != n.away_goals, "level raw game produced a level scoreboard"
        winners.add("home" if n.home_goals > n.away_goals else "away")
    check(winners == {"home", "away"}, "an exactly level raw game is split by the seeded error term (both outcomes occur)")
    led, n = result(2.0, 2.0, 5)
    check(not n.upset, "a level raw game has no leader, so it cannot be an upset")
    tie_seeds = [s for s in range(300) if result(1.5, 1.5, s)[1].tiebreak_side]
    check(len(tie_seeds) > 0, f"rounded ties occur and are resolved by a deciding goal ({len(tie_seeds)} of 300 seeds)")
    zero_noise = normalize(build_ledger(make_inputs(offense_lines("H", 100, 3.0), offense_lines("A", 200, 3.0))),
                           random.Random(1), sigma=0.0)
    check(zero_noise.home_goals == zero_noise.away_goals and zero_noise.tiebreak_side is None,
          "with the error term off, a level game stays level (no invented winner)")

    # Outliers and degenerate games.
    big = adapter.build_script(make_inputs(offense_lines("H", 100, 40.0), offense_lines("A", 200, 0.2)), 9)
    check(big.events[-1].home_score <= 7 and big.events[-1].away_score <= 1 and big.events[-1].home_score > big.events[-1].away_score
          and len(big.events) < 200,
          "an outlier blowout stays NHL-shaped (<= 7 goals) and does not flood the feed")
    empty = adapter.build_script(make_inputs([], []), 3)
    check(empty.events[0].kind == "period_start" and empty.events[-1].kind == "game_end"
          and abs(empty.events[-1].home_score - empty.events[-1].away_score) <= 1,
          "a game with no credit at all still produces a valid scoreboard")
    one_sided = adapter.build_script(make_inputs([], offense_lines("A", 200, 2.5)), 3)
    check(one_sided.events[-1].away_score > one_sided.events[-1].home_score, "one-sided credit wins the scoreboard")
    only_negative = adapter.build_script(make_inputs([line("g", "g", 1, "G1", "G", "goalie", -1.5)], []), 3)
    check(only_negative.events[-1].away_score >= only_negative.events[-1].home_score
          and any(e.detail.get("origin") == "inverted" for e in only_negative.events),
          "a goalie's bad night is inverted into the other side's chances")
    many = [line(f"s{i}", f"s{i}", 300 + i, f"Sav{i}", "G", "goalie", 0.01 + i / 10000) for i in range(300)]
    flooded = adapter.build_script(make_inputs(many, []), 4)
    check(sum(1 for e in flooded.events if e.kind == "save") == model.MAX_TEXTURE_EVENTS_PER_SIDE,
          "texture events are capped per side (highest value first)")
    validate_script(flooded)

    # The tie-out guard actually fires.
    try:
        broken = dataclasses.replace(s1, events=s1.events[:-1] + (dataclasses.replace(s1.events[-1], home_score=s1.events[-1].home_score + 1),))
        from fantasy_sports.sports.hockey.simulator.adapter import tie_out_script
        tie_out_script(ledger, normalize(ledger, derive_rng(42, 1, "noise")), broken)
        raise AssertionError("a tampered script passed the tie-out")
    except TieOutError:
        check(True, "post-build tie-out catches a script that disagrees with the normalized scoreboard")

    # Average scoring environment across many ledgers (informational, bounds only).
    totals = []
    for seed in range(400):
        r1 = random.Random(seed)
        o_home, o_away = r1.uniform(0.2, 4.0), r1.uniform(0.2, 4.0)
        _, n = result(o_home, o_away, seed)
        totals.append(n.home_goals + n.away_goals)
    mean = sum(totals) / len(totals)
    print(f"info  mean combined goals over 400 random ledgers: {mean:.2f} (target ~{model.TOTAL_GOALS})")
    check(5.5 <= mean <= 7.0, "the long-run scoring environment stays near the 6.0-goal target")


# --------------------------------------------------------------------------------------------------------
# C. End to end through the database and the reveal API
# --------------------------------------------------------------------------------------------------------

def audit_end_to_end() -> None:
    with tempfile.TemporaryDirectory(prefix="fs026-audit-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'audit').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import pecking_order as po
        from fantasy_sports.platform import series_schedule
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, Game, League, LeagueUniverse, Player, RealTeam, RosterEntry, SeriesDailyGame, Team,
            Universe, User, Week,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.platform.simulator import service
        from fantasy_sports.sports.hockey.models import Shot, ShotCredit
        from fantasy_sports.sports.hockey.pipeline import ShotRecord, process_game
        from fantasy_sports.sports.hockey.simulator import loader

        Base.metadata.create_all(bind=engine)
        date = datetime.date(2026, 10, 14)
        with SessionLocal() as db:
            commissioner = User(email="comm@example.com", display_name="Commissioner")
            viewer = User(email="viewer@example.com", display_name="Viewer")
            db.add_all([commissioner, viewer])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            bos, tor = RealTeam(universe_id=nhl.id, name="BOS", external_ref="BOS"), RealTeam(universe_id=nhl.id, name="TOR", external_ref="TOR")
            db.add_all([bos, tor])
            db.flush()
            league = League(name="AuditLeague", commissioner_user_id=commissioner.id, size=4, series_length_days=5)
            db.add(league)
            db.flush()
            db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
            teams = [Team(league_id=league.id, name=f"Fan{i + 1}", is_ai_owned=True) for i in range(4)]
            db.add_all(teams)
            db.flush()

            def roster(team, real, prefix):
                ids = {}
                for pos, n in (("C", 1), ("LW", 1), ("RW", 1), ("D", 2), ("G", 1)):
                    for k in range(n):
                        p = Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"{prefix} {pos}{k + 1}", position=pos)
                        db.add(p)
                        db.flush()
                        a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
                        db.add(a)
                        db.flush()
                        db.add(RosterEntry(team_id=team.id, draftable_asset_id=a.id))
                        ids.setdefault(pos, []).append(p.id)
                        ids.setdefault("asset", {})[p.id] = a.id
                db.flush()
                po.replace_groups(db, team, {"C": ids["C"], "W": ids["LW"] + ids["RW"], "D": ids["D"], "G": ids["G"]})
                return ids["asset"]

            assets_home = roster(teams[0], bos, "Bruin")     # fantasy team 1 = the BOS skaters
            assets_away = roster(teams[1], tor, "Leaf")      # fantasy team 2 = the TOR skaters
            home_a, away_a = list(assets_home.values()), list(assets_away.values())
            roles = {"C": home_a[0], "LW": home_a[1], "RW": home_a[2], "D1": home_a[3], "D2": home_a[4], "G": home_a[5]}
            aroles = {"C": away_a[0], "LW": away_a[1], "RW": away_a[2], "D1": away_a[3], "D2": away_a[4], "G": away_a[5]}

            # Real shots -> the pipeline's credits -> stored shot_credits, exactly as run_game_validation --write does.
            spec = [  # (shooting side, shooter, xg, goal, empty net, clock)
                ("h", roles["LW"], 0.30, True, False, 300), ("h", roles["C"], 0.12, False, False, 500),
                ("h", roles["RW"], 0.45, True, False, 1500), ("a", aroles["C"], 0.20, True, False, 900),
                ("a", aroles["LW"], 0.08, False, False, 1800), ("a", aroles["RW"], 0.55, True, False, 2600),
                ("h", roles["D1"], 0.10, False, False, 2900), ("a", None, 0.70, True, True, 3500),
            ]
            records, stored = [], []
            for n, (side, shooter, xg, goal, empty, clock) in enumerate(spec, start=1):
                shooting, goalie_team = (bos.id, tor.id) if side == "h" else (tor.id, bos.id)
                goalie = None if empty else (aroles["G"] if side == "h" else roles["G"])
                shot = Shot(game_id=1, source="moneypuck", external_shot_id=f"s{n}", period=clock // 1200 + 1, clock=str(clock),
                            shooting_team_id=shooting, goalie_team_id=goalie_team, shooter_asset_id=shooter,
                            goalie_asset_id=goalie, pre_shot_xg=xg, is_goal=goal, is_empty_net=empty)
                db.add(shot)
                db.flush()
                records.append(ShotRecord(shot.id, shooting, goalie_team, shooter, goalie, xg, goal, empty, clock))
            result = process_game(records)
            for c in result.credits:
                db.add(ShotCredit(shot_id=c.shot_id, draftable_asset_id=c.asset_id, role=c.role, credit_points=c.points))
            db.flush()
            series_schedule.generate_schedule(db, league, date)
            game = (db.query(SeriesDailyGame).filter(SeriesDailyGame.home_team_id == teams[0].id,
                                                     SeriesDailyGame.away_team_id == teams[1].id)
                    .order_by(SeriesDailyGame.calendar_date).first())
            date = game.calendar_date  # the day Fan1 (BOS skaters) hosts Fan2 (TOR skaters)
            week = Week(universe_id=nhl.id, season_year=2026, week_number=1, start_date=date, end_date=date)
            db.add(week)
            db.flush()
            db.add(Game(universe_id=nhl.id, week_id=week.id, home_team_id=bos.id, away_team_id=tor.id,
                        home_score=3, away_score=2, kickoff_at=datetime.datetime.combine(date, datetime.time(19, 0)),
                        status="final"))
            db.commit()
            game_id, series_id = game.id, game.series_id
            stored_total = {"home": sum(c.credit_points for c in db.query(ShotCredit).filter(ShotCredit.draftable_asset_id.in_(home_a))),
                            "away": sum(c.credit_points for c in db.query(ShotCredit).filter(ShotCredit.draftable_asset_id.in_(away_a)))}
            tokens = {"commissioner": create_session(db, commissioner.id).token, "viewer": create_session(db, viewer.id).token}
        auth = {k: {"Authorization": f"Bearer {v}"} for k, v in tokens.items()}

        check(close(sum(c.points for c in result.credits if c.role != "goalie") + result.unnamed_shooter_points,
                    sum(1 for s in records if s.is_goal)), "stored skater credits tie out to the real goals (3 + 2 + empty net)")

        with SessionLocal() as db:
            inputs = loader.build_inputs(db, db.get(SeriesDailyGame, game_id))
        home_roles = {l.role for l in inputs.home.lines}
        check({"shooter", "goalie"} <= home_roles and len(inputs.home.lines) >= 4 and inputs.home.attempts,
              "the loader reads the starters' stored credits and shot attempts")
        check(all(l.role != "goalie" or not l.empty_net for l in inputs.home.lines + inputs.away.lines),
              "no empty-net shot reaches a goalie")
        check(close(sum(l.points for l in inputs.home.lines), stored_total["home"])
              and close(sum(l.points for l in inputs.away.lines), stored_total["away"]),
              "loaded line totals equal the stored credits of each lineup (tie-out)")

        with SessionLocal() as db:
            sim = loader.publish_game(db, db.get(SeriesDailyGame, game_id))
            db.commit()
            digest1, prov = sim.script_digest, dict(sim.provenance)
        with SessionLocal() as db:
            again = loader.publish_game(db, db.get(SeriesDailyGame, game_id))
            check(again.script_digest == digest1, "republishing the same game replays the identical script (default seed)")
        check(prov["box_score"]["home"] and close(sum(p["points"] for p in prov["box_score"]["home"]), stored_total["home"], 1e-5),
              "provenance box score ties out to the stored credits")
        check(prov["raw"]["margin"] == round(stored_total["home"] - stored_total["away"], 6), "provenance raw margin matches the stored credits")

        # Reveal it through the FS-025 API and confirm the final matches the normalized result.
        t0 = datetime.datetime.combine(date, datetime.time(19, 0))
        clock = {"now": t0}
        service.utcnow = lambda: clock["now"]
        client = TestClient(app)
        url = f"/simulator/games/{game_id}"
        check(client.post(f"{url}/start", headers=auth["commissioner"]).json()["status"] == "LIVE", "the published game starts")
        clock["now"] = t0 + datetime.timedelta(seconds=loader.DEFAULT_WINDOW_SECONDS + 1)
        client.post(f"{url}/watch", headers=auth["viewer"])
        state = client.post(f"{url}/skip", headers=auth["viewer"]).json()
        final = client.get(f"{url}/final", headers=auth["viewer"]).json()
        check(state["status"] == "FINAL" and (final["home_score"], final["away_score"]) == (
            prov["normalization"]["home_goals"], prov["normalization"]["away_goals"]),
            f"revealed final {final['home_score']}-{final['away_score']} equals the normalized scoreboard")
        check(final["provenance"]["adapter"] == "hockey-xg" and final["provenance"]["seed"] == game_id,
              "the final snapshot carries its provenance (adapter, seed)")
        with SessionLocal() as db:
            dg = db.get(SeriesDailyGame, game_id)
            check((dg.status, dg.home_score, dg.away_score) == ("FINAL", final["home_score"], final["away_score"]),
                  "the daily game row holds the same final score (standings read this)")

        # Nothing to simulate -> an explicit error, not an invented game.
        with SessionLocal() as db:
            other = db.query(SeriesDailyGame).filter(SeriesDailyGame.calendar_date == date + datetime.timedelta(days=1)).first()
            try:
                loader.publish_game(db, other)
                raise AssertionError("a game with no stored credit was published")
            except Exception as e:
                check(type(e).__name__ == "InputError", "a date with no stored credit is refused rather than invented")


def main() -> None:
    audit_attribution()
    audit_adapter()
    audit_end_to_end()
    print("\nAll hockey simulator scoring audits passed.")


if __name__ == "__main__":
    main()
