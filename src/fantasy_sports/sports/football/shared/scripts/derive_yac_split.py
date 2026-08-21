"""
Derives the QB/receiver EPA split used for passing-play credit attribution.

Why this exists: nflfastR gives us, per NFL play, air_yards, yards_after_catch,
and (crucially) yac_epa - the slice of a completion's total EPA attributable
to what the receiver did after the catch. That makes the NFL split exact:

    qb_credit = epa - yac_epa
    receiver_credit = yac_epa

CollegeFootballData.com has no air_yards/yards_after_catch/yac_epa fields
(confirmed by inspecting play objects and StatsApi.get_categories() - see
Football/College/scripts/explore_cfbd_epa.py). It does have yardsGained on
every play, though, and yards gained on a completion is the shared feature
between both data sets. So this script builds an empirical lookup - average
yac_epa conditioned on total yards gained, from real NFL play-by-play - that
NCAAF plays can key off of by their own yardsGained.

For an NCAAF play with predicted-points-added `ppa` and `yardsGained`:
    receiver_credit = lookup[bucket(yardsGained)]
    qb_credit = ppa - receiver_credit

This preserves the no-double-counting property exactly (the two credits
always sum to that specific play's actual ppa), while using NFL data only to
estimate the *typical* after-catch share of value for a play of that length.

Run this whenever refreshing the lookup table (e.g. after each NFL season):
    python derive_yac_split.py --years 2022 2023 2024
"""

import argparse
import json
from pathlib import Path

import nfl_data_py as nfl
import pandas as pd

YARDS_BUCKETS = [
    ("<=0", -100, 0),
    ("1-2", 0, 2),
    ("3-5", 2, 5),
    ("6-9", 5, 9),
    ("10-14", 9, 14),
    ("15-19", 14, 19),
    ("20-29", 19, 29),
    ("30+", 29, 1000),
]

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "yac_epa_lookup.json"


def derive(years: list[int]) -> dict:
    df = nfl.import_pbp_data(years, downcast=False)
    comp = df[(df.play_type == "pass") & (df.complete_pass == 1)].dropna(
        subset=["epa", "yac_epa", "yards_gained"]
    ).copy()

    bin_edges = [b[1] for b in YARDS_BUCKETS] + [YARDS_BUCKETS[-1][2]]
    labels = [b[0] for b in YARDS_BUCKETS]
    comp["yards_bucket"] = pd.cut(comp["yards_gained"], bins=bin_edges, labels=labels)

    agg = comp.groupby("yards_bucket", observed=True).agg(
        n=("epa", "size"),
        mean_epa=("epa", "mean"),
        mean_yac_epa=("yac_epa", "mean"),
        std_yac_epa=("yac_epa", "std"),
    )

    lookup = {
        "source": "nfl_data_py (nflfastR) play-by-play",
        "years": years,
        "overall_mean_yac_epa": float(comp["yac_epa"].mean()),
        "buckets": [
            {
                "label": label,
                "yards_gained_min_exclusive": lo,
                "yards_gained_max_inclusive": hi,
                "n": int(agg.loc[label, "n"]),
                "mean_epa": float(agg.loc[label, "mean_epa"]),
                "mean_yac_epa": float(agg.loc[label, "mean_yac_epa"]),
                "std_yac_epa": float(agg.loc[label, "std_yac_epa"]),
            }
            for label, lo, hi in YARDS_BUCKETS
        ],
    }
    return lookup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024])
    args = parser.parse_args()

    lookup = derive(args.years)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(lookup, indent=2))
    print(f"Wrote {OUTPUT_PATH}")
    for b in lookup["buckets"]:
        print(f"  {b['label']:>6}  n={b['n']:<6}  mean_yac_epa={b['mean_yac_epa']:.3f}")


if __name__ == "__main__":
    main()
