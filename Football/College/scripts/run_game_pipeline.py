"""
Step 4 of the NCAAF pipeline: run one real game through the whole chain -
raw plays -> ppa -> attributed player points -> team-week total - and
sanity-check the output against that game's real final score.

Scope note: model total excludes punts, kickoffs, and defensive/return
scores - CFBD gives no per-play ppa for those. Player/defense/kicker
attribution logic lives in Football/College/pipeline.py, shared with
run_week_validation.py.

Usage:
    python run_game_pipeline.py --year 2024 --week 1 --team Georgia
"""

import argparse
import os
from pathlib import Path

import cfbd
from dotenv import load_dotenv

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from Football.College.pipeline import process_plays


def get_api_client() -> cfbd.ApiClient:
    load_dotenv()
    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        raise SystemExit("CFBD_API_KEY not set - see .env.example at the repo root.")
    return cfbd.ApiClient(cfbd.Configuration(access_token=api_key))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--week", type=int, default=1)
    parser.add_argument("--team", type=str, default="Georgia")
    args = parser.parse_args()

    client = get_api_client()
    games_api = cfbd.GamesApi(client)
    plays_api = cfbd.PlaysApi(client)

    games = games_api.get_games(year=args.year, week=args.week, team=args.team)
    if not games:
        raise SystemExit("No game found for that year/week/team.")
    game = games[0]

    plays = plays_api.get_plays(
        year=args.year, week=args.week, season_type="regular", team=args.team
    )

    result = process_plays(plays)

    print(f"{game.away_team} {game.away_points} @ {game.home_team} {game.home_points}")
    print(f"({len(plays)} plays pulled, {len(result.unattributed)} scrimmage plays with "
          f"ppa but no attribution-parser match, {len(result.unattributed_fg)} FG plays "
          f"with no parser match)\n")

    print("Player fantasy points (rushing + passing + kicker attribution):")
    for (player, team), pts in sorted(result.credits.items(), key=lambda kv: -kv[1]):
        print(f"  {pts:+6.2f}  {player:<22} {team}")

    print("\nDefensive group fantasy points (DL / LB / DB, heuristic v1):")
    teams = sorted({t for t, _ in result.defense_credits})
    for team in teams:
        for group in ("DL", "LB", "DB"):
            pts = result.defense_credits.get((team, group), 0.0)
            print(f"  {pts:+6.2f}  {team:<20} {group}")

    print("\nTeam scrimmage-play ppa + kicker credit sum vs. real final score:")
    real_score = {game.home_team: game.home_points, game.away_team: game.away_points}
    all_teams = set(result.scrimmage_ppa_by_team) | set(result.fg_credit_by_team)
    for team in all_teams:
        print(f"  {team:<20} model total = {result.model_total(team):+6.2f}   "
              f"real score = {real_score.get(team)}")

    print(
        "\nCaveat: model total still excludes punts, kickoffs, and defensive/"
        "return scores (CFBD gives no per-play ppa for those). This checks "
        "whether the rush/pass EP model, attribution logic, and kicker "
        "marginal-EP are sane, not the full-score validation from the spec "
        "yet. Defensive DL/LB/DB splits above are a play-type/yardage "
        "heuristic, not tackler-level data - see the comment above "
        "attribute_defense() in Football/common/attribution.py."
    )

    if result.unattributed:
        print(f"\n{len(result.unattributed)} unattributed scrimmage plays (regex didn't match):")
        for p in result.unattributed[:10]:
            print(f"  {p.play_type}: {p.play_text}")


if __name__ == "__main__":
    main()
