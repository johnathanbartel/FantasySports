"""Vercel entrypoint for the Fantasy Sports FastAPI application."""

from pathlib import Path
import sys


# Vercel imports this file from the repository root. Add the src-layout package
# directory explicitly so the entrypoint also works without an editable install.
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from fantasy_sports.api.main import app  # noqa: E402, F401
