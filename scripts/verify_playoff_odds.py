"""Offline check of platform/playoff_odds.py. No database, no network.

  * The formulas: Pythagorean record and log5 give the textbook numbers.
  * A season with no games left gives exactly 0 or 1, matching the league's own final standings.
  * Playoff probabilities add up to the size of the field (and byes to the number of byes).
  * A team with a better goal record gets better odds than a worse one, other things equal.
  * Consistency with the exact proof: a team the elimination engine proves out never makes the field in any simulated season.
  * The same seed gives the same answer; different seeds stay close.
  * Speed: a realistic league finishes in a reasonable time.

Run from the repository root:
    .\\.venv-api\\Scripts\\python.exe scripts/verify_playoff_odds.py
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.platform import elimination_engine as eng  # noqa: E402
from fantasy_sports.platform import playoff_odds as po  # noqa: E402
from fantasy_sports.platform import playoffs  # noqa: E402
from fantasy_sports.platform import series_standings as ss  # noqa: E402

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def league(rng, n, played, remaining, field_size=4, byes=0, divisions=2, strengths=None, mode="points"):
    ids = list(range(1, n + 1))
    division_of = {t: 1 + (i * divisions) // n for i, t in enumerate(ids)} if divisions else {t: None for t in ids}
    rating = strengths or {t: 1.0 for t in ids}
    pairs = [(a, b) for a in ids for b in ids if a != b]

    def goals(t):
        return max(0, int(rng.gauss(3.0 * rating[t], 1.5) + 0.5))

    results = []
    for _ in range(played):
        a, b = rng.choice(pairs)
        results.append(eng.Result(a, b, goals(a), goals(b)))
    fixtures = [eng.Fixture(*rng.choice(pairs)) for _ in range(remaining)]
    rules = ss.StandingsRules(mode, 3, 1, ("wins", "goal_differential", "head_to_head"), division_of, "league:1")
    return eng.Snapshot(ids, division_of, results, fixtures, rules, playoffs.PlayoffRules(field_size, byes, True, True))


# --- formulas ---------------------------------------------------------------------------------------------------------
check(abs(po.pythagorean(100, 100) - 0.5) < 1e-12, "equal goals is .500")
check(abs(po.pythagorean(120, 100, 2.0) - 14400 / (14400 + 10000)) < 1e-12, "pythagorean value")
check(po.pythagorean(0, 0) == 0.5, "no goals yet is .500")
check(abs(po.log5(0.5, 0.5) - 0.5) < 1e-12, "log5 of equals is .5")
check(abs(po.log5(0.6, 0.5) - 0.6) < 1e-12, "log5 against an average team is the team's own strength")
check(abs(po.log5(0.7, 0.4) + po.log5(0.4, 0.7) - 1) < 1e-12, "log5 is symmetric")
check(abs(po.log5(0.7, 0.4) - (0.7 * 0.6) / (0.7 + 0.4 - 2 * 0.28)) < 1e-12, "log5 textbook value")
check(po.log5(0.8, 0.8) == 0.5 and po.log5(1.0, 0.0) == 1.0, "log5 edges")

# --- a finished season is certain -------------------------------------------------------------------------------------
rng = random.Random(7)
done = league(rng, 8, 60, 0)
rep = po.simulate(done, 50, seed=1)
final = eng._settled(done)
for t in done.team_ids:
    check(rep.teams[t].playoff == (0.0 if t in final.eliminated else 1.0), f"settled season, team {t}")

# --- sums, ordering, determinism, consistency with the proof ----------------------------------------------------------
for trial in range(25):
    rng = random.Random(100 + trial)
    n = rng.choice([6, 8, 10])
    field_size = rng.choice([2, 4])
    byes = rng.choice([0, 0, 1])
    snap = league(rng, n, rng.randint(10, 40), rng.randint(4, 24), field_size, byes, rng.choice([0, 2]),
                  mode=rng.choice(["points", "win_pct"]))
    rep = po.simulate(snap, 300, seed=trial)
    check(abs(sum(o.playoff for o in rep.teams.values()) - field_size) < 1e-9, f"trial {trial}: probabilities sum to the field")
    check(abs(sum(o.bye for o in rep.teams.values()) - byes) < 1e-9, f"trial {trial}: bye probabilities sum to the byes")
    check(all(0.0 <= o.playoff <= 1.0 for o in rep.teams.values()), f"trial {trial}: probabilities in range")
    again = po.simulate(snap, 300, seed=trial)
    check({t: o.playoff for t, o in rep.teams.items()} == {t: o.playoff for t, o in again.teams.items()}, f"trial {trial}: seeded")
    proof = eng.analyze(snap)
    for t in proof.eliminated:
        check(rep.teams[t].playoff == 0.0, f"trial {trial}: proven-out team {t} appeared in a simulated season")
        check(rep.teams[t].bye == 0.0, f"trial {trial}: proven-out team {t} has a bye chance")

# --- strength matters -------------------------------------------------------------------------------------------------
strengths = {1: 1.5, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.0, 6: 0.6}
snap = league(random.Random(5), 6, 90, 30, field_size=3, divisions=0, strengths=strengths)
rep = po.simulate(snap, 2000, seed=3)
check(rep.teams[1].strength > rep.teams[6].strength, "the stronger scoring record rates higher")
check(rep.teams[1].playoff > rep.teams[6].playoff, "the stronger team makes the playoffs more often than the weakest")
a, b = po.simulate(snap, 2000, seed=11), po.simulate(snap, 2000, seed=12)
check(all(abs(a.teams[t].playoff - b.teams[t].playoff) < 0.08 for t in snap.team_ids), "different seeds agree within noise")

# --- early season is pulled toward .500 -------------------------------------------------------------------------------
early = eng.Snapshot([1, 2], {1: None, 2: None}, [eng.Result(1, 2, 9, 0)], [eng.Fixture(1, 2)],
                     ss.StandingsRules("points", 3, 1, ("wins",), {1: None, 2: None}, "x"), playoffs.PlayoffRules(1, 0, False, False))
st = po.team_strengths(early)
check(st[1].pythagorean == 1.0 and 0.5 < st[1].strength < 0.9, "one lopsided game is shrunk toward .500")

# --- speed ------------------------------------------------------------------------------------------------------------
big = league(random.Random(9), 16, 400, 400, field_size=8, byes=0, divisions=4)
t0 = time.time()
po.simulate(big, 1000, seed=1)
elapsed = time.time() - t0
print(f"16 teams, 400 games left, 1000 seasons: {elapsed:.1f}s")
check(elapsed < 60, "simulation too slow")

print(f"OK - {checks} checks passed")
