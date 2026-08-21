"""
Caches CFBD's field-goal expected-points curve (MetricsApi.
get_field_goal_expected_points) so the kicker marginal-EP attribution
doesn't need a live API call for every play.

expectedPoints at a given distance is the average point value of an
attempt from that distance (make probability folded in) - so a kicker's
marginal credit for an actual attempt is:

    made:   3 - expectedPoints(distance)
    missed: 0 - expectedPoints(distance)

A make from a distance where expectedPoints is already close to 3 (a
near-automatic short kick) earns very little marginal credit; a make from
distance where expectedPoints is much lower (a long, low-probability kick)
earns much more - which is the point of not flat-crediting kickers 3
points regardless of difficulty.

Usage:
    python fetch_fg_ep_table.py
"""

import json
import os
from pathlib import Path

import cfbd
from dotenv import load_dotenv

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "fg_ep_lookup.json"


def main():
    load_dotenv()
    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        raise SystemExit("CFBD_API_KEY not set - see .env.example at the repo root.")

    client = cfbd.ApiClient(cfbd.Configuration(access_token=api_key))
    metrics_api = cfbd.MetricsApi(client)
    rows = metrics_api.get_field_goal_expected_points()

    data = {
        "source": "CFBD MetricsApi.get_field_goal_expected_points",
        "rows": [
            {"distance": r.distance, "expected_points": r.expected_points}
            for r in sorted(rows, key=lambda r: r.distance)
        ],
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(data, indent=2))
    print(f"Wrote {OUTPUT_PATH} ({len(data['rows'])} distances)")


if __name__ == "__main__":
    main()
