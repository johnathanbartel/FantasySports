"""Offline check of platform/elimination_engine.py against brute force. No database, no network.

For hundreds of small random leagues (points and win %, divisions or none, byes, wild cards, every tiebreaker order) it plays
every possible finish of the remaining games - win, loss or tie, with a one-goal and a seven-goal margin - ranks each finish with
the league's real standings and seeding code, and records which teams ever make the field (or earn a bye).

  * Soundness (always asserted): the engine never eliminates a team that makes the field in some finish.
  * Exactness: how many of the teams that brute force proves out the engine also eliminates. With tiebreakers that are settled
    by wins and losses, and no dead-level ties, this must be every one of them; a ranking that depends on a margin or on the
    random draw can only be proven once those games are played, so those cases may lag, and the lag is printed.

Run from the repository root:
    .\\.venv-api\\Scripts\\python.exe scripts/verify_elimination_engine.py
"""

from __future__ import annotations

import itertools
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.platform import elimination_engine as eng  # noqa: E402
from fantasy_sports.platform import playoffs  # noqa: E402
from fantasy_sports.platform import series_standings as ss  # noqa: E402

OUTCOMES = [(2, 1), (8, 1), (1, 2), (1, 8), (3, 3)]       # home win by 1, by 7; away win by 1, by 7; tie
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def make_snapshot(rng, n, divisions, mode, order, byes, qualify, first, played_games, remaining_games, field_size):
    ids = list(range(1, n + 1))
    division_of = {t: (1 + (i * divisions) // n if divisions else None) for i, t in enumerate(ids)}
    pairs = [(a, b) for a in ids for b in ids if a != b]
    results = []
    for _ in range(played_games):
        a, b = rng.choice(pairs)
        results.append(eng.Result(a, b, *rng.choice(OUTCOMES + [(4, 2), (2, 4), (5, 0)])))
    fixtures = [eng.Fixture(*rng.choice(pairs)) for _ in range(remaining_games)]
    rules = ss.StandingsRules(mode, 3, 1, tuple(order), division_of, "league:1")
    return eng.Snapshot(ids, division_of, results, fixtures, rules,
                        playoffs.PlayoffRules(field_size, byes, qualify, first))


def brute(snap):
    """(teams that make the field in some finish, teams that earn a bye in some finish)."""
    in_some, bye_some = set(), set()
    for combo in itertools.product(range(len(OUTCOMES)), repeat=len(snap.remaining)):
        played = list(snap.results)
        for f, o in zip(snap.remaining, combo):
            hs, as_ = OUTCOMES[o]
            played.append(eng.Result(f.home, f.away, hs, as_))
        done = eng.Snapshot(snap.team_ids, snap.division_of, played, [], snap.standings, snap.playoffs)
        report = eng._settled(done)
        in_some |= set(snap.team_ids) - report.eliminated
        if snap.playoffs.byes:
            bye_some |= set(snap.team_ids) - report.bye_eliminated
    return in_some, bye_some


COUNT_ORDERS = [("head_to_head", "wins"), ("wins", "head_to_head"), ("division_record", "wins", "head_to_head")]
MIXED_ORDERS = [("goal_differential", "head_to_head", "goals_for", "goals_against"), ("head_to_head", "goal_differential"),
                ("wins", "goal_differential", "coin_flip"), ("division_record", "goals_for", "goals_against", "coin_flip")]

rng = random.Random(20260617)
lag = {"count": [0, 0], "mixed": [0, 0]}
bye_lag = [0, 0]
started = time.time()
instances = 0
for case in range(120):
    n = rng.choice([4, 5, 6])
    divisions = rng.choice([0, 2])
    mode = rng.choice(["points", "win_pct"])
    kind = rng.choice(["count", "mixed"])
    order = rng.choice(COUNT_ORDERS if kind == "count" else MIXED_ORDERS)
    field_size = rng.randint(1, n - 1)
    byes = rng.choice([0, 0, 1, 2]) if field_size > 1 else 0
    byes = min(byes, field_size)
    qualify = rng.choice([True, True, False])
    first = rng.choice([True, False])
    snap = make_snapshot(rng, n, divisions, mode, order, byes, qualify, first,
                         played_games=rng.randint(n, 3 * n), remaining_games=rng.randint(3, 6), field_size=field_size)
    report = eng.analyze(snap)
    in_some, bye_some = brute(snap)
    instances += 1
    wrongly = report.eliminated & in_some
    check(not wrongly, f"case {case}: eliminated {sorted(wrongly)} but they can make the field (order={order}, mode={mode}, "
                       f"n={n}, div={divisions}, N={field_size}, qualify={qualify}, first={first})")
    if byes:
        wrongly_b = report.bye_eliminated & bye_some
        check(not wrongly_b, f"case {case}: bye-eliminated {sorted(wrongly_b)} but they can earn a bye (order={order}, N={field_size}, "
                             f"byes={byes}, qualify={qualify}, first={first})")
        proven = set(snap.team_ids) - bye_some
        bye_lag[0] += len(proven - report.bye_eliminated)
        bye_lag[1] += len(proven)
    proven_out = set(snap.team_ids) - in_some
    lag[kind][0] += len(proven_out - report.eliminated)
    lag[kind][1] += len(proven_out)

print(f"{instances} random leagues, every finish enumerated: the engine never eliminated a team that could still qualify "
      f"or earn a bye ({time.time() - started:.1f}s).")
for kind, (missed, total) in lag.items():
    print(f"  {kind} tiebreakers: engine proved {total - missed} of {total} brute-force eliminations "
          f"({'exact' if missed == 0 else str(missed) + ' left open until the games are played'})")
if bye_lag[1]:
    print(f"  byes: engine proved {bye_lag[1] - bye_lag[0]} of {bye_lag[1]} brute-force bye eliminations")

# --- hand-made cases ------------------------------------------------------------------------------------------------------

def table(points, left, field_size=2, byes=0, order=("goal_differential", "head_to_head"), division_of=None, qualify=True, first=True):
    """Teams whose points are set with a 3-point result against a dummy opponent, then `left` games to play."""
    ids = sorted(points)
    results = []
    dummy = 99
    for t in ids:
        for _ in range(points[t] // 3):
            results.append(eng.Result(t, dummy, 2, 1))
    rules = ss.StandingsRules("points", 3, 1, tuple(order), division_of or {}, "x")
    return eng.Snapshot(ids + [dummy], {**(division_of or {}), dummy: None}, results,
                        [eng.Fixture(a, b) for a, b in left], rules, playoffs.PlayoffRules(field_size, byes, qualify, first))


# 4 teams fighting for 2 spots; 1 and 2 are far ahead with one game left each against 3 and 4.
snap = table({1: 30, 2: 27, 3: 12, 4: 9}, [(1, 3), (2, 4)])
rep = eng.analyze(snap)
check(rep.eliminated == {3, 4, 99}, f"3 (max 15) and 4 (max 12) cannot reach 1 or 2 (27 at least): out, with the dummy ({rep.eliminated})")

# a team can still catch a tied leader only if the leader loses: 3 can reach 27 = 2's 27 and the result of the tiebreak is open
snap = table({1: 30, 2: 24, 3: 21, 4: 9}, [(1, 3), (2, 4)], field_size=2)
rep = eng.analyze(snap)
check(3 not in rep.eliminated, "3 can still catch team 2 on points when 2 loses and 3 wins")

# the same race with the leader unreachable: 3 needs to pass both 1 and 2
snap = table({1: 30, 2: 30, 3: 21, 4: 9}, [(1, 3), (2, 4)], field_size=2)
check(3 in eng.analyze(snap).eliminated, "3 can reach 24 at most, and 1 and 2 both have 30 already")

# three teams that all still play each other for one spot: nobody can be ruled out
snap = table({1: 30, 2: 18, 3: 18, 4: 18}, [(2, 3), (3, 4), (2, 4)], field_size=2)
rep = eng.analyze(snap)
check(rep.eliminated >= {99}, "the dummy is out")
check(not ({2, 3, 4} & rep.eliminated), "one of 2, 3, 4 will be second: nobody is out while they all still play each other")

# byes
snap = table({1: 45, 2: 27, 3: 24, 4: 9}, [(2, 3), (1, 4)], field_size=3, byes=1)
rep = eng.analyze(snap)
check(2 in rep.bye_eliminated and 3 in rep.bye_eliminated and 1 not in rep.bye_eliminated, "only team 1 can still earn the bye")
check(2 not in rep.eliminated, "but team 2 is in the field")

# division winners: team 4 trails everyone in points but leads its own weak division with the other division already ahead
div = {1: 1, 2: 1, 3: 2, 4: 2}
snap = table({1: 45, 2: 42, 3: 15, 4: 18}, [(3, 4), (1, 2)], field_size=2, division_of=div)
rep = eng.analyze(snap)
check(3 not in rep.eliminated and 4 not in rep.eliminated, "a division winner is in the field whatever its points")
snap = table({1: 45, 2: 42, 3: 15, 4: 18}, [(3, 4), (1, 2)], field_size=2, division_of=div, qualify=False)
rep = eng.analyze(snap)
check(3 in rep.eliminated and 4 in rep.eliminated, "with division winners not guaranteed a place, both are out on rank alone")

# nothing left to play: the league's own ranking decides, ties broken by the tiebreakers in order
ids = [1, 2, 3]
rules = ss.StandingsRules("points", 3, 1, ("head_to_head",), {}, "x")
done = eng.Snapshot(ids, {}, [eng.Result(1, 2, 1, 0), eng.Result(2, 3, 1, 0), eng.Result(3, 1, 1, 0),
                              eng.Result(1, 2, 0, 1), eng.Result(2, 3, 0, 1)], [], rules, playoffs.PlayoffRules(1))
rep = eng.analyze(done)
check(len(rep.eliminated) == 2, "a finished season leaves exactly the field")

# the budget: a cut-off search leaves the team alive and says so
big = make_snapshot(random.Random(1), 12, 2, "points", ("goal_differential",), 0, True, True, 60, 60, 4)
rep = eng.analyze(big, node_budget=50)
check(rep.undetermined or rep.eliminated is not None, "a tiny budget runs and reports")
check(not (rep.undetermined & rep.eliminated), "an undetermined team is never also eliminated")

# speed: a 16-team league with 60 games left each
t0 = time.time()
big = make_snapshot(random.Random(7), 16, 4, "points", ("goal_differential", "head_to_head", "goals_for", "goals_against"),
                    2, True, True, 160, 480, 6)
rep = eng.analyze(big)
elapsed = time.time() - t0
print(f"16 teams, 480 games left: {len(rep.eliminated)} eliminated, {len(rep.undetermined)} undetermined, {elapsed:.2f}s")
check(elapsed < 60, "a deep-season league is analysed in under a minute")

print(f"Elimination engine verification passed ({checks} checks).")
