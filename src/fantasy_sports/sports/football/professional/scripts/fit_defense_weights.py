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

Then derives the centering baselines (data/defense_baseline.json) by running
every play through attribute_play uncentered: the mean credit each DL/LB/DB
group earns per defended play, and the mean credit per involved defender of
each position group.

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
from fantasy_sports.sports.football.professional.defense import (
    EVENT_COLUMNS,
    EVENT_ROLES,
    GROUPS,
    EventWeights,
    PositionResolver,
    attribute_play,
)

OUT = Path(__file__).resolve().parents[1] / "data" / "defense_event_weights.json"
BASELINE_OUT = OUT.with_name("defense_baseline.json")
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


def derive_baselines(pbp, years, weights: EventWeights) -> dict:
    roster = nfl.import_seasonal_rosters(years).dropna(subset=["player_id", "position"])
    resolver = PositionResolver.from_roster_frame(roster.drop_duplicates("player_id", keep="last"))

    plays = 0
    group_sum = {g: 0.0 for g in GROUPS}
    ind_sum = {g: 0.0 for g in GROUPS}
    ind_n = {g: 0 for g in GROUPS}
    for play in pbp.itertuples():
        d = attribute_play(play, weights, resolver)
        plays += 1
        for g, v in d.groups.items():
            group_sum[g] += v
        for c in d.individuals:
            if c.group:
                ind_sum[c.group] += c.points
                ind_n[c.group] += 1
    return {
        "source": f"mean uncentered credit over {plays} REG run/pass plays, {min(years)}-{max(years)}",
        "group": {g: group_sum[g] / plays for g in GROUPS},
        "individual": {g: ind_sum[g] / max(ind_n[g], 1) for g in GROUPS},
        "n_plays": plays,
        "n_individual_involvements": ind_n,
    }


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

    baseline = derive_baselines(pbp, args.years, EventWeights(weights, source=str(OUT)))
    BASELINE_OUT.write_text(json.dumps(baseline, indent=2))
    print("baseline per defended play (group):", {g: round(v, 4) for g, v in baseline["group"].items()})
    print("baseline per involvement (individual):", {g: round(v, 4) for g, v in baseline["individual"].items()})
    print(f"Wrote {BASELINE_OUT}")


if __name__ == "__main__":
    main()
