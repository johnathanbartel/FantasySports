"""
Step 5 of the NCAAF pipeline (spec: "Validate against reality"): run a
full week of FBS games through the pipeline and compare each team's
model total (scrimmage ppa + kicker credit) against its real score,
across every game rather than just one, to see whether the EP model and
attribution logic hold up at scale.

Reminder of scope: model total still excludes punts, kickoffs, and
defensive/return touchdowns (no per-play ppa from CFBD for those), so a
systematic negative bias is expected, not a bug - see run_game_pipeline.py.
This is checking whether the *relationship* between model total and real
score is consistent (correlation, spread of the error), not whether the
raw totals match.

Usage:
    python run_week_validation.py --year 2024 --week 1
"""

import argparse
import os
import statistics
from pathlib import Path

import cfbd
from dotenv import load_dotenv

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from fantasy_sports.sports.football.college.pipeline import process_plays


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
    args = parser.parse_args()

    client = get_api_client()
    games_api = cfbd.GamesApi(client)
    plays_api = cfbd.PlaysApi(client)

    games = games_api.get_games(year=args.year, week=args.week, classification="fbs")
    plays = plays_api.get_plays(
        year=args.year, week=args.week, season_type="regular", classification="fbs"
    )
    print(f"{len(games)} FBS games, {len(plays)} plays for {args.year} week {args.week}\n")

    plays_by_game: dict[int, list] = {}
    for p in plays:
        plays_by_game.setdefault(p.game_id, []).append(p)

    real_score: dict[str, int] = {}
    rows: list[tuple[str, float, int, float]] = []  # team, model_total, real, error
    total_unattributed = 0
    total_unattributed_fg = 0
    total_skipped = 0
    total_scrimmage_plays = 0

    for game in games:
        real_score[game.home_team] = game.home_points
        real_score[game.away_team] = game.away_points
        game_plays = plays_by_game.get(game.id, [])
        if not game_plays:
            continue
        result = process_plays(game_plays)
        total_unattributed += len(result.unattributed)
        total_unattributed_fg += len(result.unattributed_fg)
        total_skipped += len(result.skipped)
        total_scrimmage_plays += sum(1 for p in game_plays if p.ppa is not None)

        for team, real in ((game.home_team, game.home_points), (game.away_team, game.away_points)):
            if real is None:
                continue
            model_total = result.model_total(team)
            rows.append((team, model_total, real, model_total - real))

    errors = [r[3] for r in rows]
    print(f"{len(rows)} team-games scored.")
    print(f"Parser coverage: {total_unattributed} unattributed scrimmage plays / "
          f"{total_scrimmage_plays} total ({100 * total_unattributed / max(total_scrimmage_plays,1):.1f}%), "
          f"{total_unattributed_fg} unattributed FG plays, {total_skipped} plays skipped "
          f"as known out-of-scope (fumbles, turnover/return TDs, safeties).\n")

    print(f"Mean error (model - real):   {statistics.mean(errors):+.2f}")
    print(f"Median error:                {statistics.median(errors):+.2f}")
    print(f"Std dev of error:            {statistics.pstdev(errors):.2f}")

    model_vals = [r[1] for r in rows]
    real_vals = [r[2] for r in rows]
    try:
        corr = statistics.correlation(model_vals, real_vals)
    except statistics.StatisticsError:
        corr = float("nan")
    print(f"Correlation(model, real):    {corr:.3f}")

    rows_sorted = sorted(rows, key=lambda r: r[3])
    print("\nBiggest overcounts (model >> real):")
    for team, model_total, real, err in rows_sorted[-5:][::-1]:
        print(f"  {team:<20} model={model_total:+6.2f}  real={real:<4}  error={err:+.2f}")

    print("\nBiggest undercounts (model << real):")
    for team, model_total, real, err in rows_sorted[:5]:
        print(f"  {team:<20} model={model_total:+6.2f}  real={real:<4}  error={err:+.2f}")


if __name__ == "__main__":
    main()
