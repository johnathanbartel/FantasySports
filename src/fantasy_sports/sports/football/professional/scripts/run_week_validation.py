"""
NFL equivalent of college/scripts/run_week_validation.py (spec: "Validate
against reality"): run a full week of real games through the pipeline and
compare each team's model total (scrimmage epa + kicker credit) against its
real score.

Scope reminder: model total excludes kickoffs, punts, and
defensive/return touchdowns - nflfastR's epa on those play types isn't
attributed by this pipeline (see pipeline.py's SCRIMMAGE_PLAY_TYPES), so a
systematic negative bias is expected here too, same as the college
pipeline. This checks whether the offense attribution (exact, from
nflfastR's real yac_epa - no yards-bucket approximation needed) is sane at
scale, not whether raw totals match.

Usage:
    python run_week_validation.py --year 2024 --week 1
"""

import argparse
import statistics

import nfl_data_py as nfl

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from fantasy_sports.sports.football.professional.pipeline import process_plays


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--week", type=int, default=1)
    args = parser.parse_args()

    pbp = nfl.import_pbp_data([args.year], downcast=False)
    pbp = pbp[(pbp.week == args.week) & (pbp.season_type == "REG")]

    sched = nfl.import_schedules([args.year])
    sched = sched[(sched.week == args.week) & (sched.game_type == "REG")]
    print(f"{len(sched)} REG games, {len(pbp)} plays for {args.year} week {args.week}\n")

    real_score: dict[str, float] = {}
    for row in sched.itertuples():
        real_score[row.home_team] = row.home_score
        real_score[row.away_team] = row.away_score

    rows: list[tuple[str, float, float, float]] = []  # team, model_total, real, error
    total_unattributed = 0
    total_skipped = 0
    total_scrimmage_plays = 0

    for game_id, game_plays in pbp.groupby("game_id"):
        result = process_plays(game_plays)
        total_unattributed += len(result.unattributed)
        total_skipped += len(result.skipped)
        total_scrimmage_plays += game_plays.play_type.isin(("run", "pass")).sum()

        teams = set(result.scrimmage_epa_by_team) | set(result.fg_credit_by_team)
        for team in teams:
            real = real_score.get(team)
            if real is None:
                continue
            model_total = result.model_total(team)
            rows.append((team, model_total, real, model_total - real))

    errors = [r[3] for r in rows]
    print(f"{len(rows)} team-games scored.")
    print(f"Parser coverage: {total_unattributed} unattributed scrimmage plays / "
          f"{total_scrimmage_plays} total ({100 * total_unattributed / max(total_scrimmage_plays, 1):.1f}%), "
          f"{total_skipped} plays skipped as out-of-scope (kickoffs/punts/no-plays/etc.).\n")

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
        print(f"  {team:<6} model={model_total:+6.2f}  real={real:<4}  error={err:+.2f}")

    print("\nBiggest undercounts (model << real):")
    for team, model_total, real, err in rows_sorted[:5]:
        print(f"  {team:<6} model={model_total:+6.2f}  real={real:<4}  error={err:+.2f}")


if __name__ == "__main__":
    main()
