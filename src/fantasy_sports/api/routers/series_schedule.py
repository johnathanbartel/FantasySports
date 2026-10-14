"""
NHL multi-day series schedule (see platform/series_schedule.py for the contract). Separate from the
football weekly-matchup routes in schedule.py.
"""

import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from fantasy_sports.api.auth import assert_commissioner, current_user, optional_user
from fantasy_sports.api.schemas import (
    LeagueSeriesOut, SeriesScheduleCreateRequest, SeriesScheduleOut, TeamSeriesGameOut, TeamSeriesOut,
    TeamSeriesScheduleOut,
)
from fantasy_sports.platform import series_schedule
from fantasy_sports.platform.db.models.core import (
    Game, League, LeagueSeries, LeagueUniverse, SimulatorEvent, SimulatorGame, SimulatorReveal, Team, Universe, User,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.simulator import service, state as st

router = APIRouter(tags=["series-schedule"])


def _league_series(db: Session, league_id: int) -> list[LeagueSeries]:
    return (db.query(LeagueSeries).options(selectinload(LeagueSeries.daily_games)).filter_by(league_id=league_id)
            .order_by(LeagueSeries.start_date, LeagueSeries.team_a_id).all())


def _out(league: League, rows: list[LeagueSeries], db: Session | None = None, viewer_user_id: int | None = None) -> SeriesScheduleOut:
    watched_map = {}
    slate_summary = None
    real_total = 0
    real_final = 0
    real_live = 0

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

            events = db.query(SimulatorEvent).filter(SimulatorEvent.simulator_game_id.in_(sim_ids)).order_by(
                SimulatorEvent.simulator_game_id, SimulatorEvent.seq
            ).all() if sim_ids else []
            events_by_sim: dict[int, list[SimulatorEvent]] = {}
            for ev in events:
                events_by_sim.setdefault(ev.simulator_game_id, []).append(ev)

            now_utc = service.utcnow()

            for g in dg_list:
                sim = sim_by_dg.get(g.id)
                if not sim:
                    watched_map[g.id] = False
                else:
                    users_who_watched = revealed_users_by_sim.get(sim.id, set())
                    watched_map[g.id] = bool(viewer_user_id and viewer_user_id in users_who_watched)

                sim = sim_by_dg.get(g.id)
                g.home_shots = 0
                g.away_shots = 0
                if sim:
                    sim_evs = events_by_sim.get(sim.id, [])
                    shape = service.shape_of(sim)
                    if sim.status == "FINAL":
                        totals = st.totals_through(sim_evs, len(sim_evs))
                        g.home_shots = totals.home_shots
                        g.away_shots = totals.away_shots
                        g.home_score = max(g.home_score, totals.home_score)
                        g.away_score = max(g.away_score, totals.away_score)
                    elif sim.status == "LIVE":
                        elapsed = st.public_elapsed(shape, sim.status, sim.started_at, now_utc)
                        seq = st.count_through([e.game_second for e in sim_evs], elapsed)
                        totals = st.totals_through(sim_evs, seq)
                        g.home_shots = totals.home_shots
                        g.away_shots = totals.away_shots
                        g.home_score = max(g.home_score, totals.home_score)
                        g.away_score = max(g.away_score, totals.away_score)
                        period, remaining = st.clock_at(shape, elapsed)
                        g.current_period = period
                        g.seconds_remaining = min(remaining, 1200)

        nhl = db.query(Universe).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id).filter(
            LeagueUniverse.league_id == league.id, Universe.code == "nhl"
        ).first()
        if nhl:
            today_date = datetime.date.today()
            start_day = datetime.datetime.combine(today_date, datetime.time.min)
            end_day = start_day + datetime.timedelta(days=1)
            real_games = db.query(Game).filter(
                Game.universe_id == nhl.id, Game.kickoff_at >= start_day, Game.kickoff_at < end_day
            ).all()
            if real_games:
                real_total = len(real_games)
                real_final = len([g for g in real_games if g.status == "final"])
                real_live = len([g for g in real_games if g.status in ("live", "in_progress")])
                if real_final == real_total:
                    slate_summary = f"All {real_total} real NHL games final today"
                elif real_live > 0:
                    slate_summary = f"{real_final} of {real_total} real NHL games final · {real_live} live in progress"
                else:
                    slate_summary = f"{real_total} real NHL games scheduled today"

    for s in rows:
        s.daily_games.sort(key=lambda g: g.calendar_date)
        for g in s.daily_games:
            g.owner_watched = watched_map.get(g.id, False)
    return SeriesScheduleOut(
        league_id=league.id,
        series_length_days=league.series_length_days,
        series=[LeagueSeriesOut.model_validate(s) for s in rows],
        real_slate_summary=slate_summary,
        real_games_total=real_total,
        real_games_final=real_final,
        real_games_live=real_live,
    )


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
def read_series_schedule(league_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if league.series_length_days is None:
        raise HTTPException(400, "Series schedules are only available for NHL leagues")
    try:
        from fantasy_sports.platform.nhl_daily_sync import auto_sync_league_scoreboard
        auto_sync_league_scoreboard(db, league_id)
    except Exception:
        pass
    return _out(league, _league_series(db, league_id), db, viewer_user_id=user.id if user else None)


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
