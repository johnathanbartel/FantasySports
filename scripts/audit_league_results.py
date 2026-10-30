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

from sqlalchemy import func, text

from fantasy_sports.platform import game_results, series_standings as ss, spoilers
from fantasy_sports.platform.db.models.core import (
    League, LeagueSeries, SeriesDailyGame, SimulatorEvent, SimulatorGame, SimulatorReveal, Team,
)
from fantasy_sports.platform.db.session import SessionLocal, engine


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", type=int, required=True, help="league id (not a game id)")
    ap.add_argument("--as-user", type=int, default=None, help="also print the table the old standings showed this user id (unwatched finals left out)")
    ap.add_argument("--team", default=None, help="part of a team name: list every game it appears in, stored vs simulation score, and who has watched")
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
    from fantasy_sports.platform.nhl_daily_sync import current_eastern_date
    held_now = spoilers.standings_held(db, league.id, current_eastern_date())
    print(f"New hold rule today: {len(held_now)} of {len(finals)} finals not yet in the live table (unwatched and no next game started)")
    if args.as_user is not None:
        # What the OLD standings showed this viewer: finals the viewer has not been allowed to see were left out of the table.
        _, held = spoilers.final_sims(db, league.id, args.as_user)
        held_ids = {dg.id for _, dg in held}
        old_games = [dataclasses.replace(g, final=False) if g.game_id in held_ids else g for g in games]
        old = {r.team_id: r for r in ss.compute_standings(team_ids, series, old_games, rules=rules)}
        print(f"\nOld rule for user {args.as_user}: {len(held_ids)} of {len(finals)} finals held back as unwatched")
        print(f"{'Team':26} {'GP':>3} {'W':>3} {'L':>3} {'T':>3} {'GF':>4} {'GA':>4}   owner user id")
        owners = {t.id: t.owner_user_id for t in db.query(Team).filter_by(league_id=league.id)}
        for r in canonical:
            o = old[r.team_id]
            print(f"{names[r.team_id][:26]:26} {o.games_played:3} {o.wins:3} {o.losses:3} {o.ties:3} {o.goals_for:4} {o.goals_against:4}   {owners[r.team_id]}")
    # Does every game row belong to the two teams of its series? The standings tally a game for its own home/away teams; the schedule
    # lists it under its series' teams. A disagreement puts one team's result in another team's record.
    rows = (db.query(SeriesDailyGame, LeagueSeries).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league.id).order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id).all())
    stray = [(g, s) for g, s in rows if {g.home_team_id, g.away_team_id} != {s.team_a_id, s.team_b_id}]
    print(f"\n{len(stray)} game row(s) whose home/away teams are not the two teams of their series:")
    for g, s in stray:
        print(f"  game {g.id} {g.calendar_date} series {s.id} ({names.get(s.team_a_id)} v {names.get(s.team_b_id)}) but game is "
              f"{names.get(g.home_team_id)} (home) v {names.get(g.away_team_id)} (away), {g.status} {g.home_score}-{g.away_score}")
    if args.team:
        pick = [tid for tid, n in names.items() if args.team.lower() in n.lower()]
        if len(pick) != 1:
            print(f"\n--team {args.team!r} matches {[names[t] for t in pick]}; give a unique part of the name")
        else:
            tid = pick[0]
            canon = game_results.final_scores(db, league_id=league.id)
            owners = {t.id: t.owner_user_id for t in db.query(Team).filter_by(league_id=league.id)}
            print(f"\nEvery game {names[tid]} appears in (by the game's own teams, which is what the standings count):")
            wl = [0, 0, 0]
            for g, s in rows:
                if tid not in (g.home_team_id, g.away_team_id, s.team_a_id, s.team_b_id):
                    continue
                sim = db.query(SimulatorGame).filter_by(daily_game_id=g.id).first()
                last = db.query(func.max(SimulatorEvent.seq)).filter_by(simulator_game_id=sim.id).scalar() if sim else None
                seen = ""
                if sim:
                    rv = db.query(SimulatorReveal).filter(SimulatorReveal.simulator_game_id == sim.id).all()
                    seen = "; ".join(f"user {r.user_id} {r.mode} {r.cursor_seq}/{last}" for r in rv) or "no one has opened it"
                c = canon.get(g.id)
                mine_home = g.home_team_id == tid
                tag = ""
                if g.status == "FINAL" and c:
                    gf, ga = (c[0], c[1]) if mine_home else (c[1], c[0])
                    tag = "W" if gf > ga else "L" if gf < ga else "T"
                    wl[0 if tag == "W" else 1 if tag == "L" else 2] += 1
                line = (f"  game {g.id} {g.calendar_date} series {s.id}: {names[g.home_team_id]} (home) v {names[g.away_team_id]} (away) | "
                        f"{g.status} stored {g.home_score}-{g.away_score}")
                if c:
                    line += f" sim {c[0]}-{c[1]}"
                if tag:
                    line += f" -> {names[tid]} {tag}"
                if seen:
                    line += f" | {seen}"
                print(line)
            print(f"  record from these games: {wl[0]}-{wl[1]}-{wl[2]}; owner of {names[tid]}: user {owners.get(tid)}")
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
