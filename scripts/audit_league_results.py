"""
Read-only audit of one NHL league's results: rebuilds the standings from the canonical final results (platform/game_results.py)
and from the stored score columns, lists every FINAL game whose stored mirror differs from its published simulation, and prints the
exact UPDATE a repair would run. It changes nothing; applying a repair is a separate step that needs the owner's approval.

    .\\.venv-api\\Scripts\\python.exe scripts/audit_league_results.py --league 9

Uses DATABASE_URL (PostgreSQL transactions are opened READ ONLY). Does not read .env.
"""

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text

from fantasy_sports.platform import game_results, series_standings as ss
from fantasy_sports.platform.db.models.core import League, SeriesDailyGame, Team
from fantasy_sports.platform.db.session import SessionLocal, engine


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", type=int, required=True, help="league id (not a game id)")
    args = ap.parse_args()
    db = SessionLocal()
    if engine.dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION READ ONLY"))
    where = engine.url.render_as_string(hide_password=True)
    print(f"Database: {where}")
    league = db.get(League, args.league)
    if league is None:
        print(f"No league with id {args.league} in this database. If it is a production league, set DATABASE_URL to the production "
              f"database in this terminal first; with no DATABASE_URL this script reads the local fantasy.db.")
        return 1
    if league.series_length_days is None:
        print(f"League {args.league} ({league.name}) is not an NHL league (no series length)")
        return 1
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league.id)}
    team_ids, series, games = ss.load_league(db, league.id)
    rules = ss.rules_for(league, {t.id: t.division_id for t in db.query(Team).filter_by(league_id=league.id)})
    canonical = ss.compute_standings(team_ids, series, games, rules=rules)
    stored_of = {sg.id: sg for sg in db.query(SeriesDailyGame).filter(SeriesDailyGame.id.in_([g.game_id for g in games] or [0]))}
    stored_games = [dataclasses.replace(g, home_score=stored_of[g.game_id].home_score, away_score=stored_of[g.game_id].away_score)
                    for g in games]
    stored = {r.team_id: r for r in ss.compute_standings(team_ids, series, stored_games, rules=rules)}
    finals = [g for g in games if g.final]
    print(f"League {league.id} {league.name}: {len(finals)} FINAL games, {2 * len(finals)} team-games")
    print(f"{'Team':26} {'GP':>3} {'W':>3} {'L':>3} {'T':>3} {'PTS':>4} {'PCT':>6} {'GF':>4} {'GA':>4}  HOME      AWAY      stored GF-GA")
    for r in canonical:
        s = stored[r.team_id]
        flag = "" if (r.goals_for, r.goals_against, r.wins, r.ties) == (s.goals_for, s.goals_against, s.wins, s.ties) else "  <- stored differs"
        print(f"{names[r.team_id][:26]:26} {r.games_played:3} {r.wins:3} {r.losses:3} {r.ties:3} {r.points:4} {r.win_pct:6.3f} {r.goals_for:4} "
              f"{r.goals_against:4}  {r.home_wins}-{r.home_losses}-{r.home_ties:<4} {r.away_wins}-{r.away_losses}-{r.away_ties:<4} "
              f"{s.goals_for}-{s.goals_against}{flag}")
    print(f"Totals: GP {sum(r.games_played for r in canonical)}  W {sum(r.wins for r in canonical)}  L {sum(r.losses for r in canonical)}  "
          f"T {sum(r.ties for r in canonical)}  GF {sum(r.goals_for for r in canonical)}  GA {sum(r.goals_against for r in canonical)}")
    drift = game_results.drift(db, league.id)
    print(f"\n{len(drift)} FINAL game(s) whose stored score differs from the published simulation:")
    for d in drift:
        print(f"  game {d.daily_game_id} {d.calendar_date} {names[d.home_team_id]} v {names[d.away_team_id]}: stored {d.stored[0]}-{d.stored[1]}, "
              f"simulation {d.canonical[0]}-{d.canonical[1]}")
    if drift:
        print("\nProposed repair (NOT run):")
        for d in drift:
            print(f"  UPDATE series_daily_games SET home_score = {d.canonical[0]}, away_score = {d.canonical[1]} WHERE id = {d.daily_game_id};")
    return 0


if __name__ == "__main__":
    sys.exit(main())
