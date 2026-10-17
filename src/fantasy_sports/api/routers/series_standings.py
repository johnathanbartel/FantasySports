"""
NHL series standings, rivalry history and Game Score (derived on read; see platform/series_standings.py and
platform/game_score.py). Only the publish endpoint writes, and only series_daily_games.game_score_rating.
"""

import dataclasses
import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, current_user, optional_user
from fantasy_sports.api.schemas import (
    GameScoreOut, GameScoresOut, PlayoffOddsOut, PlayoffOddsTeamOut, RivalriesOut, RivalryOut, SeriesStandingOut,
    SeriesStandingsOut,
)
from fantasy_sports.platform import elimination, game_score, playoffs, series_standings, spoilers
from fantasy_sports.platform import tiebreakers as tiebreaker_rules
from fantasy_sports.platform.db.models.core import League, LeagueSeries, SeriesDailyGame, Team, User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["series-standings"])


def _nhl_league(db: Session, league_id: int) -> League:
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if league.series_length_days is None:
        raise HTTPException(400, "Series standings are only available for NHL leagues")
    return league


@router.get("/leagues/{league_id}/series-standings", response_model=SeriesStandingsOut)
def read_series_standings(league_id: int, as_of: datetime.date | None = None, db: Session = Depends(get_db),
                          user: User | None = Depends(optional_user)):
    _nhl_league(db, league_id)
    team_ids, series, games = series_standings.load_league(db, league_id)
    # Results the viewer has not been allowed to see yet (unwatched games) stay out of the table.
    _, held = spoilers.final_sims(db, league_id, user.id if user else None)
    held_ids = {dg.id for _, dg in held}
    games = [dataclasses.replace(g, final=False) if g.game_id in held_ids else g for g in games]
    league = db.get(League, league_id)
    teams = db.query(Team).filter_by(league_id=league_id).all()
    names = {t.id: t.name for t in teams}
    division_of = {t.id: t.division_id for t in teams}
    rules = series_standings.rules_for(league, division_of)
    rows = series_standings.compute_standings(team_ids, series, games, as_of, rules=rules)
    playoff_rules = playoffs.rules_for(league, len(teams))
    field_size = playoff_rules.teams
    field = {f["team_id"]: f for f in playoffs.seed_teams([r.team_id for r in rows], division_of, playoff_rules)}
    out_teams = {t.id for t in teams if elimination.is_eliminated(league, t)}
    order = (f"{'points' if rules.mode == 'points' else 'win percentage'} desc"
             + "".join(f", {k} {'asc' if k == 'goals_against' else 'desc'}" for k in rules.tiebreakers) + ", team_id asc")
    return SeriesStandingsOut(league_id=league_id, as_of=as_of, tiebreak_order=order, standings_mode=rules.mode,
                              points_per_win=rules.points_win, points_per_tie=rules.points_tie, playoff_teams=field_size,
                              tiebreakers=list(rules.tiebreakers), playoff_byes=playoff_rules.byes,
                              division_winners_qualify=playoff_rules.division_winners_qualify,
                              division_winners_seeded_first=playoff_rules.division_winners_seeded_first, standings=[
        SeriesStandingOut(rank=r.rank, team_id=r.team_id, team_name=names[r.team_id], wins=r.wins, losses=r.losses,
                          ties=r.ties, goals_for=r.goals_for,
                          goals_against=r.goals_against, goal_diff=r.goal_diff, h2h_wins=r.h2h_wins,
                          home_wins=r.home_wins, home_losses=r.home_losses, home_ties=r.home_ties,
                          away_wins=r.away_wins, away_losses=r.away_losses, away_ties=r.away_ties,
                          games_played=r.games_played, points=r.points, win_pct=round(r.win_pct, 4),
                          seed=field[r.team_id]["seed"] if r.team_id in field else None,
                          division_winner=bool(field.get(r.team_id, {}).get("division_winner")),
                          bye=bool(field.get(r.team_id, {}).get("bye")), eliminated=r.team_id in out_teams) for r in rows])


@router.get("/leagues/{league_id}/playoff-odds", response_model=PlayoffOddsOut)
def read_playoff_odds(league_id: int, simulations: int = Query(1000, ge=100, le=5000), seed: int | None = None,
                      db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    """Playoff and bye chances from simulating the rest of the season on each team's Pythagorean strength. An estimate: it never
    eliminates anyone. Games the viewer has not watched yet are treated as unplayed, so this cannot leak a result."""
    league = _nhl_league(db, league_id)
    _, held = spoilers.final_sims(db, league_id, user.id if user else None)
    report = elimination.playoff_odds(db, league, simulations, seed, frozenset(dg.id for _, dg in held))
    if report is None:
        raise HTTPException(409, "Playoff odds need a schedule and an in-season league")
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league_id)}
    rows = sorted(report.teams.values(), key=lambda o: (-o.playoff, -o.bye, o.team_id))
    return PlayoffOddsOut(
        league_id=league_id, simulations=report.simulations, pythagorean_exponent=report.exponent,
        tie_rate=round(report.tie_rate, 4), teams=[
            PlayoffOddsTeamOut(team_id=o.team_id, team_name=names.get(o.team_id, ""), games_played=o.games_played,
                               goals_for=o.goals_for, goals_against=o.goals_against, pythagorean=round(o.pythagorean, 4),
                               strength=round(o.strength, 4), playoff_probability=round(o.playoff, 4),
                               bye_probability=round(o.bye, 4), mean_points=round(o.mean_points, 3), eliminated=o.eliminated)
            for o in rows])


def _rivalry_out(league_id: int, p: series_standings.PairRecord) -> RivalryOut:
    return RivalryOut(league_id=league_id, team_low_id=p.low_id, team_high_id=p.high_id,
                      series_low_wins=p.series_low_wins, series_high_wins=p.series_high_wins, series_ties=p.series_ties,
                      daily_low_wins=p.daily_low_wins, daily_high_wins=p.daily_high_wins, daily_ties=p.daily_ties,
                      goals_low=p.goals_low, goals_high=p.goals_high, net_goal_diff_low=p.goals_low - p.goals_high,
                      intensity=round(game_score.rivalry_intensity(p), 4))


@router.get("/leagues/{league_id}/rivalries", response_model=RivalriesOut)
def read_rivalries(league_id: int, team_id: int | None = None, as_of: datetime.date | None = None,
                   db: Session = Depends(get_db)):
    """Head-to-head history for every pair (optionally only pairs involving team_id) with at least one final game."""
    _nhl_league(db, league_id)
    _, series, games = series_standings.load_league(db, league_id)
    pairs = series_standings.compute_pair_records(series, games, as_of)
    rows = [p for p in pairs.values() if p.games and (team_id is None or team_id in (p.low_id, p.high_id))]
    rows.sort(key=lambda p: (p.low_id, p.high_id))
    return RivalriesOut(league_id=league_id, as_of=as_of, rivalries=[_rivalry_out(league_id, p) for p in rows])


@router.get("/leagues/{league_id}/rivalries/{team_a_id}/{team_b_id}", response_model=RivalryOut)
def read_rivalry(league_id: int, team_a_id: int, team_b_id: int, as_of: datetime.date | None = None,
                 db: Session = Depends(get_db)):
    _nhl_league(db, league_id)
    ids = {t.id for t in db.query(Team.id).filter_by(league_id=league_id)}
    if team_a_id == team_b_id or team_a_id not in ids or team_b_id not in ids:
        raise HTTPException(404, "Both teams must be distinct teams of this league")
    _, series, games = series_standings.load_league(db, league_id)
    lo, hi = min(team_a_id, team_b_id), max(team_a_id, team_b_id)
    pair = series_standings.compute_pair_records(series, games, as_of).get((lo, hi)) or series_standings.PairRecord(lo, hi)
    return _rivalry_out(league_id, pair)


def _rate(db: Session, league_id: int, as_of: datetime.date | None) -> dict:
    team_ids, series, games = series_standings.load_league(db, league_id)
    group_of = {t.id: t.conference_id for t in db.query(Team).filter_by(league_id=league_id)}
    return game_score.rate_league(team_ids, series, games, group_of, as_of_override=as_of)


def _game_rows(db: Session, league_id: int) -> list[SeriesDailyGame]:
    return (db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id).order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id).all())


def _score_out(g: SeriesDailyGame, r: game_score.GameScore) -> GameScoreOut:
    return GameScoreOut(daily_game_id=g.id, series_id=g.series_id, calendar_date=g.calendar_date,
                        home_team_id=g.home_team_id, away_team_id=g.away_team_id, status=g.status, score=r.score,
                        margin=round(r.margin, 4), cutoff=round(r.cutoff, 4), rivalry=round(r.rivalry, 4),
                        urgency=round(r.urgency, 4), standings_as_of=r.as_of)


def _scores_out(league_id: int, rows: list[SeriesDailyGame], rated: dict) -> GameScoresOut:
    return GameScoresOut(
        league_id=league_id,
        weights={"rivalry": game_score.W_RIVALRY, "urgency": game_score.W_URGENCY, "margin": game_score.W_MARGIN,
                 "cutoff": game_score.W_CUTOFF},
        games=[_score_out(g, rated[(g.series_id, g.calendar_date)]) for g in rows])


@router.get("/leagues/{league_id}/game-scores", response_model=GameScoresOut)
def read_game_scores(league_id: int, as_of: datetime.date | None = None, db: Session = Depends(get_db)):
    """Per-game Game Score with its component breakdown. as_of overrides the one-week-ahead publication cutoff."""
    _nhl_league(db, league_id)
    return _scores_out(league_id, _game_rows(db, league_id), _rate(db, league_id, as_of))


@router.get("/series-daily-games/{game_id}/game-score", response_model=GameScoreOut)
def read_game_score(game_id: int, db: Session = Depends(get_db)):
    game = db.get(SeriesDailyGame, game_id)
    if game is None:
        raise HTTPException(404, "Game not found")
    league_id = db.get(LeagueSeries, game.series_id).league_id
    return _score_out(game, _rate(db, league_id, None)[(game.series_id, game.calendar_date)])


@router.post("/leagues/{league_id}/game-scores/publish", response_model=GameScoresOut)
def publish_game_scores(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner only. Stores the current rating in game_score_rating for games that are not FINAL yet."""
    assert_commissioner(db, user, league_id)
    _nhl_league(db, league_id)
    rated = _rate(db, league_id, None)
    rows = _game_rows(db, league_id)
    for g in rows:
        if g.status != "FINAL":
            g.game_score_rating = rated[(g.series_id, g.calendar_date)].score
    db.commit()
    return _scores_out(league_id, rows, rated)
