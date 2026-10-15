"""
Wall-clock pacing for progressive games whose periods follow real (nominal) start times instead of a compressed
playback window.

A period is made of sub-periods ("segments", e.g. the real periods of the games a fantasy period is built from), each
with a nominal local start and the same nominal length. A segment's progress at time t is clamp((t - start) / length,
0, 1); a period's progress is the mean of its segments' progress. The clock reads "period k, MM:SS left" for the first
period that is not finished, with remaining time = period_seconds x (1 - progress). The game second a viewer is at is
(k - 1) x period_seconds + period_seconds x progress_k.

This is the ONE implementation of that arithmetic: a sport adapter uses it to place events and the service uses it to
pace the live game, so the clock and the events can never disagree. Only nominal times are used, never live event
timestamps. Pure: no database, no clock of its own, no randomness.

Stored in a script's provenance as {"timezone", "segment_seconds", "periods": [[iso start, ...], ...]}; the starts
are naive local times in `timezone` (as the NHL schedule is stored).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


EASTERN = "America/New_York"


def _nth_sunday(year: int, month: int, n: int) -> datetime.datetime:
    first = datetime.datetime(year, month, 1)
    return first + datetime.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _eastern_offset_utc(utc_naive: datetime.datetime) -> datetime.timedelta:
    """UTC offset of US Eastern at a UTC instant by the US rules (DST: 2nd Sunday of March 02:00 EST to 1st Sunday of
    November 02:00 EDT). Used only when the platform has no tz database (Windows without the tzdata package)."""
    start = _nth_sunday(utc_naive.year, 3, 2) + datetime.timedelta(hours=7)     # 02:00 EST
    end = _nth_sunday(utc_naive.year, 11, 1) + datetime.timedelta(hours=6)      # 02:00 EDT
    return datetime.timedelta(hours=-4 if start <= utc_naive < end else -5)


def utc_to_local(utc_naive: datetime.datetime, timezone: str) -> datetime.datetime:
    try:
        return utc_naive.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
    except Exception:
        if timezone != EASTERN:
            raise
        return utc_naive + _eastern_offset_utc(utc_naive)


def local_to_utc(local_naive: datetime.datetime, timezone: str) -> datetime.datetime:
    try:
        return local_naive.replace(tzinfo=ZoneInfo(timezone)).astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except Exception:
        if timezone != EASTERN:
            raise
        guess = local_naive - _eastern_offset_utc(local_naive + datetime.timedelta(hours=5))
        return local_naive - _eastern_offset_utc(guess)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


@dataclass(frozen=True)
class WallClock:
    timezone: str
    segment_seconds: int
    periods: tuple[tuple[datetime.datetime, ...], ...]

    # ---- (de)serialization -------------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"timezone": self.timezone, "segment_seconds": self.segment_seconds,
                "periods": [[s.isoformat() for s in starts] for starts in self.periods]}

    @classmethod
    def from_dict(cls, data: Mapping) -> "WallClock":
        return cls(str(data["timezone"]), int(data["segment_seconds"]),
                   tuple(tuple(datetime.datetime.fromisoformat(s) for s in starts) for starts in data["periods"]))

    # ---- arithmetic (t is a naive local time) --------------------------------------------------------------------
    def progress(self, period: int, t: datetime.datetime) -> float:
        starts = self.periods[period - 1]
        if not starts:
            return 0.0
        return sum(_clamp01((t - s).total_seconds() / self.segment_seconds) for s in starts) / len(starts)

    def clock(self, t: datetime.datetime, period_seconds: int) -> tuple[int, int]:
        """(period, seconds left in it): the first unfinished period, else the last one at 0:00."""
        for k in range(1, len(self.periods) + 1):
            p = self.progress(k, t)
            if p < 1.0:
                return k, int(round(period_seconds * (1.0 - p)))
        return len(self.periods), 0

    def game_second_at(self, t: datetime.datetime, period_seconds: int) -> int:
        period, left = self.clock(t, period_seconds)
        return (period - 1) * period_seconds + (period_seconds - left)

    def event_second(self, period: int, t: datetime.datetime, period_seconds: int) -> int:
        """Game second of an event at local time t inside `period`, kept strictly inside it (1 .. period_seconds - 1)."""
        inside = int(round(period_seconds * self.progress(period, t)))
        return (period - 1) * period_seconds + min(max(inside, 1), period_seconds - 1)

    # ---- real time -----------------------------------------------------------------------------------------------
    def local(self, utc_naive: datetime.datetime) -> datetime.datetime:
        return utc_to_local(utc_naive, self.timezone)

    def elapsed(self, utc_naive: datetime.datetime, period_seconds: int) -> int:
        return self.game_second_at(self.local(utc_naive), period_seconds)

    def is_finished(self, utc_naive: datetime.datetime) -> bool:
        t = self.local(utc_naive)
        return all(self.progress(k, t) >= 1.0 for k in range(1, len(self.periods) + 1))

    def end_utc(self) -> datetime.datetime:
        """When the last segment ends, as naive UTC."""
        last = max((s for starts in self.periods for s in starts), default=None)
        if last is None:
            raise ValueError("a wall clock needs at least one segment")
        return local_to_utc(last + datetime.timedelta(seconds=self.segment_seconds), self.timezone)


def from_provenance(provenance: Mapping | None) -> "WallClock | None":
    """The wall clock a script asks to be paced by, or None (compressed playback)."""
    data = (provenance or {}).get("wall_clock")
    if not data or not any(data.get("periods", [])):
        return None
    return WallClock.from_dict(data)
