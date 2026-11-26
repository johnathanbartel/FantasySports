"""NCAAB fantasy windows: cut a season's real schedule into head-to-head windows.

A college basketball league plays one fantasy game per WINDOW, and the windows are cut from the real schedule
rather than from a fixed weekly shape.  This module chooses the cut points and reports the trade-off between
the number of windows and two costs pulling in opposite directions:

* **Empty starter slots** (minimized).  A 10-player roster has two players per starter position, on different
  teams.  A starter slot is empty in a window when neither of its two players' teams plays in it.  With ``p`` the
  share of Division I teams that play at least once in the window, the expected number of empty starter slots in
  that window is ``5 * (1 - p) ** 2``.  The season's cost is the sum over its windows: the "expected empty
  slot-windows per roster per season".
* **Surplus games** (reported, not minimized).  A surplus game is a team's second, third, ... game inside one
  window.  With ``s`` the average over Division I teams of ``max(0, games in the window - 1)``, a 10-player roster
  with every player on a different team expects ``10 * s`` surplus games in that window.  The season's surplus is
  the sum over its windows.

More windows mean more empty slots; fewer windows mean more surplus games.  Neither number is fixed by the
platform: :func:`window_curve` reports the curve for a range of window counts and the season (or the users)
picks a point on it.

Time conventions
----------------
* Fantasy time is US Eastern, as naive ``datetime`` values.  The machine has no tz database, so
  :func:`to_eastern` applies the US daylight-saving rules by hand: DST starts the second Sunday in March at
  2am EST (07:00 UTC) and ends the first Sunday in November at 2am EDT (06:00 UTC).
* A fantasy day starts at 4am Eastern, so a late West Coast tip stays with the previous game day.
* Window boundaries are instants in Eastern time.  A window runs from one boundary up to, but not including,
  the next boundary, and a game belongs to the window that contains its start time.
* The grid of candidate boundaries is counted from the season start (a Monday 4am Eastern) in steps of
  ``step_hours``; the season end is always the last grid point.

Pure: standard library only, no database, no network, deterministic.  Loading the saved schedule files lives
in the scripts (``scripts/ncaab_window_curve.py``), not here.
"""

from __future__ import annotations

import bisect
import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

STARTER_SLOTS = 5      # PG, SG, SF, PF, C
ROSTER_SIZE = 10       # 5 starters + 5 bench
COST_DECIMALS = 9      # plans whose costs agree to this many decimals are a tie (see optimize_windows)
FANTASY_DAY_START_HOUR = 4


# ---------------------------------------------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------------------------------------------

def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(days=7 * (n - 1))


def dst_bounds_utc(year: int) -> tuple[dt.datetime, dt.datetime]:
    """(start, end) of US daylight-saving time in ``year`` as naive UTC instants."""
    start = dt.datetime.combine(_nth_sunday(year, 3, 2), dt.time(7))   # 2:00 EST = 07:00 UTC
    end = dt.datetime.combine(_nth_sunday(year, 11, 1), dt.time(6))    # 2:00 EDT = 06:00 UTC
    return start, end


def to_eastern(utc_naive: dt.datetime) -> dt.datetime:
    """Naive UTC datetime -> naive US Eastern datetime (EDT = UTC-4 inside DST, EST = UTC-5 outside)."""
    if utc_naive.tzinfo is not None:
        raise ValueError("to_eastern takes a naive UTC datetime")
    start, end = dst_bounds_utc(utc_naive.year)
    return utc_naive + dt.timedelta(hours=-4 if start <= utc_naive < end else -5)


def parse_utc_iso(value: str) -> dt.datetime:
    """'2023-11-06T16:00:00.000Z' (or with an explicit offset) -> naive UTC datetime."""
    aware = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if aware.tzinfo is None:
        return aware
    return (aware - aware.utcoffset()).replace(tzinfo=None)


def fantasy_day(eastern: dt.datetime) -> dt.date:
    """The fantasy game day of an Eastern instant: days start at 4am, so 1am Sunday belongs to Saturday."""
    return (eastern - dt.timedelta(hours=FANTASY_DAY_START_HOUR)).date()


# ---------------------------------------------------------------------------------------------------------------
# Input and grid
# ---------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class GameRow:
    """One team's appearance in one game: a game between two Division I teams gives two rows."""
    team_id: int
    start_utc: dt.datetime   # naive UTC


def build_grid(start_et: dt.datetime, end_et: dt.datetime, step_hours: int = 4) -> list[dt.datetime]:
    """Candidate boundaries: ``start_et``, ``start_et + step``, ... and always ``end_et`` as the last point.

    ``start_et`` is the season start (a Monday 4am Eastern).  If the span is not a whole number of steps, the
    last step is shorter.
    """
    if step_hours <= 0:
        raise ValueError("step_hours must be positive")
    if end_et <= start_et:
        raise ValueError("end_et must be after start_et")
    step = dt.timedelta(hours=step_hours)
    grid = []
    t = start_et
    while t < end_et:
        grid.append(t)
        t += step
    grid.append(end_et)
    return grid


def _hours(a: dt.datetime, b: dt.datetime) -> float:
    return (b - a).total_seconds() / 3600.0


# ---------------------------------------------------------------------------------------------------------------
# Coverage table
# ---------------------------------------------------------------------------------------------------------------

@dataclass
class CoverageTable:
    """Per candidate window [grid[a], grid[b]) with length <= max_hours: share of teams that play, team-games
    per team, surplus per team.  Rows are indexed by ``a``; column ``b - a - 1`` holds window (a, b)."""
    grid: list[dt.datetime]
    n_teams: int
    max_hours: float
    share: list[list[float]] = field(repr=False)
    games: list[list[float]] = field(repr=False)
    surplus: list[list[float]] = field(repr=False)

    @property
    def last(self) -> int:
        return len(self.grid) - 1

    def has(self, a: int, b: int) -> bool:
        return 0 <= a < b <= self.last and b - a <= len(self.share[a])

    def cell(self, a: int, b: int) -> tuple[float, float, float]:
        """(share playing, team-games per team, surplus per team) for window [grid[a], grid[b])."""
        if not self.has(a, b):
            raise KeyError(f"window ({a}, {b}) is not in the table (longer than max_hours?)")
        k = b - a - 1
        return self.share[a][k], self.games[a][k], self.surplus[a][k]


def coverage_table(rows: Iterable[GameRow], team_ids: Iterable[int], grid: Sequence[dt.datetime],
                   max_hours: float = 336) -> CoverageTable:
    """Coverage of every candidate window up to ``max_hours`` long.

    Rows whose team is not in ``team_ids`` and rows outside [grid[0], grid[-1]) are ignored.  For each start
    point ``a`` the end ``b`` sweeps upward, adding the games of one grid step at a time, so the whole table
    costs one pass over the games per start point.  Pairs longer than ``max_hours`` are never computed.
    """
    teams = sorted(set(team_ids))
    if not teams:
        raise ValueError("team_ids is empty")
    index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    grid = list(grid)
    last = len(grid) - 1

    # Bucket each game into the grid step that contains its Eastern start.
    bucket: list[list[int]] = [[] for _ in range(last)]
    lo, hi = grid[0], grid[-1]
    for r in rows:
        i = index.get(r.team_id)
        if i is None:
            continue
        et = to_eastern(r.start_utc)
        if not (lo <= et < hi):
            continue
        step = bisect.bisect_right(grid, et) - 1
        bucket[step].append(i)

    share: list[list[float]] = []
    games: list[list[float]] = []
    surplus: list[list[float]] = []
    for a in range(last):
        counts = [0] * n
        covered = 0
        total = 0
        s_row: list[float] = []
        g_row: list[float] = []
        u_row: list[float] = []
        b = a + 1
        while b <= last and _hours(grid[a], grid[b]) <= max_hours + 1e-9:
            for i in bucket[b - 1]:
                if counts[i] == 0:
                    covered += 1
                counts[i] += 1
                total += 1
            s_row.append(covered / n)
            g_row.append(total / n)
            u_row.append((total - covered) / n)   # sum of max(0, games - 1) = games - teams that played
            b += 1
        share.append(s_row)
        games.append(g_row)
        surplus.append(u_row)
    share.append([])
    games.append([])
    surplus.append([])
    return CoverageTable(grid=grid, n_teams=n, max_hours=max_hours, share=share, games=games, surplus=surplus)


# ---------------------------------------------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------------------------------------------

def window_cost(share: float, starters: int = STARTER_SLOTS) -> float:
    """Expected empty starter slots in a window where ``share`` of teams play (two players per slot)."""
    return starters * (1.0 - share) ** 2


@dataclass
class WindowStats:
    start: dt.datetime
    end: dt.datetime
    hours: float
    coverage: float              # share of teams with at least one game
    team_games: float            # average games per team
    surplus_per_team: float      # average max(0, games - 1)
    expected_empty: float        # starters * (1 - coverage) ** 2
    expected_surplus: float      # roster_size * surplus_per_team

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "hours": self.hours,
                "coverage": self.coverage, "team_games": self.team_games,
                "surplus_per_team": self.surplus_per_team, "expected_empty": self.expected_empty,
                "expected_surplus": self.expected_surplus}


@dataclass
class WindowPlan:
    boundaries: list[dt.datetime]   # n_windows + 1 instants, first = season start, last = season end
    cost: float                     # expected empty slot-windows per roster per season
    surplus: float                  # expected surplus games per roster per season
    per_window: list[WindowStats]

    def to_dict(self) -> dict:
        return {"boundaries": [b.isoformat() for b in self.boundaries], "cost": self.cost,
                "surplus": self.surplus, "per_window": [w.to_dict() for w in self.per_window]}


class _Solver:
    """Suffix dynamic program: best[k][i] = least cost to cover [grid[i], end) with exactly k windows.

    One table serves every window count up to ``k_max``, so a curve over many counts is solved once.
    """

    def __init__(self, cov: CoverageTable, k_max: int, min_hours: float, max_hours: float, starters: int):
        if max_hours > cov.max_hours + 1e-9:
            raise ValueError("max_hours is longer than the coverage table was built for")
        if min_hours <= 0 or max_hours < min_hours:
            raise ValueError("need 0 < min_hours <= max_hours")
        self.cov = cov
        grid = cov.grid
        last = cov.last
        offs = [_hours(grid[0], g) for g in grid]
        eps = 1e-9
        # Allowed ends for a window starting at i: offs[j] - offs[i] in [min_hours, max_hours].
        self.ends: list[list[int]] = []
        self.cost: list[dict[int, float]] = []
        for i in range(last + 1):
            j0 = bisect.bisect_left(offs, offs[i] + min_hours - eps)
            j1 = bisect.bisect_right(offs, offs[i] + max_hours + eps)
            js = [j for j in range(max(j0, i + 1), min(j1, last + 1))]
            self.ends.append(js)
            self.cost.append({j: window_cost(cov.cell(i, j)[0], starters) for j in js})
        inf = float("inf")
        best = [[inf] * (last + 1) for _ in range(k_max + 1)]
        best[0][last] = 0.0
        for k in range(1, k_max + 1):
            prev = best[k - 1]
            cur = best[k]
            for i in range(last - 1, -1, -1):
                ci = self.cost[i]
                m = inf
                for j in self.ends[i]:
                    pj = prev[j]
                    if pj != inf:
                        v = ci[j] + pj
                        if v < m:
                            m = v
                cur[i] = m
        self.best = best
        self.k_max = k_max

    def plan(self, n: int) -> list[int] | None:
        """Grid indices of the lexicographically smallest optimal boundary list, or None if infeasible."""
        if n < 1 or n > self.k_max:
            raise ValueError("window count out of range")
        last = self.cov.last
        if self.best[n][0] == float("inf"):
            return None
        idx = [0]
        i = 0
        for k in range(n, 0, -1):
            target = round(self.best[k][i], COST_DECIMALS)
            prev = self.best[k - 1]
            for j in self.ends[i]:            # ascending: the earliest boundary that stays optimal wins
                if prev[j] != float("inf") and round(self.cost[i][j] + prev[j], COST_DECIMALS) == target:
                    idx.append(j)
                    i = j
                    break
            else:  # pragma: no cover - the minimum is always reached by some j
                raise AssertionError("dynamic program reconstruction failed")
        assert idx[-1] == last
        return idx


def _make_plan(cov: CoverageTable, idx: list[int], starters: int, roster_size: int) -> WindowPlan:
    per = []
    for a, b in zip(idx, idx[1:]):
        share, games, surplus = cov.cell(a, b)
        per.append(WindowStats(start=cov.grid[a], end=cov.grid[b], hours=_hours(cov.grid[a], cov.grid[b]),
                               coverage=share, team_games=games, surplus_per_team=surplus,
                               expected_empty=window_cost(share, starters),
                               expected_surplus=roster_size * surplus))
    return WindowPlan(boundaries=[cov.grid[i] for i in idx], cost=sum(w.expected_empty for w in per),
                      surplus=sum(w.expected_surplus for w in per), per_window=per)


def optimize_windows(coverage: CoverageTable, n_windows: int, min_hours: float = 24, max_hours: float = 336,
                     step_hours: float | None = None, starters: int = STARTER_SLOTS,
                     roster_size: int = ROSTER_SIZE) -> WindowPlan:
    """Exactly ``n_windows`` windows covering the grid's span, minimizing the summed ``5 * (1 - p) ** 2``.

    Every window is between ``min_hours`` and ``max_hours`` long and starts and ends on grid points; the first
    boundary is the season start and the last is the season end.  Tie-break: plans whose costs agree after
    rounding to 9 decimal places are equal, and among equal plans the lexicographically smallest boundary list
    wins (earliest first interior boundary, then earliest second, and so on).  The same input always gives the
    same plan.  ``step_hours``, if given, must match the grid the table was built on.  Raises ``ValueError``
    when no plan with that many windows fits the length limits.
    """
    if step_hours is not None:
        g = coverage.grid
        if len(g) > 2 and abs(_hours(g[0], g[1]) - step_hours) > 1e-9:
            raise ValueError("step_hours does not match the coverage table's grid")
    solver = _Solver(coverage, n_windows, min_hours, max_hours, starters)
    idx = solver.plan(n_windows)
    if idx is None:
        raise ValueError(f"no plan with {n_windows} windows fits {min_hours}-{max_hours} hour windows")
    return _make_plan(coverage, idx, starters, roster_size)


# ---------------------------------------------------------------------------------------------------------------
# Curve
# ---------------------------------------------------------------------------------------------------------------

@dataclass
class CurvePoint:
    """One window count on the curve.  Every field is JSON-native (``json.dumps(dataclasses.asdict(p))``)."""
    n_windows: int
    expected_empty_slot_windows: float     # per roster per season (minimized)
    empty_share: float                     # empty slot-windows / (starters * n_windows)
    expected_surplus_games: float          # per roster per season (reported)
    worst_window_coverage: float
    windows_at_90pct: int                  # windows where >= 90% of teams play
    length_days_min: float
    length_days_median: float
    length_days_max: float
    team_games_min: float
    team_games_max: float
    team_games_sd: float                   # population standard deviation over the windows
    boundaries: list[str]                  # ISO Eastern instants


def curve_point(plan: WindowPlan, starters: int = STARTER_SLOTS) -> CurvePoint:
    per = plan.per_window
    n = len(per)
    days = [w.hours / 24.0 for w in per]
    tg = [w.team_games for w in per]
    return CurvePoint(
        n_windows=n, expected_empty_slot_windows=plan.cost, empty_share=plan.cost / (starters * n),
        expected_surplus_games=plan.surplus, worst_window_coverage=min(w.coverage for w in per),
        windows_at_90pct=sum(1 for w in per if w.coverage >= 0.9 - 1e-12),
        length_days_min=min(days), length_days_median=statistics.median(days), length_days_max=max(days),
        team_games_min=min(tg), team_games_max=max(tg), team_games_sd=statistics.pstdev(tg),
        boundaries=[b.isoformat() for b in plan.boundaries])


def window_curve(rows: Iterable[GameRow], team_ids: Iterable[int], start_et: dt.datetime, end_et: dt.datetime,
                 counts: Iterable[int], step_hours: int = 4, min_hours: float = 24, max_hours: float = 336,
                 starters: int = STARTER_SLOTS, roster_size: int = ROSTER_SIZE,
                 plans: dict[int, WindowPlan] | None = None) -> list[CurvePoint]:
    """One :class:`CurvePoint` per feasible window count in ``counts`` (ascending; infeasible counts skipped).

    Builds the grid and coverage table once and solves the dynamic program once for the largest count.  Pass a
    dict as ``plans`` to also receive the full :class:`WindowPlan` of each count.
    """
    counts = sorted(set(counts))
    if not counts:
        return []
    grid = build_grid(start_et, end_et, step_hours)
    cov = coverage_table(rows, team_ids, grid, max_hours=max_hours)
    solver = _Solver(cov, counts[-1], min_hours, max_hours, starters)
    out = []
    for n in counts:
        idx = solver.plan(n)
        if idx is None:
            continue
        plan = _make_plan(cov, idx, starters, roster_size)
        if plans is not None:
            plans[n] = plan
        out.append(curve_point(plan, starters))
    return out
