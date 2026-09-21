"""
Validates the fitted scoring scale (platform/scoring_scale.py).

  A. Requirement 1 (league average = NCAA average), OUT OF SAMPLE: fit the scale on one season's simulated
     leagues, apply it to simulated leagues from the other season, for league sizes in and between the fitted
     table. Reports mean lineup credit vs mean fantasy score, and how far a league's season average strays.
  B. Requirement 2 (EPA leader always wins) on league 1 of user_run.db (read-only): every pair of teams in a
     week, and every scheduled matchup.

  C. Invariants of the mapping (strictly increasing, positive, deterministic) for many league sizes.

Usage:
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/validate_scoring_scale.py [A|B|C]
"""

import sqlite3
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import fit_scoring_scale as F
from fantasy_sports.platform.scoring_scale import ScaleContext, expected_lineup_credit, to_fantasy_points

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DL": 1, "LB": 1, "DB": 1, "K": 1}


def part_a(seeds=150, tau=0.25):
    pts = F.ncaa_points([2022, 2023, 2024])
    P = float(pts.mean())
    test_sizes = [5, 6, 8, 9, 12, 15, 18, 20]
    fit_sims = F.simulate_all([2023, 2024], seeds, tau)
    test_sims = F.simulate_all([2023, 2024], seeds, tau, sizes=test_sizes)
    print(f"NCAA FBS points per team per game: {P:.2f}\n")
    for fit_year, test_year in ((2023, 2024), (2024, 2023)):
        params = F.fit_params(fit_sims, [fit_year], pts, tau)
        print(f"fit on {fit_year} (b={params['b']}, c={params['softplus_c']}) -> tested on {test_year} leagues")
        print(f"  {'teams':>5} {'avg lineup credit':>18} {'avg fantasy score':>18} {'vs NCAA':>8}   {'league-avg score, sd over leagues':>34}   {'worst league':>12}")
        for n in test_sizes:
            leagues = test_sims[test_year][n]["lineup"]                 # list of [n_teams x weeks]
            ctx = ScaleContext(n, SLOTS)
            score_means = [np.mean([to_fantasy_points(x, ctx, params) for x in lg.ravel()]) for lg in leagues]
            credit = np.mean([lg.mean() for lg in leagues])
            print(f"  {n:5d} {credit:18.2f} {np.mean(score_means):18.2f} {np.mean(score_means) - P:+8.2f}   "
                  f"{np.std(score_means):34.2f}   {max(abs(m - P) for m in score_means):12.2f}")
        print()


def part_b(db_path=None):
    db = sqlite3.connect(f"file:{db_path or ROOT / 'user_run.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    n_teams = db.execute("select count(*) from teams where league_id=1").fetchone()[0]
    ctx = ScaleContext(n_teams, SLOTS)
    weeks = [r[0] for r in db.execute("select distinct week_id from lineup_entries order by 1")]
    print(f"league 1: {n_teams} teams; expected lineup credit {expected_lineup_credit(ctx):.2f}")
    violations = ties = pairs_total = 0
    for wk in weeks:
        rows = db.execute("""select le.team_id t, sum(coalesce(w.points,0)) x from lineup_entries le
             join roster_slot_definitions rs on rs.id=le.roster_slot_id and rs.is_starter=1
             left join weekly_player_scores w on w.draftable_asset_id=le.draftable_asset_id and w.week_id=le.week_id
             where le.week_id=? group by le.team_id""", (wk,)).fetchall()
        raw = {r["t"]: r["x"] for r in rows}
        fp = {t: to_fantasy_points(x, ctx) for t, x in raw.items()}
        bad = tie = 0
        for a, b in combinations(raw, 2):
            pairs_total += 1
            if raw[a] > raw[b] and not fp[a] > fp[b] or raw[a] < raw[b] and not fp[a] < fp[b]:
                bad += 1
            if raw[a] == raw[b] and fp[a] != fp[b]:
                bad += 1
            if raw[a] == raw[b]:
                tie += 1
        m = db.execute("select team_a_id a, team_b_id b from weekly_matchups where week_id=?", (wk,)).fetchall()
        mbad = sum(1 for r in m if (raw[r["a"]] > raw[r["b"]]) != (fp[r["a"]] > fp[r["b"]]) and raw[r["a"]] != raw[r["b"]])
        violations += bad
        ties += tie
        print(f"  week_id {wk}: teams {len(raw)}  team pairs checked {len(list(combinations(raw, 2)))}  order violations {bad}  "
              f"raw ties {tie}  matchups {len(m)} winner flips {mbad}   raw {min(raw.values()):+.1f}..{max(raw.values()):+.1f} -> pts {min(fp.values()):.1f}..{max(fp.values()):.1f}")
    print(f"  TOTAL pairs {pairs_total}, violations {violations}, raw ties {ties}")


def part_c():
    """Invariants of the mapping itself, for every fitted league size and a few in between."""
    for n in (2, 4, 5, 6, 7, 8, 12, 17, 20, 24, 40):
        ctx = ScaleContext(n, SLOTS)
        xs = [-150 + 0.01 * i for i in range(30001)]                     # -150 .. +150 credit, step 0.01
        ys = [to_fantasy_points(x, ctx) for x in xs]
        assert all(b > a for a, b in zip(ys, ys[1:])), f"not strictly increasing at n={n}"
        assert min(ys) > 0, f"non-positive score at n={n}"
        assert to_fantasy_points(3.25, ctx) == to_fantasy_points(3.25, ctx)      # deterministic; equal credit ties
    print("C. strictly increasing, positive and deterministic for 11 league sizes over [-150, +150] credit: OK")
    try:
        expected_lineup_credit(ScaleContext(12, {"QB": 2}))
    except ValueError:
        print("   an unfitted lineup shape raises instead of guessing: OK")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "ABC"
    if "A" in which:
        part_a()
    if "B" in which:
        part_b()
    if "C" in which:
        part_c()
