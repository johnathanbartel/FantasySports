"""
Step 1 of the NCAAF pipeline: figure out whether CollegeFootballData.com
already provides an EPA-equivalent metric (they call it PPA - Predicted
Points Added) before we spend time deriving an Expected Points model from
scratch.

Usage:
    python explore_cfbd_epa.py --year 2024 --week 1 --team Georgia
"""

import argparse
import os

import cfbd
from dotenv import load_dotenv


def get_api_client() -> cfbd.ApiClient:
    load_dotenv()
    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        raise SystemExit(
            "CFBD_API_KEY not set. Copy .env.example to .env at the repo "
            "root and fill in your key from collegefootballdata.com."
        )
    config = cfbd.Configuration(access_token=api_key)
    return cfbd.ApiClient(config)


def check_ppa_fields(client: cfbd.ApiClient, year: int, week: int, team: str | None):
    """CFBD's /ppa/plays endpoint is their pre-built EPA equivalent."""
    plays_api = cfbd.PlaysApi(client)
    plays = plays_api.get_plays(year=year, week=week, season_type="regular", team=team)

    print(f"Pulled {len(plays)} plays for year={year} week={week} team={team!r}")
    if not plays:
        print("No plays returned - check year/week/team.")
        return

    sample = plays[0]
    print("\nSample play object fields:")
    for field in sample.to_dict().keys():
        print(f"  {field}")

    plays_with_ppa = [p for p in plays if p.ppa is not None]
    print(
        f"\n{len(plays_with_ppa)}/{len(plays)} plays have a non-null per-play "
        f"'ppa' field."
    )
    if plays_with_ppa:
        print("Sample plays (playText -> ppa):")
        for p in plays_with_ppa[:8]:
            print(f"  {p.ppa:+.3f}  {p.play_type:<20}  {p.play_text}")

    metrics_api = cfbd.MetricsApi(client)

    try:
        team_ppa = metrics_api.get_predicted_points_added_by_team(year=year, team=team)
        print(f"\nget_predicted_points_added_by_team: {len(team_ppa)} rows for team={team!r}.")
        if team_ppa:
            print("Sample team-PPA record:")
            for field, value in team_ppa[0].to_dict().items():
                print(f"  {field}: {value}")
    except cfbd.ApiException as exc:
        print(f"\nget_predicted_points_added_by_team call failed: {exc}")

    print("\nget_predicted_points (raw EP model) at a few sample situations:")
    for down, distance in [(1, 10), (3, 3), (4, 1)]:
        try:
            ep = metrics_api.get_predicted_points(down=down, distance=distance)
            print(f"  down={down} distance={distance}: {len(ep)} yard-line rows")
            if ep:
                mid = ep[len(ep) // 2]
                print(f"    e.g. {mid.to_dict()}")
        except cfbd.ApiException as exc:
            print(f"  down={down} distance={distance} call failed: {exc}")

    try:
        fg_ep = metrics_api.get_field_goal_expected_points()
        print(f"\nget_field_goal_expected_points: {len(fg_ep)} rows returned.")
        if fg_ep:
            print("Sample FG-EP record:")
            for field, value in fg_ep[0].to_dict().items():
                print(f"  {field}: {value}")
    except cfbd.ApiException as exc:
        print(f"\nget_field_goal_expected_points call failed: {exc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--week", type=int, default=1)
    parser.add_argument("--team", type=str, default=None)
    args = parser.parse_args()

    client = get_api_client()
    check_ppa_fields(client, args.year, args.week, args.team)


if __name__ == "__main__":
    main()
