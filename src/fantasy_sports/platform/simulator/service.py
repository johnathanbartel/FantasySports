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
import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fantasy_sports.platform import elimination
from fantasy_sports.platform.db.models.core import (
    LeagueSeries, SeriesDailyGame, SimulatorEvent, SimulatorGame, SimulatorReveal,
)
from fantasy_sports.platform.simulator import state as st
from fantasy_sports.platform.simulator import wallclock
from fantasy_sports.platform.simulator.contract import (
    CONTRACT_VERSION, ContractError, SimEventInput, SimScript, validate_script,
)

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
    return st.GameShape(sim.periods, sim.period_seconds, sim.window_seconds, wallclock.from_provenance(sim.provenance))


def publish_script(db: Session, daily_game: SeriesDailyGame, script: SimScript, partial: bool = False) -> SimulatorGame:
    """Store a validated script for a game that has not started. Flushes, does not commit. Publishing the
    identical script again returns the existing record; a different script for the same game is refused,
    because events are immutable. Raises ContractError or SimulatorError."""
    validate_script(script, partial=partial)
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
        published_periods=0 if partial else None, status=st.SCHEDULED,
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


def append_period(db: Session, sim: SimulatorGame, period: int, events: list, provenance: dict) -> SimulatorGame:
    """Append the events of the next period to a game that is published period by period. Published events are never
    changed: the new events must continue the old ones (same validation as a whole script, as a prefix until the last
    period, whole after it), `period` must be exactly the next one, and the digest chains the old digest to the new
    events so a different history can never share one. Flushes, does not commit. Raises ContractError or SimulatorError."""
    if sim.published_periods is None:
        raise SimulatorError("This game was published whole; periods cannot be appended", "NOT_PROGRESSIVE")
    if period != sim.published_periods + 1 or period > sim.periods:
        raise SimulatorError(f"Period {period} is not the next one to publish (published: {sim.published_periods})", "BAD_PERIOD")
    old = _events(db, sim)
    new = [SimEventInput(seq=len(old) + i + 1, kind=e.kind, period=e.period, game_second=e.game_second, team_side=e.team_side,
                         home_score=e.home_score, away_score=e.away_score, home_shots=e.home_shots, away_shots=e.away_shots,
                         headline=e.headline, detail=dict(e.detail)) for i, e in enumerate(events)]
    last = period == sim.periods
    prefix = SimScript(
        contract_version=sim.contract_version, sport=sim.sport, periods=sim.periods, period_seconds=sim.period_seconds,
        window_seconds=sim.window_seconds,
        events=tuple(SimEventInput(e.seq, e.kind, e.period, e.game_second, e.team_side, e.home_score, e.away_score,
                                   e.home_shots, e.away_shots, e.headline, dict(e.detail or {})) for e in old) + tuple(new),
        provenance=provenance)
    validate_script(prefix, partial=not last)
    for e in new:
        db.add(SimulatorEvent(
            simulator_game_id=sim.id, seq=e.seq, kind=e.kind, period=e.period, game_second=e.game_second,
            team_side=e.team_side, home_score=e.home_score, away_score=e.away_score, home_shots=e.home_shots,
            away_shots=e.away_shots, headline=e.headline, detail=dict(e.detail)))
    sim.event_count = len(old) + len(new)
    sim.published_periods = period
    sim.provenance = dict(provenance)
    chain = json.dumps([sim.script_digest, [e.to_dict() for e in new]], sort_keys=True, separators=(",", ":"))
    sim.script_digest = hashlib.sha256(chain.encode("utf-8")).hexdigest()
    db.flush()
    return sim


def update_provenance(db: Session, sim: SimulatorGame, provenance: dict) -> None:
    """Replace the provenance of a game that is published period by period (state that is not an event, such as the
    frozen lineup and the projected clock). Never touches events."""
    sim.provenance = dict(provenance)
    db.flush()


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
    if sim.published_periods is not None:       # published period by period: final when the last period is out, not by time
        if sim.published_periods >= sim.periods:
            _finalize(db, sim, events, now)
            return
    elif st.is_due(shape, sim.started_at, now):
        _finalize(db, sim, events, st.finished_at(shape, sim.started_at))
        return
    elapsed = st.public_elapsed(shape, sim.status, sim.started_at, now)
    seq = st.count_through([e.game_second for e in events], elapsed)
    totals = st.totals_through(events, seq)
    period, remaining = st.clock_at(shape, elapsed)
    new = (max(dg.home_score, totals.home_score), max(dg.away_score, totals.away_score), period, min(remaining, 1200))
    if (dg.home_score, dg.away_score, dg.current_period, dg.seconds_remaining) != new:
        dg.home_score, dg.away_score, dg.current_period, dg.seconds_remaining = new
        db.flush()


def _finalize(db: Session, sim: SimulatorGame, events: list[SimulatorEvent], at: datetime.datetime) -> None:
    shape = shape_of(sim)
    dg = sim.daily_game
    sim.status = st.FINAL
    sim.finalized_at = at
    last = events[-1]
    dg.status = "FINAL"
    dg.home_score, dg.away_score = max(dg.home_score, last.home_score), max(dg.away_score, last.away_score)
    dg.current_period, dg.seconds_remaining = shape.periods, 0
    series = db.get(LeagueSeries, dg.series_id)
    db.flush()
    siblings = db.query(SeriesDailyGame).filter_by(series_id=series.id).all()
    series.status = "FINAL" if all(g.status == "FINAL" for g in siblings) else "IN_PROGRESS"
    db.flush()
    elimination.recalculate_league(db, series.league_id)          # a finished game can prove a team out of the playoffs


def reset_simulation(db: Session, daily_game: SeriesDailyGame) -> bool:
    """Void a game's simulation completely so it can be simulated again from scratch: its events, every viewer's progress and the
    simulation record are deleted and the daily game goes back to SCHEDULED with a blank scoreboard. Published events are immutable
    in normal use (the ORM refuses to change them), so this is a deliberate bulk delete, only for the commissioner's repair of a game
    that was simulated from a wrong lineup. Flushes, does not commit. Returns whether there was a simulation to void."""
    sim = get_sim(db, daily_game.id)
    db.flush()
    if sim is not None:
        db.query(SimulatorReveal).filter_by(simulator_game_id=sim.id).delete(synchronize_session=False)
        db.query(SimulatorEvent).filter_by(simulator_game_id=sim.id).delete(synchronize_session=False)
        db.query(SimulatorGame).filter_by(id=sim.id).delete(synchronize_session=False)
    db.expire_all()
    dg = db.get(SeriesDailyGame, daily_game.id)
    dg.status, dg.home_score, dg.away_score, dg.current_period, dg.seconds_remaining = "SCHEDULED", 0, 0, 0, 1200
    db.flush()
    series = db.get(LeagueSeries, dg.series_id)
    siblings = db.query(SeriesDailyGame).filter_by(series_id=series.id).all()
    series.status = "SCHEDULED" if all(g.status == "SCHEDULED" for g in siblings) else "IN_PROGRESS"
    db.flush()
    elimination.recalculate_league(db, series.league_id, clear_stale=True)     # the voided result may have been part of a proof
    return sim is not None


def finalize_now(db: Session, sim: SimulatorGame, now: datetime.datetime) -> None:
    """Close a live game ahead of its schedule (e.g. every real game is already final). Idempotent."""
    if sim.status == st.LIVE:
        _finalize(db, sim, _events(db, sim), now)


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
    # Games published period by period (the NHL windows): the viewer is never veiled; the clock is public and the score is
    # whatever the viewer has watched. `period_phases` has one entry per period (see period_phases()).
    progressive: bool = False
    published_periods: int | None = None
    period_phases: list | None = None


def is_progressive(sim: SimulatorGame) -> bool:
    """Published period by period (the NHL windows). Such a game has no spoiler veil: the clock is public, the score is
    only what the viewer has watched, and highlights are watched one published period at a time, in order."""
    return sim.published_periods is not None


def period_marks(events: list[SimulatorEvent]) -> dict[int, int]:
    """period -> seq of the last event whose period it is (its period_end, or game_end for the last). The opening
    period_start of the next period is published with the previous one but belongs to the next period."""
    marks: dict[int, int] = {}
    for e in events:
        marks[e.period] = max(marks.get(e.period, 0), e.seq)
    return marks


def watch_bound(sim: SimulatorGame, events: list[SimulatorEvent], cursor: int) -> int:
    """The highest seq a viewer of a progressive game may fetch: the end of the first published period they have not yet
    watched (so they can watch it, and replay every earlier one), or everything published when all are watched."""
    marks = period_marks(events)
    for k in range(1, (sim.published_periods or 0) + 1):
        if marks.get(k, 0) > cursor:
            return marks[k]
    return len(events)


def live_shots(sim: SimulatorGame, events: list[SimulatorEvent], now: datetime.datetime) -> tuple[int, int]:
    """(home, away) shots on goal as the viewer may see them: what the published events add up to, plus the shots the
    script is known to be about to publish for later periods whose clock has already passed them (provenance
    "live_shots" = [[side, game second], ...], kept by the sport adapter). Shots are not a spoiler; scores are."""
    home = events[-1].home_shots if events else 0
    away = events[-1].away_shots if events else 0
    shape = shape_of(sim)
    clock = shape.wall_clock
    if sim.status != st.LIVE or clock is None:
        return home, away
    published = sim.published_periods or 0
    local = clock.local(now)
    progress: dict[int, float] = {}
    for side, second in (sim.provenance or {}).get("live_shots") or []:
        k = int(second) // shape.period_seconds + 1
        if k <= published or k > shape.periods:
            continue
        if k not in progress:
            progress[k] = clock.progress(k, local)
        if second <= (k - 1) * shape.period_seconds + shape.period_seconds * progress[k]:
            if side == "home":
                home += 1
            else:
                away += 1
    return home, away


def period_phases(sim: SimulatorGame, events: list[SimulatorEvent], cursor: int, now: datetime.datetime) -> list[dict]:
    """One entry per period: upcoming / in_progress (live, with the time left) / simulating (the real periods are over
    and the simulation has not published it yet: the intermission) / ready (published, not watched) / watched."""
    shape = shape_of(sim)
    marks = period_marks(events)
    published = sim.published_periods or 0
    clock = shape.wall_clock
    local = clock.local(now) if clock is not None and sim.status == st.LIVE else None
    sim_progress = (sim.provenance or {}).get("sim_progress") or {}
    out = []
    for k in range(1, shape.periods + 1):
        if k <= published:
            last = marks.get(k, 0)
            out.append({"period": k, "phase": "watched" if cursor >= last else "ready", "seconds_remaining": 0, "last_seq": last})
            continue
        progress = clock.progress(k, local) if local is not None else (1.0 if sim.status == st.FINAL else 0.0)
        if progress >= 1.0:
            out.append({"period": k, "phase": "simulating", "seconds_remaining": 0, "last_seq": 0,
                        "percent": int(sim_progress.get(str(k), 0)), "waiting_for": published + 1 if k > published + 1 else None})
        elif progress > 0.0:
            out.append({"period": k, "phase": "in_progress", "seconds_remaining": int(round(shape.period_seconds * (1.0 - progress))), "last_seq": 0})
        else:
            out.append({"period": k, "phase": "upcoming", "seconds_remaining": shape.period_seconds, "last_seq": 0})
    return out


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


def _progressive_state(sim: SimulatorGame, shape: st.GameShape, events: list[SimulatorEvent], reveal: SimulatorReveal | None,
                       now: datetime.datetime) -> StateView:
    total = len(events)
    cursor = min(reveal.cursor_seq, total) if reveal else 0
    if reveal is not None and reveal.mode == st.SKIPPED and sim.status == st.FINAL:
        cursor = total
    phases = period_phases(sim, events, cursor, now)
    view = StateView(
        daily_game_id=sim.daily_game_id, published=True, contract_version=sim.contract_version, status=sim.status,
        veiled=False, reveal_mode=reveal.mode if reveal else None, periods=shape.periods, period_seconds=shape.period_seconds,
        window_seconds=shape.window_seconds, progressive=True, published_periods=sim.published_periods, period_phases=phases)
    if sim.status == st.FINAL:
        view.period, view.seconds_remaining = shape.periods, 0
    elif shape.wall_clock is not None:
        view.period, view.seconds_remaining = shape.wall_clock.clock(shape.wall_clock.local(now), shape.period_seconds)
    else:
        view.period, view.seconds_remaining = st.clock_at(shape, st.public_elapsed(shape, sim.status, sim.started_at, now))
    view.shown_seq = cursor
    view.cursor_seq = reveal.cursor_seq if reveal else 0
    view.home_shots, view.away_shots = live_shots(sim, events, now)       # shots build up with the clock; they are no spoiler
    if cursor > 0:                     # the score is hidden (the client prints "?") until a period has been watched
        totals = st.totals_through(events, cursor)
        view.home_score, view.away_score = totals.home_score, totals.away_score
    view.can_skip = any(p["phase"] == "ready" for p in phases) or (sim.status == st.FINAL and cursor < total)
    view.has_more = sim.status == st.LIVE or cursor < total
    view.is_complete = sim.status == st.FINAL and cursor == total
    if view.is_complete:
        view.provenance = dict(sim.provenance)
    return view


def view_state(db: Session, sim: SimulatorGame, user_id: int, now: datetime.datetime) -> StateView:
    """The viewer's scoreboard. Read-only apart from the idempotent sync."""
    events = _events(db, sim)
    sync(db, sim, now, events)
    shape = shape_of(sim)
    reveal = get_reveal(db, sim, user_id)
    mode = reveal.mode if reveal else None
    total = len(events)
    public = _public_seq(sim, events, now)
    if is_progressive(sim) and sim.status != st.SCHEDULED:
        return _progressive_state(sim, shape, events, reveal, now)
    if reveal is not None and mode == st.WATCHING and sim.status == st.LIVE and public > reveal.cursor_seq:
        reveal.cursor_seq = public  # a live viewer has now been shown everything so far; the cursor never rewinds
        db.flush()
    shown = st.shown_seq(sim.status, mode, reveal.cursor_seq if reveal else 0, public, total)
    view = StateView(
        daily_game_id=sim.daily_game_id, published=True, contract_version=sim.contract_version, status=sim.status,
        veiled=mode is None, reveal_mode=mode, periods=shape.periods, period_seconds=shape.period_seconds,
        window_seconds=shape.window_seconds,
        can_skip=sim.status == st.FINAL or (
            sim.status == st.LIVE and (st.public_elapsed(shape, sim.status, sim.started_at, now) // shape.period_seconds) > 0
        ),
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
    if is_progressive(sim) and bound:
        bound = min(bound, watch_bound(sim, events, reveal.cursor_seq if reveal else 0))
    limit = max(1, min(limit, MAX_EVENT_PAGE))
    page = [e for e in events if after_seq < e.seq <= bound][:limit]
    more = bool(page) and page[-1].seq < bound
    if (page and reveal is not None and reveal.mode == st.WATCHING and sim.status == st.LIVE and not is_progressive(sim)
            and page[-1].seq > reveal.cursor_seq):
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
    if is_progressive(sim) and ceiling:
        ceiling = min(ceiling, watch_bound(sim, events, reveal.cursor_seq))
    if seq > ceiling:
        raise SimulatorError("That event is not available to you yet", "AHEAD_OF_GAME")
    if seq > reveal.cursor_seq:
        reveal.cursor_seq = seq
        db.flush()
    return reveal


def skip(db: Session, sim: SimulatorGame, user_id: int, now: datetime.datetime) -> SimulatorReveal:
    """Jump to the box score / latest completed period. When FINAL, skips to end. When LIVE, skips to latest completed period."""
    events = _events(db, sim)
    sync(db, sim, now, events)
    shape = shape_of(sim)
    if is_progressive(sim) and sim.status == st.LIVE:
        # Reveal every period published so far (not a box score: the game is not over). The viewer stays in follow mode.
        target_seq = period_marks(events).get(sim.published_periods or 0, 0)
        if target_seq <= 0:
            raise SimulatorError("No period has been simulated yet", "NOT_FINAL")
        reveal = get_reveal(db, sim, user_id)
        if reveal is None:
            reveal = SimulatorReveal(simulator_game_id=sim.id, user_id=user_id, mode=st.WATCHING, cursor_seq=target_seq)
            db.add(reveal)
        elif target_seq > reveal.cursor_seq:
            reveal.cursor_seq = target_seq
        db.flush()
        return reveal
    if sim.status == st.FINAL:
        target_seq = len(events)
    elif sim.status == st.LIVE:
        elapsed = st.public_elapsed(shape, sim.status, sim.started_at, now)
        completed_periods = elapsed // shape.period_seconds
        if completed_periods <= 0:
            raise SimulatorError("No period has completed yet; watch live highlights", "NOT_FINAL")
        target_game_second = completed_periods * shape.period_seconds
        target_seq = st.count_through([e.game_second for e in events], target_game_second)
    else:
        raise SimulatorError("This game has not started", "NOT_STARTED")

    reveal = get_reveal(db, sim, user_id)
    if reveal is None:
        reveal = SimulatorReveal(simulator_game_id=sim.id, user_id=user_id, mode=st.SKIPPED, cursor_seq=target_seq)
        db.add(reveal)
    else:
        reveal.mode = st.SKIPPED
        if target_seq > reveal.cursor_seq:
            reveal.cursor_seq = target_seq
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
    "watch", "progress", "skip", "final_view", "get_sim", "unpublished_state", "utcnow", "finalize_now",
    "append_period", "update_provenance", "reset_simulation",
]
