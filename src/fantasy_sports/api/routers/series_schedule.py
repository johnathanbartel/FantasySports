"""
NHL multi-day series schedule (see platform/series_schedule.py for the contract). Separate from the
football weekly-matchup routes in schedule.py.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from fantasy_sports.api.auth import assert_commissioner, current_user
from fantasy_sports.api.schemas import (
    LeagueSeriesOut, SeriesScheduleCreateRequest, SeriesScheduleOut, TeamSeriesGameOut, TeamSeriesOut,
    TeamSeriesScheduleOut,
)
from fantasy_sports.platform import series_schedule
from fantasy_sports.platform.db.models.core import League, LeagueSeries, SimulatorGame, SimulatorReveal, Team, User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["series-schedule"])


def _league_series(db: Session, league_id: int) -> list[LeagueSeries]:
    return (db.query(LeagueSeries).options(selectinload(LeagueSeries.daily_games)).filter_by(league_id=league_id)
            .order_by(LeagueSeries.start_date, LeagueSeries.team_a_id).all())


def _out(league: League, rows: list[LeagueSeries], db: Session | None = None) -> SeriesScheduleOut:
    watched_map = {}
    if db is not None:
        teams = db.query(Team).filter_by(league_id=league.id).all()
        team_map = {t.id: t for t in teams}
        dg_list = [g for s in rows for g in s.daily_games]
        dg_ids = [g.id for g in dg_list]
        if dg_ids:
            sims = db.query(SimulatorGame).filter(SimulatorGame.daily_game_id.in_(dg_ids)).all()
            sim_by_dg = {s.daily_game_id: s for s in sims}
            sim_ids = [s.id for s in sims]
            reveals = db.query(SimulatorReveal).filter(SimulatorReveal.simulator_game_id.in_(sim_ids)).all() if sim_ids else []
            revealed_users_by_sim = {}
            for r in reveals:
                revealed_users_by_sim.setdefault(r.simulator_game_id, set()).add(r.user_id)

            for g in dg_list:
                t_home = team_map.get(g.home_team_id)
                t_away = team_map.get(g.away_team_id)
                if (not t_home or t_home.is_ai_owned) and (not t_away or t_away.is_ai_owned):
                    watched_map[g.id] = True
                else:
                    sim = sim_by_dg.get(g.id)
                    if not sim:
                        watched_map[g.id] = False
                    else:
                        users_who_watched = revealed_users_by_sim.get(sim.id, set())
                        owners = set()
                        if t_home and not t_home.is_ai_owned and t_home.owner_user_id:
                            owners.add(t_home.owner_user_id)
                        if t_away and not t_away.is_ai_owned and t_away.owner_user_id:
                            owners.add(t_away.owner_user_id)
                        watched_map[g.id] = bool(owners & users_who_watched)

    for s in rows:
        s.daily_games.sort(key=lambda g: g.calendar_date)
        for g in s.daily_games:
            g.owner_watched = watched_map.get(g.id, False)
    return SeriesScheduleOut(league_id=league.id, series_length_days=league.series_length_days,
                             series=[LeagueSeriesOut.model_validate(s) for s in rows])


@router.post("/leagues/{league_id}/series-schedule", response_model=SeriesScheduleOut)
def create_series_schedule(league_id: int, payload: SeriesScheduleCreateRequest, db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """Commissioner only. Replace-only: 400 if a schedule exists unless replace=true, and never once play has begun."""
    league = assert_commissioner(db, user, league_id)
    try:
        series_schedule.generate_schedule(db, league, payload.start_date, payload.cycles, payload.replace)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    return _out(league, _league_series(db, league_id), db)


@router.get("/leagues/{league_id}/series-schedule", response_model=SeriesScheduleOut)
def read_series_schedule(league_id: int, db: Session = Depends(get_db)):
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if league.series_length_days is None:
        raise HTTPException(400, "Series schedules are only available for NHL leagues")
    return _out(league, _league_series(db, league_id), db)


@router.get("/teams/{team_id}/series-schedule", response_model=TeamSeriesScheduleOut)
def read_team_series_schedule(team_id: int, db: Session = Depends(get_db)):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=team.league_id)}
    out = []
    for s in _league_series(db, team.league_id):
        if team_id not in (s.team_a_id, s.team_b_id):
            continue
        opp = s.team_b_id if s.team_a_id == team_id else s.team_a_id
        games = [TeamSeriesGameOut(daily_game_id=g.id, calendar_date=g.calendar_date, is_matchup_day=g.is_matchup_day,
                                   home_away="home" if g.home_team_id == team_id else "away", status=g.status)
                 for g in sorted(s.daily_games, key=lambda g: g.calendar_date)]
        out.append(TeamSeriesOut(series_id=s.id, opponent_id=opp, opponent_name=names[opp], start_date=s.start_date,
                                 end_date=s.end_date, total_days=s.total_days, status=s.status, games=games))
    return TeamSeriesScheduleOut(team_id=team_id, league_id=team.league_id, series=out)
