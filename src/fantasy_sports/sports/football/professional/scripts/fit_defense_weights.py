"""
Fits the per-event weights behind individual NFL defender credit
(professional/defense.py: attribute_individuals) and writes
professional/data/defense_event_weights.json.

Method: for pass plays and run plays separately, regress the defense's play
value (-epa) on how many times each event role (sack, QB hit, tackle for loss,
pass defense, interception, forced fumble, solo tackle, assist) appears on the
play, with an intercept. A role's weight is the magnitude of its fitted
coefficient - how much that kind of involvement moves a play's value - floored
at FLOOR_FRAC of the median weight so a named defender is never credited zero.
Weights are then used only as relative shares of a play's value, so credit per
play still sums to -epa.

Downloads nflfastR play-by-play for the given seasons (nfl_data_py, no key).

Usage:
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/professional/scripts/fit_defense_weights.py --years 2022 2023 2024
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

import nfl_data_py as nfl
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from fantasy_sports.sports.football.professional.defense import EVENT_COLUMNS, EVENT_ROLES

OUT = Path(__file__).resolve().parents[1] / "data" / "defense_event_weights.json"
FLOOR_FRAC = 0.05


def event_matrix(plays) -> np.ndarray:
    """plays x roles matrix of event counts (half sacks count 0.5)."""
    cols = []
    for role in EVENT_ROLES:
        total = np.zeros(len(plays))
        for id_col, _name, count in EVENT_COLUMNS[role]:
            if id_col in plays.columns:
                total += plays[id_col].notna().to_numpy() * count
        cols.append(total)
    return np.column_stack(cols)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024])
    args = parser.parse_args()

    pbp = nfl.import_pbp_data(args.years, downcast=False)
    pbp = pbp[(pbp.season_type == "REG") & pbp.play_type.isin(("run", "pass")) & pbp.epa.notna()]

    weights, report = {}, {}
    for play_type in ("pass", "run"):
        plays = pbp[pbp.play_type == play_type]
        X = np.column_stack([np.ones(len(plays)), event_matrix(plays)])
        y = -plays.epa.to_numpy()
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        mags = np.abs(coef[1:])
        floor = FLOOR_FRAC * statistics.median(mags[mags > 0]) if (mags > 0).any() else 1.0
        weights[play_type] = {r: float(max(m, floor)) for r, m in zip(EVENT_ROLES, mags)}
        report[play_type] = {
            "n_plays": int(len(plays)),
            "intercept": float(coef[0]),
            "coefficients": {r: float(c) for r, c in zip(EVENT_ROLES, coef[1:])},
            "role_counts": {r: float(n) for r, n in zip(EVENT_ROLES, X[:, 1:].sum(axis=0))},
        }
        print(f"{play_type}: {len(plays)} plays, intercept {coef[0]:+.3f}")
        for r, c, n in zip(EVENT_ROLES, coef[1:], X[:, 1:].sum(axis=0)):
            print(f"  {r:<16} coef={c:+.3f}  weight={weights[play_type][r]:.3f}  events={n:.0f}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "source": f"nflfastR REG-season run/pass plays {min(args.years)}-{max(args.years)}, "
                  "OLS of -epa on event counts",
        "years": args.years,
        "floor_frac": FLOOR_FRAC,
        "weights": weights,
        "fit": report,
    }, indent=2))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
