"""
Pick clock for snake drafts: live seconds-per-pick or slow/asynchronous hours-per-pick.

Everything is a function of (stored columns, an injected `now`); nothing here sleeps, schedules or reads the wall
clock, so a restart changes nothing and tests can replay any timeline.

Concepts
  * Active time: wall-clock time that is NOT inside the overnight window. A pick's allowance (`pick_seconds`) is
    spent in active time only, so an overnight window or a commissioner pause freezes the remaining time instead of
    consuming it.
  * Overnight window: a daily local-time range [start, end) (it may wrap midnight, e.g. 22:00-06:00) in a FIXED
    UTC offset chosen by the commissioner. There is no time-zone database: the offset does not follow daylight
    saving, so a league that observes DST must change the offset when the clocks change (documented limitation).
  * deadline_at (naive UTC) is the authoritative end of the current pick. It is NULL when untimed or paused.
    remaining_seconds is meaningful while paused (the active seconds left when the pause began).
  * Overdue: now >= deadline_at. The server then makes the pick itself (the router's process_overdue), and the next
    pick's clock starts at the missed deadline, not at `now`, so the result is the same however late it is applied.

Mode (reported, not stored): untimed (no pick_seconds), live (< 1 hour per pick), async (1 hour or more).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fantasy_sports.platform.db.models.core import Draft

MIN_PICK_SECONDS = 10
MAX_PICK_SECONDS = 7 * 86400
ASYNC_THRESHOLD_SECONDS = 3600
DAY = 86400
MAX_STEPS = 10000


class ClockError(ValueError):
    """Invalid clock configuration or an action that does not apply in the draft's current state."""


@dataclass(frozen=True)
class OvernightWindow:
    start_second: int   # local seconds since midnight
    end_second: int
    utc_offset_seconds: int

    def _local_second(self, dt: datetime.datetime) -> float:
        local = dt + datetime.timedelta(seconds=self.utc_offset_seconds)
        return (local - local.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()

    def contains(self, dt: datetime.datetime) -> bool:
        s = self._local_second(dt)
        if self.start_second < self.end_second:
            return self.start_second <= s < self.end_second
        return s >= self.start_second or s < self.end_second

    def seconds_until_end(self, dt: datetime.datetime) -> float:
        """For a moment inside the window: seconds until the window ends."""
        s = self._local_second(dt)
        if self.start_second < self.end_second or s < self.end_second:
            return self.end_second - s
        return DAY - s + self.end_second

    def seconds_until_start(self, dt: datetime.datetime) -> float:
        """For a moment outside the window: seconds until the next window starts."""
        s = self._local_second(dt)
        return (self.start_second - s) % DAY or DAY


def make_window(start_minute: int | None, end_minute: int | None, utc_offset_minutes: int) -> OvernightWindow | None:
    if start_minute is None and end_minute is None:
        return None
    if start_minute is None or end_minute is None:
        raise ClockError("An overnight pause needs both a start and an end time")
    for m in (start_minute, end_minute):
        if not 0 <= m < 1440:
            raise ClockError("Overnight times must be within one day")
    if start_minute == end_minute:
        raise ClockError("The overnight pause must start and end at different times")
    if not -14 * 60 <= utc_offset_minutes <= 14 * 60:
        raise ClockError("UTC offset must be between -14:00 and +14:00")
    return OvernightWindow(start_minute * 60, end_minute * 60, utc_offset_minutes * 60)


def next_active(dt: datetime.datetime, window: OvernightWindow | None) -> datetime.datetime:
    if window is None or not window.contains(dt):
        return dt
    return dt + datetime.timedelta(seconds=window.seconds_until_end(dt))


def add_active_seconds(start: datetime.datetime, seconds: float, window: OvernightWindow | None) -> datetime.datetime:
    """The instant at which `seconds` of active time have elapsed since `start`."""
    if window is None:
        return start + datetime.timedelta(seconds=seconds)
    t = next_active(start, window)
    remaining = float(seconds)
    for _ in range(MAX_STEPS):
        to_start = window.seconds_until_start(t)
        if remaining <= to_start:
            return t + datetime.timedelta(seconds=remaining)
        remaining -= to_start
        t = next_active(t + datetime.timedelta(seconds=to_start), window)
    raise ClockError("Pick allowance too long for this overnight window")


def active_seconds_between(a: datetime.datetime, b: datetime.datetime, window: OvernightWindow | None) -> float:
    """Active (non-overnight) seconds from a to b; 0 when b <= a."""
    if b <= a:
        return 0.0
    if window is None:
        return (b - a).total_seconds()
    total, t = 0.0, next_active(a, window)
    for _ in range(MAX_STEPS):
        if t >= b:
            return total
        end = min(b, t + datetime.timedelta(seconds=window.seconds_until_start(t)))
        total += (end - t).total_seconds()
        if end >= b:
            return total
        t = next_active(end, window)
    raise ClockError("Interval too long for this overnight window")


# ---- draft state ---------------------------------------------------------------------------------------------

def window_of(draft: Draft) -> OvernightWindow | None:
    return make_window(draft.overnight_start_minute, draft.overnight_end_minute, draft.utc_offset_minutes or 0)


def is_timed(draft: Draft) -> bool:
    return draft.pick_seconds is not None


def mode(draft: Draft) -> str:
    if draft.pick_seconds is None:
        return "untimed"
    return "async" if draft.pick_seconds >= ASYNC_THRESHOLD_SECONDS else "live"


def configure(draft: Draft, pick_seconds: int | None, overnight_start_minute: int | None = None,
              overnight_end_minute: int | None = None, utc_offset_minutes: int = 0) -> None:
    """Validate and store the clock configuration (before the draft starts)."""
    if pick_seconds is None:
        if overnight_start_minute is not None or overnight_end_minute is not None:
            raise ClockError("An overnight pause needs a pick time")
        return
    if not MIN_PICK_SECONDS <= pick_seconds <= MAX_PICK_SECONDS:
        raise ClockError(f"Pick time must be between {MIN_PICK_SECONDS} seconds and {MAX_PICK_SECONDS // 86400} days")
    make_window(overnight_start_minute, overnight_end_minute, utc_offset_minutes)  # validates
    draft.pick_seconds = pick_seconds
    draft.overnight_start_minute, draft.overnight_end_minute = overnight_start_minute, overnight_end_minute
    draft.utc_offset_minutes = utc_offset_minutes


def begin_pick(draft: Draft, started_at: datetime.datetime) -> None:
    """Start the clock for the pick now on the clock."""
    if not is_timed(draft):
        return
    draft.pick_started_at = started_at
    draft.deadline_at = add_active_seconds(started_at, draft.pick_seconds, window_of(draft))
    draft.remaining_seconds = None


def clear(draft: Draft) -> None:
    draft.deadline_at = None
    draft.pick_started_at = None
    draft.remaining_seconds = None
    draft.paused = False
    draft.paused_at = None


def remaining_seconds(draft: Draft, now: datetime.datetime) -> int | None:
    """Active seconds left on the current pick (whole seconds, rounded up); None if untimed or complete."""
    if not is_timed(draft) or draft.status != "in_progress":
        return None
    if draft.paused:
        return draft.remaining_seconds
    if draft.deadline_at is None:
        return None
    left = active_seconds_between(now, draft.deadline_at, window_of(draft))
    return int(-(-left // 1))


def is_overdue(draft: Draft, now: datetime.datetime) -> bool:
    return (is_timed(draft) and draft.status == "in_progress" and not draft.paused
            and draft.deadline_at is not None and now >= draft.deadline_at)


def pause(draft: Draft, now: datetime.datetime) -> bool:
    """Freeze the remaining time. Returns False (and changes nothing) if already paused. Call only when not overdue."""
    if not is_timed(draft):
        raise ClockError("This draft has no pick clock to pause")
    if draft.status != "in_progress":
        raise ClockError("The draft is not in progress")
    if draft.paused:
        return False
    draft.remaining_seconds = remaining_seconds(draft, now)
    draft.paused, draft.paused_at, draft.deadline_at = True, now, None
    return True


def resume(draft: Draft, now: datetime.datetime) -> bool:
    """Restart the clock with the frozen remaining time. Returns False if it was not paused."""
    if not is_timed(draft):
        raise ClockError("This draft has no pick clock to resume")
    if not draft.paused:
        return False
    draft.deadline_at = add_active_seconds(now, draft.remaining_seconds or 0, window_of(draft))
    draft.paused, draft.paused_at, draft.remaining_seconds = False, None, None
    return True


def hhmm(minute: int | None) -> str | None:
    return None if minute is None else f"{minute // 60:02d}:{minute % 60:02d}"


def parse_hhmm(text: str | None) -> int | None:
    if text is None or text == "":
        return None
    try:
        h, m = text.split(":")
        h, m = int(h), int(m)
    except ValueError:
        raise ClockError("Times must look like 22:00")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ClockError("Times must look like 22:00")
    return h * 60 + m
