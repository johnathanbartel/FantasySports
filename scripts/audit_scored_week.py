"""
Audit ONE scored week of a league for realism, against the real games in the same database.

Read-only: it opens the database it is pointed at and never writes. Point it at a throwaway copy that has a
scored league (e.g. mock_score_run.db after scripts/run_mock_scoring.py), not at anything you care about.

Checks (each prints its numbers; hard failures raise):
  * the week's real games are final and give a baseline: mean / sd of points per team per game
  * every team's fantasy score for the week (platform/scoring.py, the fitted scale) is positive, finite, and its
    league mean is not inflated against that baseline
  * credit follows real results: across the real teams in the week's games, the summed credit of each real
    team's players correlates with the points that team actually scored
  * the individual credit lines behind the scores (best / worst, by position) are plausible, not runaway
  * the standings view adds up: each team's points_for is the sum of its weekly scores, wins == losses

Usage (repo root):
    .venv-api/Scripts/python.exe scripts/audit_scored_week.py mock_score_run.db --league 3 --week 1
"""

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ap = argparse.ArgumentParser()
ap.add_argument("db")
ap.add_argument("--league", type=int, required=True)
ap.add_argument("--week", type=int, default=1, help="week_number within the league's current season")
args = ap.parse_args()
db_path = (ROOT / args.db).resolve()
if not db_path.exists():
    sys.exit(f"{db_path} missing")
os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.platform import scoring
from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import DraftableAsset, Game, RealTeam, Team
from fantasy_sports.platform.db.session import SessionLocal
from fantasy_sports.sports.football.models import WeeklyPlayerScore

NCAA_PPG = 28.11          # FBS points per team per game (scoring_scale.json ncaa_ppg)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def pearson(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def main():
    db = SessionLocal()
    week = next((w for w in scoring.season_weeks(db, args.league) if w.week_number == args.week), None)
    check(week is not None, f"league {args.league} has no week {args.week}")
    games = db.query(Game).filter_by(week_id=week.id).all()
    check(games and all(g.status == "final" for g in games), f"week {args.week}: real games missing or not all final")
    print(f"League {args.league}, season {week.season_year} week {args.week} ({week.start_date}): {len(games)} real games, all final")

    # -- baseline from the real games ------------------------------------------------------------
    real_pts = defaultdict(float)
    for g in games:
        real_pts[g.home_team_id] += g.home_score
        real_pts[g.away_team_id] += g.away_score
    teams_meta = {t.id: t for t in db.query(RealTeam)}
    all_pts = [g.home_score for g in games] + [g.away_score for g in games]
    fbs_pts = [p for tid, p in real_pts.items() if teams_meta[tid].classification == "fbs"]
    print(f"Real points per team-game: all {statistics.fmean(all_pts):.1f} (sd {statistics.pstdev(all_pts):.1f}, n={len(all_pts)}); "
          f"FBS teams only {statistics.fmean(fbs_pts):.1f} (n={len(fbs_pts)}); season-long NCAA benchmark {NCAA_PPG}")

    # -- fantasy team scores for the week -------------------------------------------------------
    league_teams = db.query(Team).filter_by(league_id=args.league).order_by(Team.id).all()
    context = scoring.league_context(db, args.league)
    rows = []
    for t in league_teams:
        lines = scoring.lineup_breakdown(db, t.id, week.id)
        check(lines, f"{t.name}: no starters set for the week")
        raw = sum(l.points for l in lines)
        rows.append((t, raw, scoring.team_week_score(db, t.id, week.id, context), lines))
    scores = [r[2] for r in rows]
    check(all(math.isfinite(s) and s > 0 for s in scores), "a team score is non-finite or not positive")
    mean, sd = statistics.fmean(scores), statistics.pstdev(scores)
    print(f"\nTeam scores ({len(rows)} teams): mean {mean:.1f}, sd {sd:.1f}, min {min(scores):.1f}, max {max(scores):.1f}")
    for t, raw, s, lines in sorted(rows, key=lambda r: -r[2]):
        played = sum(1 for l in lines if l.played)
        print(f"  {t.name:<22} {s:6.1f} pts   raw credit {raw:7.2f}   {played}/{len(lines)} starters had a game")
    # Non-inflation: the league mean tracks the real scoring environment of the same week, and no team
    # scores more than a football team plausibly could.
    check(mean <= 1.5 * statistics.fmean(all_pts), f"league mean {mean:.1f} is inflated vs real {statistics.fmean(all_pts):.1f}")
    check(max(scores) <= 90, f"top team score {max(scores):.1f} is implausible")

    # -- credit follows real results ---------------------------------------------------------------
    credit_by_team = defaultdict(float)
    asset_team = {}
    for a in db.query(DraftableAsset):
        asset_team[a.id] = a.player.real_team_id if a.player else a.defense_group.real_team_id
    for s in db.query(WeeklyPlayerScore).filter_by(week_id=week.id):
        credit_by_team[asset_team[s.draftable_asset_id]] += s.points
    common = [tid for tid in real_pts if tid in credit_by_team]
    xs = [credit_by_team[t] for t in common]
    ys = [real_pts[t] for t in common]
    r = pearson(xs, ys)
    print(f"\nCredit vs real: summed player credit per real team vs points it actually scored, n={len(common)} teams, r = {r:.2f}")
    check(r > 0.5, f"credit barely tracks real scoring (r = {r:.2f})")

    # -- individual lines ----------------------------------------------------------------------------
    by_pos = defaultdict(list)
    assets = {a.id: a for a in db.query(DraftableAsset)}
    for s in db.query(WeeklyPlayerScore).filter_by(week_id=week.id):
        a = assets[s.draftable_asset_id]
        by_pos[draftable_asset_position(a)].append((s.points, draftable_asset_display_name(a), teams_meta[asset_team[a.id]].name))
    print("\nRaw credit by position (all scored assets that week): n / mean / best / worst")
    for p, v in sorted(by_pos.items(), key=lambda kv: str(kv[0])):
        pts = [x[0] for x in v]
        print(f"  {str(p):<4} n={len(v):5d}  mean {statistics.fmean(pts):6.2f}  best {max(pts):6.2f} ({max(v)[1]}, {max(v)[2]})  worst {min(pts):6.2f} ({min(v)[1]}, {min(v)[2]})")

    # -- standings tie back ------------------------------------------------------------------------
    standings = scoring.standings(db, args.league)
    results = scoring.league_results(db, args.league)
    total_for = defaultdict(float)
    for wk_results in results.values():
        for m in wk_results:
            if m.scored and not m.championship:
                total_for[m.team_a_id] += m.team_a_score
                total_for[m.team_b_id] += m.team_b_score
    for s in standings:
        check(abs(s["points_for"] - total_for[s["team_id"]]) < 1e-6, f"{s['team_name']}: points_for != sum of weekly scores")
    check(sum(s["wins"] for s in standings) == sum(s["losses"] for s in standings), "wins != losses league-wide")
    print("\nStandings (scored regular-season weeks so far):")
    for i, s in enumerate(standings, 1):
        print(f"  {i:2d}. {s['team_name']:<22} {s['wins']}-{s['losses']}-{s['ties']}   PF {s['points_for']:6.1f}   PA {s['points_against']:6.1f}")
    print("\nAUDIT PASSED")


main()
