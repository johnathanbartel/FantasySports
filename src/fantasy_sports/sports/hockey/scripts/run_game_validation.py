"""
NHL equivalent of football/professional/scripts/run_week_validation.py: run
every game's shots in the database through the xG credit pipeline and compare
each team's model total with its real goals.

Reads only (never writes) unless --write is passed, which replaces the
shot_credits rows for the games it scored. Point DATABASE_URL at a throwaway
copy when using --write.

Scope: the model total is the shooter credit (goals in regulation and
overtime). Real scores also include the shootout winner's extra goal, which
has no shot rows, so a -1 error on shootout winners is expected, exactly as
the football validation expects a negative bias from unattributed kickoffs
and punts. Model goals allowed is rebuilt from goalie GSAx (xG faced - GSAx)
and excludes empty-net goals, which are not charged to a goalie.

Note the stored game scores come from the same MoneyPuck shot file (see the
README), so this checks the attribution arithmetic, shootout/empty-net
accounting and credit tie-outs, not an independent scoreboard.

Usage:
    DATABASE_URL=sqlite:///./copy.db python run_game_validation.py [--limit N] [--write]
"""

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from fantasy_sports.platform.db.models.core import Game
from fantasy_sports.platform.db.session import SessionLocal
from fantasy_sports.sports.hockey.models import Shot, ShotCredit
from fantasy_sports.sports.hockey.pipeline import ShotRecord, process_shots


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="score only the first N games")
    parser.add_argument("--write", action="store_true", help="replace shot_credits rows for scored games")
    args = parser.parse_args()

    with SessionLocal() as db:
        games = db.query(Game).filter(Game.home_score.isnot(None)).order_by(Game.id)
        if args.limit:
            games = games.limit(args.limit)
        games = games.all()

        by_game = defaultdict(list)
        ids = {g.id for g in games}
        for s in db.query(Shot).order_by(Shot.id):
            if s.game_id not in ids:
                continue
            by_game[s.game_id].append(ShotRecord(
                shot_id=s.id, shooting_team_id=s.shooting_team_id, goalie_team_id=s.goalie_team_id,
                shooter_asset_id=s.shooter_asset_id, goalie_asset_id=s.goalie_asset_id,
                pre_shot_xg=s.pre_shot_xg or 0.0, is_goal=bool(s.is_goal), is_empty_net=bool(s.is_empty_net),
            ))
        print(f"{len(by_game)} NHL games with shots ({len(games)} scored games in the database), "
              f"{sum(len(v) for v in by_game.values())} shots\n")

        rows = []      # team, model_total, real, error
        def_rows = []  # team, model_allowed, real_allowed, error
        max_shooter_tie = max_goalie_tie = 0.0
        unnamed_shooter = unnamed_goalie = 0.0
        en_goals = shootout_games = 0
        shooter_assets, goalie_assets = set(), set()
        scored = 0
        xg_total = goals_total = 0.0

        for game in games:
            shots = by_game.get(game.id)
            if not shots:
                continue
            res = process_shots(shots)
            scored += 1
            real_for = {game.home_team_id: game.home_score, game.away_team_id: game.away_score}
            real_against = {game.home_team_id: game.away_score, game.away_team_id: game.home_score}
            shot_goals = sum(1 for s in shots if s.is_goal)
            if shot_goals != game.home_score + game.away_score:
                shootout_games += 1
            for team, real in real_for.items():
                total = res.model_total(team)
                rows.append((team, total, real, total - real))
                allowed = res.model_goals_allowed(team)
                def_rows.append((team, allowed, real_against[team], allowed - real_against[team]))
            en_goals += sum(res.empty_net_goals_by_team.values())

            # Tie-outs: player-level credits sum back to team/shot-level truth.
            shooter_sum = sum(c.points for c in res.credits if c.role == "shooter") + res.unnamed_shooter_points
            max_shooter_tie = max(max_shooter_tie, abs(shooter_sum - shot_goals))
            goalie_sum = sum(c.points for c in res.credits if c.role == "goalie") + res.unnamed_goalie_points
            max_goalie_tie = max(max_goalie_tie, abs(goalie_sum - sum(res.gsax_by_team.values())))
            unnamed_shooter += res.unnamed_shooter_points
            unnamed_goalie += res.unnamed_goalie_points
            shooter_assets |= set(res.shooter_credit_by_asset)
            goalie_assets |= set(res.gsax_by_asset)
            xg_total += sum(res.xg_faced_by_team.values())
            goals_total += sum(1 for s in shots if s.is_goal and not s.is_empty_net)

            if args.write:
                db.query(ShotCredit).filter(
                    ShotCredit.shot_id.in_([s.shot_id for s in shots])
                ).delete(synchronize_session=False)
                db.bulk_insert_mappings(ShotCredit, [
                    {"shot_id": c.shot_id, "draftable_asset_id": c.asset_id, "role": c.role, "credit_points": c.points}
                    for c in res.credits
                ])
        if args.write:
            db.commit()
            print(f"Wrote shot_credits for {scored} games.\n")

    errors = [r[3] for r in rows]
    exact = sum(1 for e in errors if abs(e) < 1e-9)
    minus_one = sum(1 for e in errors if abs(e + 1) < 1e-9)
    print(f"{len(rows)} team-games scored ({shootout_games} games decided by shootout).")
    print(f"Mean error (model - real):   {statistics.mean(errors):+.3f}")
    print(f"Median error:                {statistics.median(errors):+.2f}")
    print(f"Std dev of error:            {statistics.pstdev(errors):.3f}")
    print(f"Exact matches:               {exact} / {len(errors)}; {minus_one} shootout winners at -1")
    print(f"Other errors (not 0, not -1): {len(errors) - exact - minus_one}")
    corr = statistics.correlation([r[1] for r in rows], [r[2] for r in rows])
    print(f"Correlation(model, real):    {corr:.4f}")
    print(f"Mean per team-game: model {statistics.mean(r[1] for r in rows):.3f} vs real {statistics.mean(r[2] for r in rows):.3f} goals")

    rows_sorted = sorted(rows, key=lambda r: r[3])
    print("\nBiggest overcounts (model >> real):")
    for team, total, real, err in rows_sorted[-3:][::-1]:
        print(f"  team {team:<4} model={total:+6.2f}  real={real:<3}  error={err:+.2f}")
    print("Biggest undercounts (model << real):")
    for team, total, real, err in rows_sorted[:3]:
        print(f"  team {team:<4} model={total:+6.2f}  real={real:<3}  error={err:+.2f}")

    d_errors = [r[3] for r in def_rows]
    print(f"\n=== Goalies: {len(def_rows)} team-games, model goals allowed (xG faced - GSAx) vs real ===")
    print(f"Credit tie-out: max per-game |shooter credit - goals| = {max_shooter_tie:.6f}; "
          f"|goalie credits - team GSAx| = {max_goalie_tie:.6f} (both must be ~0)")
    print(f"Credited players: {len(shooter_assets)} shooters, {len(goalie_assets)} goalies; "
          f"unnamed (id not on roster) shooter points {unnamed_shooter:.0f}, goalie GSAx {unnamed_goalie:+.2f}")
    print(f"Empty-net goals excluded from goalies: {en_goals}")
    print(f"Mean error (model - real):   {statistics.mean(d_errors):+.3f}  (negative = empty-net + shootout goals, by design)")
    print(f"Median error:                {statistics.median(d_errors):+.2f}")
    print(f"Std dev of error:            {statistics.pstdev(d_errors):.3f}")
    print(f"Correlation(model allowed, real allowed): "
          f"{statistics.correlation([r[1] for r in def_rows], [r[2] for r in def_rows]):.4f}")
    print(f"Calibration: league xG faced {xg_total:.0f} vs non-empty-net goals {goals_total:.0f} "
          f"(ratio {xg_total / goals_total:.3f}; total GSAx {goals_total and xg_total - goals_total:+.0f} over {len(def_rows)} team-games)")


if __name__ == "__main__":
    main()
