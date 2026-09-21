"""
Serves the app against a private copy of the seeded demo dataset, so a manual
end-to-end run (create league -> AI teams -> recruiting period) never touches
mock_draft_demo.db or fantasy.db.

    .venv-api/Scripts/python.exe scripts/serve_demo.py [--reset] [--port 8000]

First run copies mock_draft_demo.db (built by scripts/seed_demo_league.py) to
user_run.db; later runs keep whatever state you left. --reset re-copies it.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

parser = argparse.ArgumentParser()
parser.add_argument("--reset", action="store_true")
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()

source, target = ROOT / "mock_draft_demo.db", ROOT / "user_run.db"
if not source.exists():
    sys.exit("mock_draft_demo.db missing - run scripts/seed_demo_league.py first")
if args.reset or not target.exists():
    shutil.copyfile(source, target)
    print(f"Fresh copy of the demo dataset -> {target.name}")

os.environ["DATABASE_URL"] = f"sqlite:///{target.as_posix()}"
# Dev/testing: every user gets premium features (commissioner mode) - see platform/entitlements.py.
os.environ.setdefault("FSC_GRANT_ALL_PREMIUM", "1")

from fantasy_sports.platform.db.base import Base  # noqa: E402
from fantasy_sports.platform.db.models import core  # noqa: E402,F401
from fantasy_sports.platform.db.session import engine  # noqa: E402

Base.metadata.create_all(bind=engine)  # adds tables newer than the seed (e.g. user_sessions)

import uvicorn  # noqa: E402

uvicorn.run("fantasy_sports.api.main:app", port=args.port, app_dir=str(ROOT / "src"))
