"""
Pure state arithmetic for progressive games: no database, no clock, no randomness. Everything is a
function of (script shape, event list, started_at, now), so the same inputs always give the same view.
"""

from __future__ import annotations

import datetime
from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence

SCHEDULED, LIVE, FINAL = "SCHEDULED", "LIVE", "FINAL"
WATCHING, SKIPPED = "WATCHING", "SKIPPED"


@dataclass(frozen=True)
class GameShape:
    periods: int
    period_seconds: int
    window_seconds: int

    @property
    def total_seconds(self) -> int:
        return self.periods * self.period_seconds


@dataclass(frozen=True)
class Totals:
    home_score: int = 0
    away_score: int = 0
    home_shots: int = 0
    away_shots: int = 0


def public_elapsed(shape: GameShape, status: str, started_at: datetime.datetime | None, now: datetime.datetime) -> int:
    """Game seconds that have happened in real time. A live game plays out over window_seconds."""
    if status == SCHEDULED or started_at is None:
        return 0
    if status == FINAL:
        return shape.total_seconds
    real = (now - started_at).total_seconds()
    if real <= 0:
        return 0
    return min(shape.total_seconds, int(real * shape.total_seconds / shape.window_seconds))


def is_due(shape: GameShape, started_at: datetime.datetime, now: datetime.datetime) -> bool:
    return (now - started_at).total_seconds() >= shape.window_seconds


def finished_at(shape: GameShape, started_at: datetime.datetime) -> datetime.datetime:
    return started_at + datetime.timedelta(seconds=shape.window_seconds)


def count_through(game_seconds: Sequence[int], elapsed: int) -> int:
    """How many events (in seq order, non-decreasing game_second) have happened by `elapsed`."""
    return bisect_right(game_seconds, elapsed)


def clock_at(shape: GameShape, game_second: int) -> tuple[int, int]:
    """(period, seconds remaining in it) at an elapsed game second."""
    second = max(0, min(game_second, shape.total_seconds))
    period = max(1, -(-second // shape.period_seconds))
    return period, period * shape.period_seconds - second


def totals_through(events: Sequence, upto_seq: int) -> Totals:
    """Running totals after the last event with seq <= upto_seq (events are in seq order)."""
    if upto_seq <= 0 or not events:
        return Totals()
    e = events[min(upto_seq, len(events)) - 1]
    return Totals(e.home_score, e.away_score, e.home_shots, e.away_shots)


def reveal_bound(status: str, mode: str | None, public_seq: int, total: int) -> int:
    """The highest event seq the events endpoint may return to this viewer.
    No reveal row (mode None): 0, the spoiler veil. A live game: what has happened in real time. A finished
    game: everything, once the viewer has opted in (watching a replay, or skipped to the box score). A viewer
    who is only SKIPPED-in cannot see events of a game that is not yet final."""
    if mode is None or status == SCHEDULED:
        return 0
    if status == LIVE:
        return public_seq
    return total


def shown_seq(status: str, mode: str | None, cursor_seq: int, public_seq: int, total: int) -> int:
    """The seq the viewer's scoreboard currently shows: live viewers track real time, replay viewers their
    own cursor, skipped viewers the final state."""
    if mode is None or status == SCHEDULED:
        return 0
    if mode == SKIPPED:
        return total if status == FINAL else 0
    if status == LIVE:
        return public_seq
    return min(max(cursor_seq, 0), total)
