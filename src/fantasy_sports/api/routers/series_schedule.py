"""
NHL multi-day series schedule (see platform/series_schedule.py for the contract). Separate from the
football weekly-matchup routes in schedule.py.
"""

import datetime
import logging
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

logger = logging.getLogger(__name__)
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
            reveal_of = {}
            for r in reveals:
                revealed_users_by_sim.setdefault(r.simulator_game_id, set()).add(r.user_id)
                reveal_of[(r.simulator_game_id, r.user_id)] = r

            events = db.query(SimulatorEvent).filter(SimulatorEvent.simulator_game_id.in_(sim_ids)).order_by(
                SimulatorEvent.simulator_game_id, SimulatorEvent.seq
            ).all() if sim_ids else []
            events_by_sim: dict[int, list[SimulatorEvent]] = {}
            for ev in events:
                events_by_sim.setdefault(ev.simulator_game_id, []).append(ev)

            now_utc = service.utcnow()

            for g in dg_list:
                t_home = team_map.get(g.home_team_id)
                t_away = team_map.get(g.away_team_id)
                is_ai_vs_ai = bool(t_home and t_home.is_ai_owned and t_away and t_away.is_ai_owned)
                if is_ai_vs_ai:
                    watched_map[g.id] = True
                else:
                    sim = sim_by_dg.get(g.id)
                    if not sim:
                        watched_map[g.id] = False
                    else:
                        users_who_watched = revealed_users_by_sim.get(sim.id, set())
                        owners = set()
                        if t_home and t_home.owner_user_id:
                            owners.add(t_home.owner_user_id)
                        if t_away and t_away.owner_user_id:
                            owners.add(t_away.owner_user_id)
                        watched_map[g.id] = bool((owners & users_who_watched) or (viewer_user_id and viewer_user_id in users_who_watched))

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
                        if service.is_progressive(sim):
                            g.home_shots, g.away_shots = service.live_shots(sim, sim_evs, now_utc)
                    if service.is_progressive(sim) and not is_ai_vs_ai:
                        # Period-by-period games: a viewer sees the score of the periods they have watched, no more.
                        mine = reveal_of.get((sim.id, viewer_user_id)) if viewer_user_id else None
                        cursor = 0
                        if mine is not None:
                            cursor = len(sim_evs) if (mine.mode == st.SKIPPED and sim.status == "FINAL") else min(mine.cursor_seq, len(sim_evs))
                        watched_map[g.id] = cursor > 0
                        shown = st.totals_through(sim_evs, cursor)
                        g.home_score, g.away_score = shown.home_score, shown.away_score

        nhl = db.query(Universe).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id).filter(
            LeagueUniverse.league_id == league.id, Universe.code == "nhl"
        ).first()
        if nhl:
            from fantasy_sports.platform.nhl_daily_sync import current_eastern_date, is_real_game_started
            today_date = current_eastern_date()
            start_day = datetime.datetime.combine(today_date, datetime.time.min)
            end_day = start_day + datetime.timedelta(days=1)
            raw_real_games = db.query(Game).filter(
                Game.universe_id == nhl.id, Game.kickoff_at >= start_day, Game.kickoff_at < end_day
            ).all()
            real_games = [g for g in raw_real_games if not (g.kickoff_at and 0 <= g.kickoff_at.hour < 6)]
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
    """Commissioner only. Replace-only: 400 if a schedule exists unless replace=true, and never once play has begun (unless force=true)."""
    league = assert_commissioner(db, user, league_id)
    try:
        series_schedule.generate_schedule(db, league, payload.start_date, payload.cycles, payload.replace, payload.force,
                                        window_rows=payload.window_rows)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    return _out(league, _league_series(db, league_id), db)


@router.post("/leagues/{league_id}/reschedule-next-window", response_model=SeriesScheduleOut)
def reschedule_next_window(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner only. Reschedules an NHL league to start at the beginning of the next full window."""
    league = assert_commissioner(db, user, league_id)
    from fantasy_sports.platform import auto_schedule
    err = auto_schedule.reschedule_nhl_league(db, league, force=True)
    if err:
        raise HTTPException(400, err)
    db.commit()
    return _out(league, _league_series(db, league_id), db, viewer_user_id=user.id)


@router.get("/leagues/{league_id}/series-schedule", response_model=SeriesScheduleOut)
def read_series_schedule(league_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if league.series_length_days is None:
        raise HTTPException(400, "Series schedules are only available for NHL leagues")
    # These refreshes are best effort. On Postgres a failed statement aborts the whole transaction, so each failure has
    # to be rolled back or every query after it (the commit and the read below) fails and the scoreboard shows nothing.
    try:
        from fantasy_sports.platform import auto_schedule
        auto_schedule.ensure_schedule(db, league)
    except Exception:
        logger.exception("ensure_schedule failed for league %s", league_id)
        db.rollback()
    try:
        from fantasy_sports.platform.nhl_daily_sync import auto_sync_league_scoreboard
        auto_sync_league_scoreboard(db, league_id)
    except Exception:
        logger.exception("scoreboard sync failed for league %s", league_id)
        db.rollback()
    try:
        db.commit()
    except Exception:
        logger.exception("commit after the scoreboard refresh failed for league %s", league_id)
        db.rollback()
    league = db.get(League, league_id)
    return _out(league, _league_series(db, league_id), db, viewer_user_id=user.id if user else None)



@router.get("/teams/{team_id}/series-schedule", response_model=TeamSeriesScheduleOut)
def read_team_series_schedule(team_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    try:
        from fantasy_sports.platform.nhl_daily_sync import auto_sync_league_scoreboard
        auto_sync_league_scoreboard(db, team.league_id)
        db.commit()
    except Exception:
        logger.exception("scoreboard sync failed for league %s", team.league_id)
        db.rollback()
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=team.league_id)}
    league = db.get(League, team.league_id)
    rows = _league_series(db, team.league_id)
    # Final scores follow the scoreboard's no-spoiler rule: shown once the owner has watched (AI vs AI games always are).
    shown = {g.id: g for s in _out(league, rows, db, viewer_user_id=user.id if user else None).series for g in s.daily_games}
    out = []
    for s in rows:
        if team_id not in (s.team_a_id, s.team_b_id):
            continue
        opp = s.team_b_id if s.team_a_id == team_id else s.team_a_id
        games = []
        for g in sorted(s.daily_games, key=lambda g: g.calendar_date):
            home = g.home_team_id == team_id
            v = shown.get(g.id)
            final = g.status == "FINAL" and v is not None
            visible = final and v.owner_watched
            games.append(TeamSeriesGameOut(
                daily_game_id=g.id, calendar_date=g.calendar_date, calendar_date_end=g.calendar_date_end,
                window_index=g.window_index, is_matchup_day=g.is_matchup_day, home_away="home" if home else "away",
                status=g.status, team_score=(v.home_score if home else v.away_score) if visible else None,
                opponent_score=(v.away_score if home else v.home_score) if visible else None,
                score_hidden=final and not visible))
        out.append(TeamSeriesOut(series_id=s.id, opponent_id=opp, opponent_name=names.get(opp, "Opponent"), start_date=s.start_date,
                                 end_date=s.end_date, total_days=s.total_days, status=s.status, games=games))
    return TeamSeriesScheduleOut(team_id=team_id, league_id=team.league_id, series=out)
