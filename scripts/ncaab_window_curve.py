"""Read-only report: the NCAAB windows-versus-gaps curve for one season, from the saved schedule files.

For each window count it prints the optimizer's expected empty starter slot-windows per roster per season
(what the optimizer minimizes) beside the expected surplus games per roster per season (the counterweight),
plus window coverage and length.  ``--show N`` prints the boundaries of the N-window plan.

Reads only ``analysis/ncaab_schedule/raw/games_<season>.json.gz`` and ``teams_<season>.json.gz`` (CBBD seasons
2024, 2025, 2026 = 2023-24, 2024-25, 2025-26).  No database, no ``.env``, no network.

Run from the repository root:
    .\\.venv-api\\Scripts\\python.exe scripts/ncaab_window_curve.py --season 2026 --min-windows 20 --max-windows 36
    .\\.venv-api\\Scripts\\python.exe scripts/ncaab_window_curve.py --season 2026 --show 28
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fantasy_sports.platform import ncaab_windows as nw  # noqa: E402

RAW_DIR = ROOT / "analysis" / "ncaab_schedule" / "raw"
SEASON_LABELS = {2024: "2023-24", 2025: "2024-25", 2026: "2025-26"}

# Regular-season fantasy span per CBBD season: first Monday 4am Eastern, and the Monday 4am after the last full
# Monday-to-Sunday week before the first conference-tournament game.  derive_span() recomputes both from the
# saved games; the smoke test checks they agree.
SEASON_SPANS = {
    2024: (dt.datetime(2023, 11, 6, 4), dt.datetime(2024, 3, 4, 4)),
    2025: (dt.datetime(2024, 11, 4, 4), dt.datetime(2025, 2, 24, 4)),
    2026: (dt.datetime(2025, 11, 3, 4), dt.datetime(2026, 3, 2, 4)),
}


def _load(name: str, raw_dir: Path = RAW_DIR):
    with gzip.open(raw_dir / f"{name}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def load_teams(season: int, raw_dir: Path = RAW_DIR) -> list[int]:
    """The season's Division I team ids (CBBD teams list)."""
    return sorted(t["id"] for t in _load(f"teams_{season}", raw_dir))


def load_games(season: int, raw_dir: Path = RAW_DIR) -> list[dict]:
    return _load(f"games_{season}", raw_dir)


def is_completed(g: dict) -> bool:
    return g["status"] == "final" and g["homePoints"] is not None and g["awayPoints"] is not None


def is_conf_tournament(g: dict, et: dt.datetime) -> bool:
    """Same rule as analysis/ncaab_schedule/build_analysis.py: a tournament-type, same-conference game on or
    after Feb 25 (earlier ones are early-season events CBBD also flags as conference games)."""
    return (g["seasonType"] != "postseason" and g["gameType"] == "TRNMNT" and bool(g["conferenceGame"])
            and (et.month, et.day) >= (2, 25) and et.month <= 4)


def _monday_4am(day: dt.date) -> dt.datetime:
    monday = day - dt.timedelta(days=day.weekday())
    return dt.datetime.combine(monday, dt.time(nw.FANTASY_DAY_START_HOUR))


def derive_span(games: list[dict], d1: set[int]) -> tuple[dt.datetime, dt.datetime]:
    """(start, end) from the data: the Monday 4am of the first regular-season game's fantasy week, and the Monday
    4am that starts the fantasy week holding the first conference-tournament game."""
    regular, ct = [], []
    for g in games:
        if not is_completed(g) or g["seasonType"] != "regular":
            continue
        if g["homeTeamId"] not in d1 and g["awayTeamId"] not in d1:
            continue
        et = nw.to_eastern(nw.parse_utc_iso(g["startDate"]))
        regular.append(et)
        if is_conf_tournament(g, et):
            ct.append(et)
    return _monday_4am(nw.fantasy_day(min(regular))), _monday_4am(nw.fantasy_day(min(ct)))


def season_rows(games: list[dict], d1: set[int], start_et: dt.datetime, end_et: dt.datetime) -> list[nw.GameRow]:
    """Team rows for the optimizer: completed (final, both scores) regular-season games with at least one D1 team,
    starting inside [start_et, end_et).  A game against a non-D1 opponent gives a row for the D1 team only."""
    rows = []
    for g in games:
        if not is_completed(g) or g["seasonType"] != "regular":
            continue
        utc = nw.parse_utc_iso(g["startDate"])
        et = nw.to_eastern(utc)
        if not (start_et <= et < end_et):
            continue
        for tid in (g["homeTeamId"], g["awayTeamId"]):
            if tid in d1:
                rows.append(nw.GameRow(team_id=tid, start_utc=utc))
    rows.sort(key=lambda r: (r.start_utc, r.team_id))
    return rows


def load_season(season: int, raw_dir: Path = RAW_DIR):
    """(rows, d1 team ids, start_et, end_et) for one CBBD season."""
    teams = load_teams(season, raw_dir)
    start, end = SEASON_SPANS[season]
    return season_rows(load_games(season, raw_dir), set(teams), start, end), teams, start, end


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%a %Y-%m-%d %H:%M")


def print_curve(points: list[nw.CurvePoint]) -> None:
    print(f"{'N':>3} | {'empty slot-win':>14} | {'empty %':>7} | {'surplus games':>13} | {'worst cov':>9} | "
          f"{'>=90%':>6} | {'days min/med/max':>16} | {'team-games min/max/sd':>21}")
    print("-" * 113)
    for p in points:
        print(f"{p.n_windows:>3} | {p.expected_empty_slot_windows:>14.2f} | {100 * p.empty_share:>6.2f}% | "
              f"{p.expected_surplus_games:>13.1f} | {100 * p.worst_window_coverage:>8.1f}% | "
              f"{p.windows_at_90pct:>2}/{p.n_windows:<3} | "
              f"{p.length_days_min:>4.1f} /{p.length_days_median:>5.1f} /{p.length_days_max:>4.1f} | "
              f"{p.team_games_min:>5.2f} /{p.team_games_max:>5.2f} /{p.team_games_sd:>5.2f}")


def print_plan(plan: nw.WindowPlan) -> None:
    n = len(plan.per_window)
    print(f"\n{n}-window plan: expected empty slot-windows {plan.cost:.2f}, expected surplus games {plan.surplus:.1f}")
    print(f"{'#':>3} | {'start (ET)':<20} | {'end (ET)':<20} | {'days':>5} | {'coverage':>8} | "
          f"{'team-games':>10} | {'exp. empty':>10} | {'exp. surplus':>12}")
    print("-" * 110)
    for i, w in enumerate(plan.per_window, 1):
        print(f"{i:>3} | {_fmt(w.start):<20} | {_fmt(w.end):<20} | {w.hours / 24:>5.2f} | "
              f"{100 * w.coverage:>7.1f}% | {w.team_games:>10.2f} | {w.expected_empty:>10.3f} | "
              f"{w.expected_surplus:>12.2f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--season", type=int, choices=sorted(SEASON_SPANS), default=2026)
    ap.add_argument("--min-windows", type=int, default=20)
    ap.add_argument("--max-windows", type=int, default=36)
    ap.add_argument("--step-hours", type=int, default=4)
    ap.add_argument("--min-hours", type=float, default=24)
    ap.add_argument("--max-hours", type=float, default=336)
    ap.add_argument("--show", type=int, default=None, metavar="N", help="print the boundaries of the N-window plan")
    args = ap.parse_args(argv)
    if args.min_windows < 1 or args.max_windows < args.min_windows:
        ap.error("need 1 <= --min-windows <= --max-windows")

    t0 = time.perf_counter()
    rows, teams, start, end = load_season(args.season)
    counts = set(range(args.min_windows, args.max_windows + 1))
    if args.show is not None:
        counts.add(args.show)
    plans: dict[int, nw.WindowPlan] = {}
    points = nw.window_curve(rows, teams, start, end, counts, step_hours=args.step_hours,
                             min_hours=args.min_hours, max_hours=args.max_hours, plans=plans)
    elapsed = time.perf_counter() - t0

    print(f"NCAAB {SEASON_LABELS[args.season]} (CBBD season {args.season}): {len(teams)} D1 teams, "
          f"{len(rows)} team-games, span {_fmt(start)} to {_fmt(end)} ET")
    print(f"grid {args.step_hours}h, windows {args.min_hours:g}-{args.max_hours:g}h; empty slot-windows = "
          f"sum of 5*(1-p)^2 (minimized), surplus games = sum of 10*avg(max(0, games-1)) (reported)\n")
    shown = [p for p in points if args.min_windows <= p.n_windows <= args.max_windows]
    print_curve(shown)
    skipped = sorted(set(range(args.min_windows, args.max_windows + 1)) - {p.n_windows for p in shown})
    if skipped:
        print(f"(no plan fits the length limits for N = {', '.join(map(str, skipped))})")
    if args.show is not None:
        if args.show not in plans:
            print(f"\nno {args.show}-window plan fits the length limits")
            return 1
        print_plan(plans[args.show])
    print(f"\n(took {elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
