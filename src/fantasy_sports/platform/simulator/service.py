"""
Persistence and reveal logic for progressive games (contract: contract.py, arithmetic: state.py).

Lifecycle of a SimulatorGame: publish_script (SCHEDULED, immutable events stored) -> start_game (LIVE,
real-time window begins) -> FINAL once window_seconds have passed. Transitions are applied by sync(),
which is idempotent and is called before every read, so no scheduler is needed: whoever looks first after
a deadline performs the transition, and the result is the same regardless of who that is.

Spoiler veil: a viewer with no SimulatorReveal row sees only the lifecycle status. watch() or skip() opens
the game for that viewer alone; one viewer's choice never changes what another sees.

The mirrored columns on series_daily_games (status, scores, period, clock) follow the public live state
and only ever move forward; FINAL is written when the window closes. Standings read FINAL rows, so a
finished game's result is league-visible through standings regardless of individual veils - the veil
protects the live/replay experience, not the league table.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    LeagueSeries, SeriesDailyGame, SimulatorEvent, SimulatorGame, SimulatorReveal,
)
from fantasy_sports.platform.simulator import state as st
from fantasy_sports.platform.simulator.contract import CONTRACT_VERSION, ContractError, SimScript, validate_script

MAX_EVENT_PAGE = 500


class SimulatorError(ValueError):
    """A request that is valid HTTP but not valid for the game's current state."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def get_sim(db: Session, daily_game_id: int) -> SimulatorGame | None:
    return db.query(SimulatorGame).filter_by(daily_game_id=daily_game_id).first()


def shape_of(sim: SimulatorGame) -> st.GameShape:
    return st.GameShape(sim.periods, sim.period_seconds, sim.window_seconds)


def publish_script(db: Session, daily_game: SeriesDailyGame, script: SimScript) -> SimulatorGame:
    """Store a validated script for a game that has not started. Flushes, does not commit. Publishing the
    identical script again returns the existing record; a different script for the same game is refused,
    because events are immutable. Raises ContractError or SimulatorError."""
    validate_script(script)
    existing = get_sim(db, daily_game.id)
    if existing is not None:
        if existing.script_digest == script.digest():
            return existing
        raise SimulatorError("This game already has a different published script; events are immutable", "ALREADY_PUBLISHED")
    if daily_game.status != "SCHEDULED" or daily_game.home_score or daily_game.away_score:
        raise SimulatorError("Only a scheduled, unscored game can be given a script", "GAME_STARTED")
    sim = SimulatorGame(
        daily_game_id=daily_game.id, contract_version=script.contract_version, sport=script.sport,
        periods=script.periods, period_seconds=script.period_seconds, window_seconds=script.window_seconds,
        event_count=len(script.events), script_digest=script.digest(), provenance=dict(script.provenance),
        status=st.SCHEDULED,
    )
    db.add(sim)
    db.flush()
    for e in script.events:
        db.add(SimulatorEvent(
            simulator_game_id=sim.id, seq=e.seq, kind=e.kind, period=e.period, game_second=e.game_second,
            team_side=e.team_side, home_score=e.home_score, away_score=e.away_score, home_shots=e.home_shots,
            away_shots=e.away_shots, headline=e.headline, detail=dict(e.detail),
        ))
    db.flush()
    return sim


def _events(db: Session, sim: SimulatorGame) -> list[SimulatorEvent]:
    return db.query(SimulatorEvent).filter_by(simulator_game_id=sim.id).order_by(SimulatorEvent.seq).all()


def start_game(db: Session, sim: SimulatorGame, now: datetime.datetime) -> SimulatorGame:
    """Begin the real-time window. Idempotent: a started game keeps its original start time."""
    if sim.status == st.SCHEDULED:
        sim.status = st.LIVE
        sim.started_at = now
        dg = sim.daily_game
        dg.status = "LIVE_SIMULATING"
        if dg.series.status == "SCHEDULED":
            dg.series.status = "IN_PROGRESS"
        db.flush()
    return sim


def sync(db: Session, sim: SimulatorGame, now: datetime.datetime, events: list[SimulatorEvent] | None = None) -> None:
    """Apply any due transition and mirror the public state onto the daily-game row. Idempotent, forward-only,
    and flushes only; the caller commits."""
    if sim.status != st.LIVE or sim.started_at is None:
        return
    shape = shape_of(sim)
    events = events if events is not None else _events(db, sim)
    dg = sim.daily_game
    if st.is_due(shape, sim.started_at, now):
        sim.status = st.FINAL
        sim.finalized_at = st.finished_at(shape, sim.started_at)
        last = events[-1]
        dg.status = "FINAL"
        dg.home_score, dg.away_score = max(dg.home_score, last.home_score), max(dg.away_score, last.away_score)
        dg.current_period, dg.seconds_remaining = shape.periods, 0
        series = db.get(LeagueSeries, dg.series_id)
        db.flush()
        siblings = db.query(SeriesDailyGame).filter_by(series_id=series.id).all()
        series.status = "FINAL" if all(g.status == "FINAL" for g in siblings) else "IN_PROGRESS"
        db.flush()
        return
    elapsed = st.public_elapsed(shape, sim.status, sim.started_at, now)
    seq = st.count_through([e.game_second for e in events], elapsed)
    totals = st.totals_through(events, seq)
    period, remaining = st.clock_at(shape, elapsed)
    new = (max(dg.home_score, totals.home_score), max(dg.away_score, totals.away_score), period, min(remaining, 1200))
    if (dg.home_score, dg.away_score, dg.current_period, dg.seconds_remaining) != new:
        dg.home_score, dg.away_score, dg.current_period, dg.seconds_remaining = new
        db.flush()


def get_reveal(db: Session, sim: SimulatorGame, user_id: int) -> SimulatorReveal | None:
    return db.query(SimulatorReveal).filter_by(simulator_game_id=sim.id, user_id=user_id).first()


@dataclass
class StateView:
    daily_game_id: int
    published: bool
    contract_version: str
    status: str
    veiled: bool
    reveal_mode: str | None = None
    periods: int | None = None
    period_seconds: int | None = None
    window_seconds: int | None = None
    period: int | None = None
    seconds_remaining: int | None = None
    home_score: int | None = None
    away_score: int | None = None
    home_shots: int | None = None
    away_shots: int | None = None
    shown_seq: int = 0
    cursor_seq: int = 0
    has_more: bool = False
    is_complete: bool = False
    can_skip: bool = False
    provenance: dict | None = None


def _public_seq(sim: SimulatorGame, events: list[SimulatorEvent], now: datetime.datetime) -> int:
    if sim.status == st.FINAL:
        return len(events)
    if sim.status == st.SCHEDULED:
        return 0
    elapsed = st.public_elapsed(shape_of(sim), sim.status, sim.started_at, now)
    return st.count_through([e.game_second for e in events], elapsed)


def unpublished_state(daily_game: SeriesDailyGame) -> StateView:
    return StateView(daily_game_id=daily_game.id, published=False, contract_version=CONTRACT_VERSION,
                     status=st.SCHEDULED, veiled=True)


def view_state(db: Session, sim: SimulatorGame, user_id: int, now: datetime.datetime) -> StateView:
    """The viewer's scoreboard. Read-only apart from the idempotent sync."""
    events = _events(db, sim)
    sync(db, sim, now, events)
    shape = shape_of(sim)
    reveal = get_reveal(db, sim, user_id)
    mode = reveal.mode if reveal else None
    total = len(events)
    public = _public_seq(sim, events, now)
    if reveal is not None and mode == st.WATCHING and sim.status == st.LIVE and public > reveal.cursor_seq:
        reveal.cursor_seq = public  # a live viewer has now been shown everything so far; the cursor never rewinds
        db.flush()
    shown = st.shown_seq(sim.status, mode, reveal.cursor_seq if reveal else 0, public, total)
    view = StateView(
        daily_game_id=sim.daily_game_id, published=True, contract_version=sim.contract_version, status=sim.status,
        veiled=mode is None, reveal_mode=mode, periods=shape.periods, period_seconds=shape.period_seconds,
        window_seconds=shape.window_seconds, can_skip=sim.status == st.FINAL,
    )
    if mode is None:
        return view
    totals = st.totals_through(events, shown)
    if sim.status == st.LIVE:
        game_second = st.public_elapsed(shape, sim.status, sim.started_at, now)
    elif shown:
        game_second = events[shown - 1].game_second
    else:
        game_second = 0
    view.period, view.seconds_remaining = st.clock_at(shape, game_second)
    view.home_score, view.away_score = totals.home_score, totals.away_score
    view.home_shots, view.away_shots = totals.home_shots, totals.away_shots
    view.shown_seq = shown
    view.cursor_seq = reveal.cursor_seq
    view.has_more = sim.status == st.LIVE or shown < total
    view.is_complete = sim.status == st.FINAL and shown == total
    if view.is_complete:
        view.provenance = dict(sim.provenance)
    return view


def view_events(db: Session, sim: SimulatorGame, user_id: int, now: datetime.datetime, after_seq: int,
                limit: int) -> tuple[list[SimulatorEvent], int, bool]:
    """(events with after_seq < seq <= the viewer's bound, the bound, whether more are available now).
    Pure function of the stored data and `now`: asking twice returns the same page."""
    if after_seq < 0:
        raise SimulatorError("after_seq cannot be negative", "BAD_CURSOR")
    events = _events(db, sim)
    sync(db, sim, now, events)
    reveal = get_reveal(db, sim, user_id)
    bound = st.reveal_bound(sim.status, reveal.mode if reveal else None, _public_seq(sim, events, now), len(events))
    limit = max(1, min(limit, MAX_EVENT_PAGE))
    page = [e for e in events if after_seq < e.seq <= bound][:limit]
    more = bool(page) and page[-1].seq < bound
    if page and reveal is not None and reveal.mode == st.WATCHING and sim.status == st.LIVE             and page[-1].seq > reveal.cursor_seq:
        reveal.cursor_seq = page[-1].seq  # live events handed out count as shown
        db.flush()
    return page, bound, more


def watch(db: Session, sim: SimulatorGame, user_id: int, now: datetime.datetime) -> SimulatorReveal:
    """Open the game for this viewer in follow mode. Idempotent: never rewinds or downgrades an existing reveal."""
    sync(db, sim, now)
    reveal = get_reveal(db, sim, user_id)
    if reveal is None:
        if sim.status == st.SCHEDULED:
            raise SimulatorError("This game has not started", "NOT_STARTED")
        reveal = SimulatorReveal(simulator_game_id=sim.id, user_id=user_id, mode=st.WATCHING, cursor_seq=0)
        db.add(reveal)
        db.flush()
    return reveal


def progress(db: Session, sim: SimulatorGame, user_id: int, now: datetime.datetime, seq: int) -> SimulatorReveal:
    """Record that the viewer has now been shown events up to `seq`. Forward-only: a lower value is a no-op,
    so a retried or out-of-order request cannot rewind the cursor."""
    events = _events(db, sim)
    sync(db, sim, now, events)
    reveal = get_reveal(db, sim, user_id)
    if reveal is None:
        raise SimulatorError("Watch the game before reporting progress", "NOT_WATCHING")
    ceiling = st.reveal_bound(sim.status, reveal.mode, _public_seq(sim, events, now), len(events))
    if seq > ceiling:
        raise SimulatorError("That event is not available to you yet", "AHEAD_OF_GAME")
    if seq > reveal.cursor_seq:
        reveal.cursor_seq = seq
        db.flush()
    return reveal


def skip(db: Session, sim: SimulatorGame, user_id: int, now: datetime.datetime) -> SimulatorReveal:
    """Jump to the box score. Only a FINAL game can be skipped to; SKIPPED is terminal and idempotent."""
    events = _events(db, sim)
    sync(db, sim, now, events)
    if sim.status != st.FINAL:
        raise SimulatorError("The game is not final yet; watch it live or come back when it ends", "NOT_FINAL")
    reveal = get_reveal(db, sim, user_id)
    if reveal is None:
        reveal = SimulatorReveal(simulator_game_id=sim.id, user_id=user_id, mode=st.SKIPPED, cursor_seq=len(events))
        db.add(reveal)
    else:
        reveal.mode = st.SKIPPED
        reveal.cursor_seq = len(events)
    db.flush()
    return reveal


@dataclass
class FinalView:
    daily_game_id: int
    contract_version: str
    home_score: int
    away_score: int
    home_shots: int
    away_shots: int
    periods: int
    event_count: int
    scoring_events: list[SimulatorEvent]
    provenance: dict


def final_view(db: Session, sim: SimulatorGame, user_id: int, now: datetime.datetime) -> FinalView:
    """The box score. Needs a FINAL game and a viewer who has skipped to it or watched every event."""
    events = _events(db, sim)
    sync(db, sim, now, events)
    if sim.status != st.FINAL:
        raise SimulatorError("The game is not final yet", "NOT_FINAL")
    reveal = get_reveal(db, sim, user_id)
    if reveal is None or not (reveal.mode == st.SKIPPED or reveal.cursor_seq >= len(events)):
        raise SimulatorError("Watch the game or skip to the box score to see the final", "REVEAL_REQUIRED")
    last = events[-1]
    return FinalView(
        daily_game_id=sim.daily_game_id, contract_version=sim.contract_version, home_score=last.home_score,
        away_score=last.away_score, home_shots=last.home_shots, away_shots=last.away_shots, periods=sim.periods,
        event_count=len(events), scoring_events=[e for e in events if e.kind == "score"],
        provenance=dict(sim.provenance),
    )


__all__ = [
    "ContractError", "SimulatorError", "publish_script", "start_game", "sync", "view_state", "view_events",
    "watch", "progress", "skip", "final_view", "get_sim", "unpublished_state", "utcnow",
]
