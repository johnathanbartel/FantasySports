"""
Progressive game simulator endpoints (contract and rules: platform/simulator/). Sport-agnostic: the routes
reveal a published script; they never score anything. Scripts are published by sport adapters through
service.publish_script, not over HTTP.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, current_user
from fantasy_sports.api.schemas import (
    SimEventOut, SimEventsOut, SimFinalOut, SimProgressRequest, SimStateOut, SimTeamOut,
)
from fantasy_sports.platform.db.models.core import SeriesDailyGame, SimulatorGame, User
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.simulator import contract, service

router = APIRouter(prefix="/simulator", tags=["simulator"])

_STATUS = {"NOT_STARTED": 409, "NOT_FINAL": 409, "ALREADY_PUBLISHED": 409, "GAME_STARTED": 409,
           "NOT_WATCHING": 409, "AHEAD_OF_GAME": 400, "BAD_CURSOR": 400, "REVEAL_REQUIRED": 403}


def _fail(e: service.SimulatorError) -> HTTPException:
    return HTTPException(_STATUS.get(e.code, 400), {"code": e.code, "message": str(e)})


def _game(db: Session, daily_game_id: int) -> SeriesDailyGame:
    game = db.get(SeriesDailyGame, daily_game_id)
    if game is None:
        raise HTTPException(404, "Daily game not found")
    return game


def _sim(db: Session, daily_game_id: int) -> SimulatorGame:
    _game(db, daily_game_id)
    sim = service.get_sim(db, daily_game_id)
    if sim is None:
        raise HTTPException(409, {"code": "NOT_PUBLISHED", "message": "No simulation has been published for this game"})
    return sim


def _state_out(view: service.StateView, game: SeriesDailyGame) -> SimStateOut:
    return SimStateOut(
        **view.__dict__, league_id=game.series.league_id, series_id=game.series_id, calendar_date=game.calendar_date,
        home_team=SimTeamOut(id=game.home_team_id, name=game.home_team.name),
        away_team=SimTeamOut(id=game.away_team_id, name=game.away_team.name))


@router.get("/contract")
def read_contract():
    """The contract version this server speaks and the event vocabulary."""
    return {"contract_version": contract.CONTRACT_VERSION, "supported_major": contract.SUPPORTED_MAJOR,
            "event_kinds": list(contract.EVENT_KINDS), "max_events": contract.MAX_EVENTS}


_LAST_SYNC: dict[int, float] = {}
SYNC_EVERY_SECONDS = 15


def _drive_window_engine(db: Session, game: SeriesDailyGame) -> None:
    """A game page that is open is what is watching the game, so it must also be what moves it: ingest the feed and let
    the window engine publish what is due, at most every SYNC_EVERY_SECONDS per league and server process. Without this
    a period waits for someone to open the league page. Never raises."""
    import logging
    import time
    if game.window_index is None or game.status == "FINAL":
        return
    league_id = game.series.league_id
    now = time.monotonic()
    if now - _LAST_SYNC.get(league_id, -1e9) < SYNC_EVERY_SECONDS:
        return
    _LAST_SYNC[league_id] = now
    try:
        from fantasy_sports.platform.nhl_daily_sync import auto_sync_league_scoreboard
        auto_sync_league_scoreboard(db, league_id)
    except Exception:
        logging.getLogger(__name__).exception("game page sync failed for league %s", league_id)
        db.rollback()


@router.get("/games/{daily_game_id}/state", response_model=SimStateOut)
def read_state(daily_game_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    game = _game(db, daily_game_id)
    _drive_window_engine(db, game)
    game = _game(db, daily_game_id)
    sim = service.get_sim(db, daily_game_id)
    if sim is None:
        return _state_out(service.unpublished_state(game), game)
    view = service.view_state(db, sim, user.id, service.utcnow())
    db.commit()
    return _state_out(view, game)


@router.get("/games/{daily_game_id}/live-box")
def read_live_box(daily_game_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Transparency view of an NHL window matchup: which real periods fell in which fantasy period and the credit earned
    in each so far. Read-only and deliberately unveiled (no reveal row needed, nothing is recorded for the viewer)."""
    game = _game(db, daily_game_id)
    from fantasy_sports.sports.hockey.simulator import live_box
    try:
        return live_box.live_box_score(db, game, service.utcnow())
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


@router.get("/games/{daily_game_id}/events", response_model=SimEventsOut)
def read_events(daily_game_id: int, after_seq: int = 0, limit: int = 200, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """Idempotent polling: the events after `after_seq` that this viewer may see. Repeating a request returns
    the same page; nothing is consumed server-side."""
    sim = _sim(db, daily_game_id)
    try:
        page, bound, more = service.view_events(db, sim, user.id, service.utcnow(), after_seq, limit)
    except service.SimulatorError as e:
        raise _fail(e)
    db.commit()
    return SimEventsOut(
        contract_version=sim.contract_version, daily_game_id=daily_game_id, status=sim.status, after_seq=after_seq,
        next_seq=page[-1].seq if page else after_seq, available_through=bound, has_more=more,
        events=[SimEventOut.model_validate(e) for e in page],
    )


def _transition(daily_game_id: int, db: Session, user: User, action) -> SimStateOut:
    sim = _sim(db, daily_game_id)
    now = service.utcnow()
    try:
        action(sim, now)
    except service.SimulatorError as e:
        db.rollback()
        raise _fail(e)
    db.commit()
    return _state_out(service.view_state(db, sim, user.id, now), sim.daily_game)


@router.post("/games/{daily_game_id}/watch", response_model=SimStateOut)
def watch_game(daily_game_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Open the game for you (live, or a replay of a finished game). Idempotent; never rewinds."""
    return _transition(daily_game_id, db, user, lambda sim, now: service.watch(db, sim, user.id, now))


@router.post("/games/{daily_game_id}/progress", response_model=SimStateOut)
def report_progress(daily_game_id: int, payload: SimProgressRequest, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Save how far you have watched (forward-only), so a reconnect resumes there."""
    return _transition(daily_game_id, db, user,
                       lambda sim, now: service.progress(db, sim, user.id, now, payload.cursor_seq))


@router.post("/games/{daily_game_id}/skip", response_model=SimStateOut)
def skip_to_box_score(daily_game_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Jump straight to the box score of a finished game. Idempotent; only affects you."""
    return _transition(daily_game_id, db, user, lambda sim, now: service.skip(db, sim, user.id, now))


@router.get("/games/{daily_game_id}/final", response_model=SimFinalOut)
def read_final(daily_game_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    sim = _sim(db, daily_game_id)
    try:
        view = service.final_view(db, sim, user.id, service.utcnow())
    except service.SimulatorError as e:
        raise _fail(e)
    db.commit()
    return SimFinalOut(
        contract_version=view.contract_version, daily_game_id=view.daily_game_id, status="FINAL",
        home_score=view.home_score, away_score=view.away_score, home_shots=view.home_shots,
        away_shots=view.away_shots, periods=view.periods, event_count=view.event_count,
        scoring_events=[SimEventOut.model_validate(e) for e in view.scoring_events], provenance=view.provenance,
    )


@router.post("/games/{daily_game_id}/start", response_model=SimStateOut)
def start_game(daily_game_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner only. Begins the real-time window of a published game. Idempotent."""
    game = _game(db, daily_game_id)
    assert_commissioner(db, user, game.series.league_id)
    if game.window_index is not None:
        raise HTTPException(409, {"code": "WINDOW_GAME", "message": "A window game opens by itself at the window's first kickoff"})
    sim = service.get_sim(db, daily_game_id)
    if sim is None:
        from fantasy_sports.sports.hockey.simulator import loader
        sim = loader.publish_game(db, game)
    service.start_game(db, sim, service.utcnow())
    db.commit()
    return _state_out(service.view_state(db, sim, user.id, service.utcnow()), game)


@router.post("/leagues/{league_id}/simulate-date")
def simulate_league_date(
    league_id: int,
    date: str | None = None,
    finish: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Commissioner only. Publishes and starts (or skips to final) all scheduled daily games for the league on that calendar date."""
    assert_commissioner(db, user, league_id)
    import datetime
    if date:
        try:
            target_date = datetime.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "Invalid date format, expected YYYY-MM-DD")
    else:
        try:
            from zoneinfo import ZoneInfo
            target_date = datetime.datetime.now(ZoneInfo("America/New_York")).date()
        except Exception:
            target_date = datetime.date.today()

    from fantasy_sports.platform.db.models.core import LeagueSeries
    from fantasy_sports.sports.hockey.simulator import loader

    daily_games = (
        db.query(SeriesDailyGame)
        .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
        .filter(LeagueSeries.league_id == league_id, SeriesDailyGame.calendar_date == target_date)
        .all()
    )
    if not daily_games:
        raise HTTPException(404, f"No daily games scheduled for date {target_date.isoformat()}")

    count = 0
    now_utc = service.utcnow()
    errors: list[str] = []
    from fantasy_sports.sports.hockey.simulator import window_engine

    for dg in daily_games:
        if dg.window_index is not None:      # window games are driven by the clock, not forced
            try:
                result = window_engine.tick(db, dg, now_utc)
                count += 1 if service.get_sim(db, dg.id) is not None else 0
                if finish:
                    errors.append(f"game {dg.id}: a window game cannot be finished early ({result.status})")
            except Exception as e:
                errors.append(str(e))
            continue
        sim = service.get_sim(db, dg.id)
        if sim is None:
            try:
                sim = loader.publish_game(db, dg)
            except Exception as e:
                errors.append(str(e))
                continue
        if dg.status == "SCHEDULED":
            service.start_game(db, sim, now_utc)
        if finish and dg.status != "FINAL":
            service.skip_game(db, sim)
        count += 1

    db.commit()
    return {
        "status": "ok",
        "date": target_date.isoformat(),
        "games_simulated": count,
        "message": errors[0] if (count == 0 and errors) else None,
        "errors": errors,
    }
