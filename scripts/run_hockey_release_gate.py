"""
FS-034 release gate runner: runs every NHL verification script and the shared regression scripts in turn (each in its
own process, from the repository root, with the API venv), checks the Alembic head, compiles the changed modules and
prints a pass/fail matrix. Exit code 0 only if everything passes. Offline: no network, no production data, no deploy.

    .venv-api/Scripts/python.exe scripts/run_hockey_release_gate.py [--skip-e2e]
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

NHL_SCRIPTS = [
    "seed_hockey_smoke_test.py", "seed_hockey_league_creation_smoke_test.py", "verify_hockey_draft_board_schema.py",
    "verify_hockey_series_schema.py", "verify_hockey_series_schedule.py", "verify_hockey_pecking_order.py",
    "verify_hockey_schedule_viewer.py", "verify_hockey_series_standings.py", "verify_simulator_core.py",
    "audit_hockey_simulator_scoring.py", "verify_hockey_simulator_ui.py", "verify_hockey_banking.py",
    "verify_hockey_banking_ui.py", "verify_async_snake_draft.py", "verify_draft_broadcast.py", "verify_draft_media_seams.py",
    "verify_weekly_redraft.py",
]
SHARED_SCRIPTS = ["seed_smoke_test.py", "seed_duplicate_pick_smoke_test.py", "seed_auction_smoke_test.py", "seed_conference_smoke_test.py",
                  "run_mock_e2e.py"]
E2E = "verify_hockey_series_e2e.py"


def run(cmd: list[str], timeout: int) -> tuple[bool, str, float]:
    t = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        tail = [l for l in (p.stdout + p.stderr).strip().splitlines() if l.strip() and "Warning" not in l and "starlette" not in l]
        return p.returncode == 0, (tail[-1] if tail else ""), time.time() - t
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s", time.time() - t


def main() -> int:
    rows: list[tuple[str, bool, str, float]] = []
    ok, out, secs = run([PY, "-m", "alembic", "heads"], 60)
    heads = re.findall(r"^(\w+) \(head\)", out + "\n", re.M)
    p = subprocess.run([PY, "-m", "alembic", "heads"], cwd=ROOT, capture_output=True, text=True)
    heads = re.findall(r"^(\w+) \(head\)", p.stdout, re.M)
    rows.append(("alembic heads == 1", len(heads) == 1, ", ".join(heads), secs))
    changed = subprocess.run(["git", "diff", "--name-only", "main...HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    py = [c for c in changed if c.endswith(".py") and (ROOT / c).exists()]
    ok, out, secs = run([PY, "-m", "py_compile", *py], 120)
    rows.append((f"py_compile ({len(py)} changed modules)", ok, out, secs))
    d = subprocess.run(["git", "diff", "--check", "main...HEAD"], cwd=ROOT, capture_output=True, text=True)
    rows.append(("git diff --check", d.returncode == 0, d.stdout.strip()[:80], 0.0))
    for name in NHL_SCRIPTS + SHARED_SCRIPTS + ([] if "--skip-e2e" in sys.argv else [E2E]):
        ok, out, secs = run([PY, str(ROOT / "scripts" / name)], 3000 if name == E2E else 600)
        rows.append((name, ok, out[:90], secs))
    width = max(len(r[0]) for r in rows)
    print("\nNHL series release gate")
    print("-" * (width + 30))
    for name, ok, out, secs in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {secs:6.1f}s  {out}")
    failed = [r for r in rows if not r[1]]
    print("-" * (width + 30))
    print(f"{len(rows) - len(failed)} of {len(rows)} passed" + ("" if not failed else f"; FAILED: {', '.join(r[0] for r in failed)}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
