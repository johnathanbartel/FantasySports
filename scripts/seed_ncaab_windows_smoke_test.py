"""Offline checks of platform/ncaab_windows.py on the saved NCAAB schedules.  No database, no network.

  a. to_eastern: right offset on both sides of the March and November 2024 / 2025 daylight-saving changes;
     fantasy days start at 4am.
  b. Each season starts on its Monday 4am Eastern and ends on the listed Monday; the dates recomputed from the
     games agree; D1 team counts 362 / 364 / 365 and completed-game counts 6,243 / 6,292 / 6,300.
  c. Optimizer cost for N = 24, 28, 31, 34 on the 4-hour grid beside the prototype's numbers.  Outside the
     tolerance (0.15 or 5%, whichever is larger) is a printed WARNING, not a failure.
  d. Plan shape: exactly N windows, first boundary = season start, last = season end, every window inside the
     length limits, contiguous, every team-game counted exactly once, and byte-identical output on a re-run
     (also with the input rows shuffled).
  e. Empty-slot cost rises and surplus falls as N goes 24 -> 28 -> 31 -> 34.
  f. Tiny hand-worked schedules match exactly, including equal-cost plans where the lexicographically smallest
     boundary list must win, and random tiny schedules match a brute-force search.
  g. A window spanning the whole season covers every team that played at least once.

Run from the repository root:
    .\\.venv-api\\Scripts\\python.exe scripts/seed_ncaab_windows_smoke_test.py
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import ncaab_window_curve as curve  # noqa: E402
from fantasy_sports.platform import ncaab_windows as nw  # noqa: E402

checks = 0
warnings: list[str] = []


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


D = dt.datetime
H = dt.timedelta(hours=1)

# ---------------------------------------------------------------------------------------------------------------
# a. Eastern time
# ---------------------------------------------------------------------------------------------------------------
for day, kind in [((2024, 3, 10), "start"), ((2024, 11, 3), "end"), ((2025, 3, 9), "start"), ((2025, 11, 2), "end")]:
    y, m, d = day
    if kind == "start":   # 07:00 UTC = 2:00 EST becomes 3:00 EDT
        edge = D(y, m, d, 7)
        check(nw.to_eastern(edge - dt.timedelta(minutes=1)) == D(y, m, d, 1, 59), f"{day}: before DST start is EST")
        check(nw.to_eastern(edge) == D(y, m, d, 3, 0), f"{day}: DST start instant is 3:00 EDT")
    else:                 # 06:00 UTC = 2:00 EDT becomes 1:00 EST
        edge = D(y, m, d, 6)
        check(nw.to_eastern(edge - dt.timedelta(minutes=1)) == D(y, m, d, 1, 59), f"{day}: before DST end is EDT")
        check(nw.to_eastern(edge) == D(y, m, d, 1, 0), f"{day}: DST end instant is 1:00 EST")
    check(nw.dst_bounds_utc(y)[0 if kind == "start" else 1] == edge, f"{day}: dst_bounds_utc")
check(nw.to_eastern(D(2025, 1, 15, 12)) == D(2025, 1, 15, 7), "winter is UTC-5")
check(nw.to_eastern(D(2025, 7, 15, 12)) == D(2025, 7, 15, 8), "summer is UTC-4")
check(nw.parse_utc_iso("2023-11-06T16:00:00.000Z") == D(2023, 11, 6, 16), "parse Z")
check(nw.parse_utc_iso("2023-11-06T11:00:00-05:00") == D(2023, 11, 6, 16), "parse offset")
check(nw.fantasy_day(D(2026, 1, 11, 1, 30)) == dt.date(2026, 1, 10), "1:30am Sunday is Saturday's game day")
check(nw.fantasy_day(D(2026, 1, 11, 4, 0)) == dt.date(2026, 1, 11), "4am starts the day")
# A West Coast 10:30pm PT tip (06:30 UTC next day) stays on the previous fantasy day.
check(nw.fantasy_day(nw.to_eastern(D(2026, 1, 11, 6, 30))) == dt.date(2026, 1, 10), "late tip stays on its day")
print("a. Eastern time and DST transitions: ok")

# ---------------------------------------------------------------------------------------------------------------
# b. Season spans and data counts
# ---------------------------------------------------------------------------------------------------------------
EXPECT_TEAMS = {2024: 362, 2025: 364, 2026: 365}
EXPECT_GAMES = {2024: 6243, 2025: 6292, 2026: 6300}
EXPECT_SPANS = {2024: (D(2023, 11, 6, 4), D(2024, 3, 4, 4)), 2025: (D(2024, 11, 4, 4), D(2025, 2, 24, 4)),
                2026: (D(2025, 11, 3, 4), D(2026, 3, 2, 4))}
seasons = {}
for s in (2024, 2025, 2026):
    games = curve.load_games(s)
    teams = curve.load_teams(s)
    d1 = set(teams)
    start, end = curve.SEASON_SPANS[s]
    check(len(teams) == EXPECT_TEAMS[s], f"{s}: {len(teams)} D1 teams, expected {EXPECT_TEAMS[s]}")
    done = [g for g in games if curve.is_completed(g) and (g["homeTeamId"] in d1 or g["awayTeamId"] in d1)]
    check(len(done) == EXPECT_GAMES[s], f"{s}: {len(done)} completed games, expected {EXPECT_GAMES[s]}")
    check((start, end) == EXPECT_SPANS[s], f"{s}: span constant {start} - {end}")
    check(curve.derive_span(games, d1) == (start, end), f"{s}: span from the data {curve.derive_span(games, d1)}")
    check(start.weekday() == 0 and start.hour == 4 and start.minute == 0, f"{s}: starts Monday 4am")
    check(end.weekday() == 0 and end.hour == 4 and end.minute == 0, f"{s}: ends Monday 4am")
    for x in nw.dst_bounds_utc(start.year) + nw.dst_bounds_utc(end.year):
        check(not (start <= nw.to_eastern(x) < end), f"{s}: no DST change inside the span")
    rows = curve.season_rows(games, d1, start, end)
    check(all(r.team_id in d1 for r in rows), f"{s}: only D1 team rows")
    seasons[s] = (rows, teams, start, end)
    played = len({r.team_id for r in rows})
    print(f"b. {curve.SEASON_LABELS[s]}: {len(teams)} D1 teams ({played} played in the span), {len(done)} completed "
          f"games, {len(rows)} team-games in {start:%a %Y-%m-%d %H:%M} - {end:%a %Y-%m-%d %H:%M}: ok")

# ---------------------------------------------------------------------------------------------------------------
# c, d, e. The real seasons
# ---------------------------------------------------------------------------------------------------------------
TARGETS_4H = {2024: {24: 0.5, 28: 1.2, 31: 2.8, 34: 7.0}, 2025: {24: 0.6, 28: 1.8, 31: 5.2, 34: 10.6},
              2026: {24: 0.4, 28: 0.9, 31: 2.5, 34: 6.3}}
TARGETS_DAY = {2024: {24: 0.6, 28: 1.3, 31: 3.0, 34: 7.1}, 2025: {24: 0.6, 28: 1.8, 31: 5.3, 34: 11.0},
               2026: {24: 0.4, 28: 1.0, 31: 2.6, 34: 6.4}}
SURPLUS_SANITY = {24: (41, 56), 28: (24, 33), 31: (17, 21)}   # simulation's per-slot count, informational
COUNTS = (24, 28, 31, 34)
MIN_H, MAX_H = 24, 336


def within(value, target):
    return abs(value - target) <= max(0.15, 0.05 * target)


def run_curve(rows, teams, start, end, step, plans=None):
    return nw.window_curve(rows, teams, start, end, COUNTS, step_hours=step, min_hours=MIN_H, max_hours=MAX_H,
                           plans=plans)


def dump(points):
    return json.dumps([dataclasses.asdict(p) for p in points], sort_keys=True)


runtimes = {}
for s, (rows, teams, start, end) in seasons.items():
    label = curve.SEASON_LABELS[s]
    t0 = time.perf_counter()
    plans: dict[int, nw.WindowPlan] = {}
    points = run_curve(rows, teams, start, end, 4, plans)
    runtimes[s] = time.perf_counter() - t0
    by_n = {p.n_windows: p for p in points}
    check(sorted(by_n) == list(COUNTS), f"{s}: a plan for every count")

    # c. Cost beside the prototype targets.
    for n in COUNTS:
        got, want = by_n[n].expected_empty_slot_windows, TARGETS_4H[s][n]
        lo, hi = SURPLUS_SANITY.get(n, (None, None))
        sur = by_n[n].expected_surplus_games
        flag = "ok" if within(got, want) else "WARNING"
        if flag == "WARNING":
            warnings.append(f"{label} N={n} 4h grid: cost {got:.2f} vs target {want}")
        sanity = f" (simulation range {lo}-{hi})" if lo is not None else ""
        print(f"c. {label} N={n:>2} 4h: empty slot-windows {got:5.2f} target {want:4.1f} [{flag}] | "
              f"surplus games {sur:5.1f}{sanity} | worst window {100 * by_n[n].worst_window_coverage:.0f}%")

    # d. Shape.
    total_rows = sum(1 for r in rows if start <= nw.to_eastern(r.start_utc) < end)
    for n in COUNTS:
        plan = plans[n]
        b = plan.boundaries
        check(len(plan.per_window) == n and len(b) == n + 1, f"{s} N={n}: exactly N windows")
        check(b[0] == start and b[-1] == end, f"{s} N={n}: first/last boundary")
        check(all(x < y for x, y in zip(b, b[1:])), f"{s} N={n}: boundaries ascend")
        for w, (x, y) in zip(plan.per_window, zip(b, b[1:])):
            check(w.start == x and w.end == y, f"{s} N={n}: window matches its boundaries")
            check(MIN_H <= w.hours <= MAX_H, f"{s} N={n}: window length {w.hours}h")
            check((w.start - start) % dt.timedelta(hours=4) == dt.timedelta(0), f"{s} N={n}: on the grid")
        check(all(p.end == q.start for p, q in zip(plan.per_window, plan.per_window[1:])), f"{s} N={n}: contiguous")
        counted = sum(w.team_games * len(teams) for w in plan.per_window)
        check(close(counted, total_rows, 1e-6), f"{s} N={n}: every team-game counted once ({counted} vs {total_rows})")
        check(close(plan.cost, sum(w.expected_empty for w in plan.per_window)), f"{s} N={n}: cost is the sum")
        check(close(by_n[n].empty_share, plan.cost / (5 * n)), f"{s} N={n}: empty share")
    first = dump(points)
    check(first == dump(run_curve(rows, teams, start, end, 4)), f"{s}: identical on a re-run")
    shuffled = list(rows)
    random.Random(s).shuffle(shuffled)
    check(first == dump(run_curve(shuffled, list(reversed(teams)), start, end, 4)), f"{s}: input order irrelevant")
    print(f"d. {label}: N windows, season start/end, {MIN_H}-{MAX_H}h, contiguous, all {total_rows} team-games "
          f"counted once, identical on re-run: ok")

    # e. Monotone.
    costs = [by_n[n].expected_empty_slot_windows for n in COUNTS]
    surpl = [by_n[n].expected_surplus_games for n in COUNTS]
    check(all(x < y for x, y in zip(costs, costs[1:])), f"{s}: cost rises with N {costs}")
    check(all(x > y for x, y in zip(surpl, surpl[1:])), f"{s}: surplus falls with N {surpl}")
    print(f"e. {label}: cost {' < '.join(f'{c:.2f}' for c in costs)}; surplus "
          f"{' > '.join(f'{x:.1f}' for x in surpl)}: ok")

    # Day grid, for comparison with the prototype's day-grid runs (informational).
    day = {p.n_windows: p.expected_empty_slot_windows for p in run_curve(rows, teams, start, end, 24)}
    for n in COUNTS:
        check(day[n] >= by_n[n].expected_empty_slot_windows - 1e-9, f"{s} N={n}: day grid never beats 4h grid")
    print(f"   {label} day grid (info): " + ", ".join(
        f"N={n} {day[n]:.2f} (prototype {TARGETS_DAY[s][n]})" for n in COUNTS))

# ---------------------------------------------------------------------------------------------------------------
# f. Hand-worked tiny schedules
# ---------------------------------------------------------------------------------------------------------------
T0 = D(2026, 1, 5, 4)   # a Monday 4am Eastern (EST, so UTC = ET + 5h)


def tiny_rows(days):
    """days: list of team-id lists; each team plays once at 7pm ET on that day."""
    return [nw.GameRow(t, T0 + dt.timedelta(days=i, hours=15) + 5 * H) for i, ts in enumerate(days) for t in ts]


def tiny_plan(days, n, teams=(1, 2, 3, 4), max_hours=None):
    end = T0 + dt.timedelta(days=len(days))
    grid = nw.build_grid(T0, end, 24)
    mh = max_hours or 24 * len(days)
    cov = nw.coverage_table(tiny_rows(days), teams, grid, max_hours=mh)
    plan = nw.optimize_windows(cov, n, min_hours=24, max_hours=mh, step_hours=24)
    return plan, [round((b - T0) / H) for b in plan.boundaries]


c = {k: 5 * (1 - k / 4) ** 2 for k in range(5)}   # 4 teams: k teams playing -> 0, .3125, 1.25, 2.8125, 5 (k=4..0)

# Case 1, unique answer.  Day 1: teams 1,2; day 2: 3,4; day 3: 1.
#   N=2: cut after day 1 -> {1,2} 1.25 + {3,4,1} 0.3125 = 1.5625; cut after day 2 -> {1,2,3,4} 0 + {1} 2.8125.
case1 = [[1, 2], [3, 4], [1]]
plan, hrs = tiny_plan(case1, 2)
check(hrs == [0, 24, 72] and close(plan.cost, c[2] + c[3]), f"case 1 N=2: {hrs} {plan.cost}")
plan, hrs = tiny_plan(case1, 1)
check(hrs == [0, 72] and close(plan.cost, 0.0), "case 1 N=1: one window, everybody plays")
#   One window holds 5 team-games over 4 teams: 1.25 games per team, team 1 has one surplus game -> 0.25 per team,
#   so a 10-player roster expects 2.5 surplus games.
check(close(plan.per_window[0].team_games, 1.25) and close(plan.per_window[0].surplus_per_team, 0.25)
      and close(plan.surplus, 2.5), "case 1 N=1: team-games and surplus")
plan, hrs = tiny_plan(case1, 3)
check(hrs == [0, 24, 48, 72] and close(plan.cost, c[2] + c[2] + c[1]) and close(plan.surplus, 0.0),
      "case 1 N=3: one day each, no surplus")
try:
    tiny_plan(case1, 4)
    check(False, "case 1 N=4 must be infeasible")
except ValueError:
    check(True, "case 1 N=4 infeasible")
try:
    tiny_plan(case1, 1, max_hours=48)
    check(False, "case 1 N=1 with 48h max must be infeasible")
except ValueError:
    check(True, "max_hours limits a window")

# Case 2, two equal plans.  Day 1: 1,2; day 2: 3,4; day 3: 1,2.
#   Cut after day 1 -> 1.25 + 0 = 1.25; cut after day 2 -> 0 + 1.25 = 1.25.  Tie: the earlier boundary wins.
plan, hrs = tiny_plan([[1, 2], [3, 4], [1, 2]], 2)
check(hrs == [0, 24, 72] and close(plan.cost, 1.25), f"case 2 tie: earliest boundary wins, got {hrs}")
plan, hrs = tiny_plan([[1, 2], [1, 2, 3, 4], [3, 4]], 2)   # mirror image, still the earlier cut
check(hrs == [0, 24, 72] and close(plan.cost, 1.25), f"case 2b tie: got {hrs}")

# Case 3, a hand-built table where the lexicographic rule and a "smallest last boundary" rule disagree.
#   Five one-day steps, three windows.  Only (0,1),(1,4),(4,5) and (0,2),(2,3),(3,5) have everybody playing, so
#   plans [0,1,4,5] and [0,2,3,5] both cost 0 and every other plan costs at least 5.  Lexicographically
#   [0,1,4,5] is smaller (1 < 2) even though its last interior boundary is later (4 > 3).
grid5 = nw.build_grid(T0, T0 + dt.timedelta(days=5), 24)
full = {(0, 1), (1, 4), (4, 5), (0, 2), (2, 3), (3, 5)}
share = [[1.0 if (a, b) in full else 0.0 for b in range(a + 1, 6)] for a in range(5)] + [[]]
zeros = [[0.0] * len(r) for r in share]
table = nw.CoverageTable(grid=grid5, n_teams=1, max_hours=120, share=share, games=zeros, surplus=zeros)
plan = nw.optimize_windows(table, 3, min_hours=24, max_hours=120)
check([round((b - T0) / H) // 24 for b in plan.boundaries] == [0, 1, 4, 5] and close(plan.cost, 0.0),
      f"case 3: lexicographically smallest of two equal plans, got {plan.boundaries}")

# Case 4, random tiny schedules against brute force (all plans, lexicographic tie-break).
rng = random.Random(38)
for trial in range(300):
    ndays = rng.randint(3, 7)
    days = [[t for t in (1, 2, 3, 4) if rng.random() < 0.45] for _ in range(ndays)]
    maxd = rng.randint(1, ndays)
    n = rng.randint(1, ndays)
    best = None
    for cuts in itertools.combinations(range(1, ndays), n - 1):
        bounds = (0, *cuts, ndays)
        if any(y - x > maxd for x, y in zip(bounds, bounds[1:])):
            continue
        cost = sum(c[len({t for d in days[x:y] for t in d})] for x, y in zip(bounds, bounds[1:]))
        key = (round(cost, 9), bounds)
        if best is None or key < best:
            best = key
    try:
        plan, hrs = tiny_plan(days, n, max_hours=24 * maxd)
        got = tuple(h // 24 for h in hrs)
    except ValueError:
        got = None
    want = best[1] if best else None
    check(got == want, f"random trial {trial}: {days} n={n} max={maxd}d got {got} want {want}")
    if got is not None:
        check(close(plan.cost, best[0], 1e-9), f"random trial {trial}: cost")
print("f. tiny schedules: unique plan, two ties (earliest boundaries win), hand-built crossing tie, "
      "300 random schedules match brute force: ok")

# ---------------------------------------------------------------------------------------------------------------
# g. Full-season window
# ---------------------------------------------------------------------------------------------------------------
for s, (rows, teams, start, end) in seasons.items():
    span_h = (end - start) / H
    grid = nw.build_grid(start, end, 24)
    played = sorted({r.team_id for r in rows})
    cov = nw.coverage_table(rows, played, grid, max_hours=span_h)
    check(cov.cell(0, cov.last)[0] == 1.0, f"{s}: full-season window covers every team that played")
    cov_all = nw.coverage_table(rows, teams, grid, max_hours=span_h)
    check(close(cov_all.cell(0, cov_all.last)[0], len(played) / len(teams)), f"{s}: share over the D1 list")
    check(close(cov_all.cell(0, cov_all.last)[1] * len(teams), len(rows), 1e-6), f"{s}: all games in the span")
    plan = nw.optimize_windows(cov, 1, min_hours=24, max_hours=span_h)
    check(plan.boundaries == [start, end] and close(plan.cost, 0.0), f"{s}: one-window plan")
    print(f"g. {curve.SEASON_LABELS[s]}: full-season window covers all {len(played)} teams that played "
          f"({len(played)}/{len(teams)} of the D1 list): ok")

print()
print("runtime per season (coverage + curve for N=24/28/31/34, 4h grid): "
      + ", ".join(f"{curve.SEASON_LABELS[s]} {t:.2f}s" for s, t in runtimes.items()))
if warnings:
    print(f"{len(warnings)} WARNING(s), cost outside tolerance of the prototype's number (not a failure):")
    for w in warnings:
        print("  WARNING " + w)
else:
    print("no warnings: every cost is within tolerance of the prototype's number")
print(f"all {checks} checks passed")
