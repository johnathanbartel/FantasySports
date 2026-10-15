"""
Chronological period division for one hockey matchup (docs/hockey_spec.md section 6.1).

A matchup is one fantasy window. Every starter's counting game contributes 3 real periods with NOMINAL start times
(P1 = kickoff, P2 = kickoff + 60 min, P3 = kickoff + 120 min). The real periods of BOTH sides are pooled, sorted by
nominal start (ties: game id, then asset id, then period number, so the result never depends on input order) and cut
into 3 consecutive chunks of N/3. Those chunks are fantasy periods 1, 2 and 3. N is a multiple of 3 (a full matchup
has 36; an empty slot only lowers it), so the chunks are always equal.

This module is the ONE place that turns a wall-clock time into a clock reading and a real-game second into a fantasy
game second, so the live clock and the event placement can never disagree:

  * real period progress at time t   = clamp((t - nominal_start) / 60 min, 0, 1)
  * fantasy period k progress at t   = the mean progress of the real periods in chunk k
  * clock display                    = "Period k - MM:SS left", remaining = 20:00 x (1 - progress)
  * an event s seconds into real period p has estimated wall time nominal_start(p) + (s / 1200) x 60 min, and its
    fantasy game second is (k - 1) x 1200 + 1200 x progress_k(that time), kept inside 1..1199 of period k
  * overtime (real game second >= 3600) belongs to the player's real P3, whose progress is 1: it sits at the end of
    its fantasy period, in its own relative order. Shootouts are never ingested (the scoring contract excludes them).

Only nominal times are used, never live event timestamps. Pure and offline: no database, no clock, no randomness.
"""

from __future__ import annotations

import dataclasses
import datetime
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from fantasy_sports.sports.hockey.simulator.model import (
    PERIOD_SECONDS, PERIODS, CountingGame, CreditLine, InputError, ShotAttempt,
)

REAL_PERIOD = datetime.timedelta(minutes=60)    # nominal wall length of one real period (intermission included)
REAL_PERIOD_SECONDS = 1200                      # game seconds in one real period
REGULATION_SECONDS = PERIODS * REAL_PERIOD_SECONDS
# Used when the inputs carry no kickoff data: every game "starts" at the same moment, so real Pn -> fantasy Pn.
FALLBACK_KICKOFF = datetime.datetime(2000, 1, 1, 19, 0)


@dataclass(frozen=True)
class RealPeriodSlot:
    asset_id: int
    game_id: int
    period: int                     # real period 1..3
    start: datetime.datetime        # nominal start
    fantasy_period: int             # 1..3 once divided


def is_overtime(real_second: int) -> bool:
    return real_second >= REGULATION_SECONDS


def real_period_of(real_second: int) -> int:
    """Real period 1..3 of a real game second; overtime belongs to period 3."""
    return min(max(int(real_second), 0) // REAL_PERIOD_SECONDS, PERIODS - 1) + 1


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


class Division:
    """The chronological division of one matchup. Build it with `Division.build`; read it with the methods below."""

    def __init__(self, slots: Sequence[RealPeriodSlot]):
        self.slots: tuple[RealPeriodSlot, ...] = tuple(slots)
        self._by_key = {(s.asset_id, s.period): s for s in self.slots}
        if len(self._by_key) != len(self.slots):
            raise InputError("a starter has two counting games in one matchup")
        self.chunks: tuple[tuple[RealPeriodSlot, ...], ...] = tuple(
            tuple(s for s in self.slots if s.fantasy_period == k) for k in range(1, PERIODS + 1))

    # ---- construction ------------------------------------------------------------------------------------------
    @classmethod
    def build(cls, games: Iterable[CountingGame]) -> "Division":
        pooled = []
        for g in games:
            for n in (1, 2, 3):
                pooled.append((g.kickoff_at + (n - 1) * REAL_PERIOD, g.game_id, g.asset_id, n))
        pooled.sort()
        total = len(pooled)
        if total % PERIODS:
            raise InputError("the pooled real periods must come in threes")
        size = total // PERIODS
        slots = [RealPeriodSlot(asset, game, n, start, (i // size) + 1 if size else 1)
                 for i, (start, game, asset, n) in enumerate(pooled)]
        return cls(slots)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping]) -> "Division":
        """Rebuild from the `slots` stored in a script's provenance (see `slot_rows`)."""
        return cls([RealPeriodSlot(r["asset_id"], r["game_id"], r["period"],
                                   datetime.datetime.fromisoformat(r["start"]), r["fantasy_period"]) for r in rows])

    def slot_rows(self) -> list[dict]:
        return [{"asset_id": s.asset_id, "game_id": s.game_id, "period": s.period, "start": s.start.isoformat(),
                 "fantasy_period": s.fantasy_period} for s in self.slots]

    @property
    def pooled(self) -> int:
        return len(self.slots)

    @property
    def chunk_size(self) -> int:
        return self.pooled // PERIODS

    # ---- the shared clock / placement function -----------------------------------------------------------------
    @staticmethod
    def real_progress(slot: RealPeriodSlot, t: datetime.datetime) -> float:
        return _clamp01((t - slot.start) / REAL_PERIOD)

    def progress(self, k: int, t: datetime.datetime) -> float:
        """Fantasy period k's progress at wall time t: the mean progress of its real periods."""
        chunk = self.chunks[k - 1]
        if not chunk:
            return 0.0
        return sum(self.real_progress(s, t) for s in chunk) / len(chunk)

    def clock(self, t: datetime.datetime) -> tuple[int, int]:
        """(fantasy period, seconds left in it) at wall time t: the first period that is not finished yet, else the
        last one at 0:00. Each matchup has its own division, hence its own clock."""
        for k in range(1, PERIODS + 1):
            if self.progress(k, t) < 1.0:
                return k, int(round(PERIOD_SECONDS * (1.0 - self.progress(k, t))))
        return PERIODS, 0

    def clock_label(self, t: datetime.datetime) -> str:
        period, left = self.clock(t)
        return f"Period {period} — {left // 60}:{left % 60:02d} left"

    def game_second_at(self, t: datetime.datetime) -> int:
        """The fantasy game second a viewer at wall time t is at (what the clock reading counts from)."""
        period, left = self.clock(t)
        return (period - 1) * PERIOD_SECONDS + (PERIOD_SECONDS - left)

    def slot(self, asset_id: int, real_second: int) -> RealPeriodSlot:
        try:
            return self._by_key[(asset_id, real_period_of(real_second))]
        except KeyError:
            raise InputError(f"asset {asset_id} has no counting game in this matchup's division") from None

    def place(self, asset_id: int, real_second: int) -> int:
        """Fantasy game second (1 .. 3599, strictly inside its fantasy period) of an event `real_second` seconds into
        the real game of `asset_id`'s counting game."""
        slot = self.slot(asset_id, real_second)
        k = slot.fantasy_period
        base = (k - 1) * PERIOD_SECONDS
        if is_overtime(real_second):                       # its real P3 is complete: the end of the fantasy period
            return base + PERIOD_SECONDS - 1
        offset = max(int(real_second), 0) - (slot.period - 1) * REAL_PERIOD_SECONDS
        when = slot.start + (offset / REAL_PERIOD_SECONDS) * REAL_PERIOD
        inside = int(round(PERIOD_SECONDS * self.progress(k, when)))
        return base + min(max(inside, 1), PERIOD_SECONDS - 1)

    def draw(self, rng) -> int:
        """A seeded fantasy game second for an event with no real clock (inverted, unattributed, tie-break): the
        period is drawn uniformly among the three fantasy periods, then a second strictly inside it. One rng call."""
        x = rng.randint(0, PERIODS * (PERIOD_SECONDS - 1) - 1)
        return (x // (PERIOD_SECONDS - 1)) * PERIOD_SECONDS + x % (PERIOD_SECONDS - 1) + 1


def fallback_counting_games(lines: Iterable[CreditLine], attempts: Iterable[ShotAttempt],
                            starters: Mapping[str, Sequence[tuple[int, str]]]) -> tuple[CountingGame, ...]:
    """Counting games for inputs that carry no kickoff data: one per starter, all at the same nominal start."""
    games: dict[int, CountingGame] = {}
    for side, people in starters.items():
        for asset_id, name in people:
            games[asset_id] = CountingGame(asset_id, name, side, 0, FALLBACK_KICKOFF)
    game_of = {item.asset_id: item.game_id for item in (*lines, *attempts) if item.game_id is not None}
    return tuple(dataclasses.replace(games[a], game_id=game_of.get(a, 0)) for a in sorted(games))


def division_table(division: Division, games: Iterable[CountingGame], lines: Iterable[CreditLine],
                   attempts: Iterable[ShotAttempt], teams: Mapping[str, str]) -> dict:
    """The box-score division table: per fantasy period, every real period assigned to it with the player, fantasy team,
    real game, real period number, nominal start, OT flag and the points earned in that real period. Sorted the way the
    division sorted them, so the table reads chronologically."""
    info = {g.asset_id: g for g in games}
    points: dict[tuple[int, int], float] = {}
    overtime: set[tuple[int, int]] = set()
    for line in lines:
        key = (line.asset_id, real_period_of(line.real_second))
        points[key] = points.get(key, 0.0) + line.points
        if is_overtime(line.real_second):
            overtime.add(key)
    for a in attempts:
        if is_overtime(a.real_second):
            overtime.add((a.asset_id, real_period_of(a.real_second)))
    periods = []
    for k, chunk in enumerate(division.chunks, start=1):
        rows = []
        for s in chunk:
            g = info[s.asset_id]
            rows.append({
                "asset_id": s.asset_id, "player": g.player_name, "side": g.side, "fantasy_team": teams.get(g.side, g.side),
                "game_id": s.game_id, "opponent": g.opponent, "game_date": g.kickoff_at.date().isoformat(),
                "real_period": s.period, "nominal_start": s.start.isoformat(),
                "ot": (s.asset_id, s.period) in overtime, "points": round(points.get((s.asset_id, s.period), 0.0), 6)})
        periods.append({"fantasy_period": k, "real_periods": len(rows), "rows": rows})
    return {"pooled": division.pooled, "chunk_size": division.chunk_size, "periods": periods,
            "slots": division.slot_rows()}
