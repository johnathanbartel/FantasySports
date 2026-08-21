"""
Derives the receiver drop-share used to correct incompletion attribution.

Why this exists: split_passing_epa_incomplete() previously gave 100% of an
incompletion's (usually negative) value to the QB, regardless of whether it
was a bad throw or a receiver drop. Neither CFBD nor nflfastR's play-by-play
has a per-play drop flag - drops are a subjective charting judgment, not an
official stat, and the only free source that tracks them at all is Pro
Football Reference's advanced receiving tables (nfl_data_py's
import_weekly_pfr('rec', ...): receiving_drop, charted weekly per player).

PFR's drop counts are weekly aggregates, not tied to a specific pbp row, and
use a different player-ID system (pfr_player_id vs pbp's gsis-style ids), so
there's no clean per-play join available without a full ID crosswalk. What
IS defensible without that join: an aggregate population rate - total
charted drops over the same seasons, divided by total incompletions in the
same pbp sample (excluding spikes/sacks/INTs, which aren't drop-eligible
targets). That rate gets applied as a flat share of every incompletion's
EPA to the receiver, when one is identifiable from the play text - not
depth-conditioned (unlike the YAC split), since there's no way to bucket
PFR's weekly drop counts by how deep the target was.

Run this whenever refreshing the rate (e.g. after each NFL season):
    python derive_drop_rate.py --years 2022 2023 2024
"""

import argparse
import json
from pathlib import Path

import nfl_data_py as nfl

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "drop_rate.json"


def derive(years: list[int]) -> dict:
    pbp = nfl.import_pbp_data(years, downcast=False)
    pfr = nfl.import_weekly_pfr("rec", years)

    incompletions = pbp[
        (pbp.play_type == "pass")
        & (pbp.complete_pass == 0)
        & (pbp.interception == 0)
        & (pbp.qb_spike != 1)
        & (pbp.sack == 0)
    ]
    total_incompletions = int(len(incompletions))
    total_drops = float(pfr["receiving_drop"].sum())
    drop_rate = total_drops / total_incompletions

    return {
        "source": "nfl_data_py: import_pbp_data (nflfastR) for incompletion counts, "
        "import_weekly_pfr('rec', ...) (Pro Football Reference charting) for drop counts",
        "years": years,
        "total_incompletions": total_incompletions,
        "total_charted_drops": total_drops,
        "drop_rate": drop_rate,
        "note": "Flat population rate, not depth-conditioned - PFR's drop counts are "
        "weekly aggregates with no per-play air_yards to bucket against, and no ID "
        "crosswalk was used to join PFR's pfr_player_id to pbp's gsis-style ids.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024])
    args = parser.parse_args()

    result = derive(args.years)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, indent=2))
    print(f"Wrote {OUTPUT_PATH}")
    print(f"  drop_rate = {result['drop_rate']:.4f} "
          f"({result['total_charted_drops']:.0f} drops / {result['total_incompletions']} incompletions)")


if __name__ == "__main__":
    main()
