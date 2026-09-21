from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from fantasy_sports.api.routers import (
    auction, draft, entitlements, league_home, leagues, recruiting, rosters, schedule, scoring, sessions, teams,
)

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "web" / "static"

app = FastAPI(
    title="Fantasy Sports Platform API",
    description=(
        "Shared API for the multi-sport fantasy platform. See "
        "docs/data_contract.md for the plain-English schema summary."
    ),
    version="0.1.0",
)

# Wide open for local dev only - identity is a dev-grade session token (see
# api/auth.py), not real authentication. Tighten to specific origins before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions.router)
app.include_router(entitlements.router)
app.include_router(league_home.router)
app.include_router(leagues.router)
app.include_router(teams.router)
app.include_router(draft.router)
app.include_router(auction.router)
app.include_router(recruiting.router)
app.include_router(rosters.router)
app.include_router(scoring.router)
app.include_router(schedule.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/static/index.html")


# Serves the single-site frontend (web/static/draft-board.html etc.) at
# /static/... - a real, working page hitting the API above, not a static
# mockup. One process (uvicorn) hosts both, so there's nothing else to run.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
