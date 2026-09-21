"""
The whole mock, end to end, in one command - each stage is its own audited script run against the real HTTP API:

  1. run_mock_recruiting_period   league creation -> AI teams -> recruiting periods (auto-bidding AI) -> results
  2. run_mock_commissioner_mode   all-AI league, simulated draft, roster/lineup rules and permissions
  3. run_mock_league_home         league home tabs, free agency, AI roster fill
  4. run_mock_scoring             12-team all-AI league drafted on the live 2026 data, lineups for weeks 1-3, schedule,
                                  matchups scored, standings, a team total cross-checked against its per-player
                                  credits, a retroactive lineup swap, lineup lock

    .venv-api/Scripts/python.exe scripts/run_mock_e2e.py

Stages 1-3 need mock_draft_demo.db (scripts/seed_demo_league.py); stage 4 needs user_run.db with 2026 weeks scored
(scripts/ingest_current_season.py, scripts/ingest_week_scores.py). All run on throwaway copies.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = ["run_mock_recruiting_period", "run_mock_commissioner_mode", "run_mock_league_home", "run_mock_scoring", "run_mock_schedule_rankings"]


def main():
    results = []
    for name in STAGES:
        t0 = time.time()
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / f"{name}.py")], cwd=ROOT, capture_output=True, text=True)
        ok = p.returncode == 0
        results.append((name, ok, time.time() - t0))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({time.time() - t0:.0f}s)")
        if not ok:
            print((p.stdout + p.stderr)[-2000:])
            break
    if all(ok for _, ok, _ in results) and len(results) == len(STAGES):
        print("\nEND-TO-END MOCK PASSED")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
