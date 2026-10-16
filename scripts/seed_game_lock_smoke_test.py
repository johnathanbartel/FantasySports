"""Offline check of the game-start lock (platform/game_lock.py): kickoff normalisation and the lock window.
No database, no network. Run with .venv-api."""

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fantasy_sports.platform import game_lock as gl  # noqa: E402

D = datetime.datetime


def check(label, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + label + ("" if ok else f": got {got}, want {want}"))
    return ok


def game(k):
    return SimpleNamespace(kickoff_at=k)


results = [
    # US Eastern: EDT in October, EST in January
    check("EDT to UTC", gl.eastern_to_utc(D(2026, 10, 15, 19)), D(2026, 10, 15, 23)),
    check("EST to UTC", gl.eastern_to_utc(D(2026, 1, 15, 19)), D(2026, 1, 16, 0)),
    check("UTC to EDT", gl.utc_to_eastern(D(2026, 10, 15, 23)), D(2026, 10, 15, 19)),
    check("DST starts 2026-03-08", gl.eastern_offset(D(2026, 3, 8, 3)), datetime.timedelta(hours=4)),
    check("DST ends 2026-11-01", gl.eastern_offset(D(2026, 11, 1, 3)), datetime.timedelta(hours=5)),
    # NHL rows: Eastern wall clock, or UTC when the hour is under 5
    check("nhl eastern row", gl.kickoff_utc(game(D(2026, 10, 15, 19)), "nhl"), D(2026, 10, 15, 23)),
    check("nhl utc row", gl.kickoff_utc(game(D(2026, 10, 15, 23, 30)), "nhl") is not None, True),
    check("nhl utc-stored row", gl.kickoff_utc(game(D(2026, 10, 16, 0, 0)), "nhl"), D(2026, 10, 16, 0, 0)),
    # college is UTC; a date-only (midnight) kickoff counts from the start of the day, Eastern
    check("college utc", gl.kickoff_utc(game(D(2026, 10, 17, 16, 0)), "ncaaf"), D(2026, 10, 17, 16, 0)),
    check("nfl date-only", gl.kickoff_utc(game(D(2026, 10, 18)), "nfl"), D(2026, 10, 18, 4)),
    check("no kickoff", gl.kickoff_utc(game(None), "nfl"), None),
]

# week_over uses the Eastern calendar day
wk = SimpleNamespace(start_date=datetime.date(2026, 10, 12), end_date=datetime.date(2026, 10, 18))
results += [
    check("week not over on its last evening", gl.week_over(wk, D(2026, 10, 18, 23)), False),
    check("week over after midnight Eastern", gl.week_over(wk, D(2026, 10, 19, 5)), True),
]

if not all(results):
    sys.exit("game-lock smoke test FAILED")
print("game-lock smoke test passed")
