import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from fantasy_sports.api.routers import (
    auction, banking, draft, draft_broadcast, entitlements, league_home, league_leaders, leagues, media, pecking_order, recruiting, rosters, schedule, scoring, series_schedule, series_standings, sessions, simulator, teams, trades, weekly_redraft,
)
from fantasy_sports.platform.db.models.core import League
from fantasy_sports.platform.db.session import get_db

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "web" / "static"

app = FastAPI(
    title="FantasyMotion API",
    description=(
        "Your league. Your rules. Your universe. "
        "Shared API for the FantasyMotion multi-sport platform. See "
        "docs/data_contract.md for the plain-English schema summary."
    ),
    version="0.1.0",
)

cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(sessions.router)
app.include_router(entitlements.router)
app.include_router(league_home.router)
app.include_router(leagues.router)
app.include_router(teams.router)
app.include_router(draft.router)
app.include_router(draft_broadcast.router)
app.include_router(media.router)
app.include_router(weekly_redraft.router)
app.include_router(auction.router)
app.include_router(recruiting.router)
app.include_router(rosters.router)
app.include_router(scoring.router)
app.include_router(schedule.router)
app.include_router(series_schedule.router)
app.include_router(series_standings.router)
app.include_router(league_leaders.router)
app.include_router(pecking_order.router)
app.include_router(simulator.router)
app.include_router(banking.router)
app.include_router(trades.router)


@app.middleware("http")
async def revalidate_static(request, call_next):
    """The pages and their scripts/styles change with every deploy: have browsers check (a cheap 304) instead of
    reusing a stale copy, so a fix like a missing CSS token shows up straight away."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/l/{slug}", include_in_schema=False)
def league_by_slug(slug: str, db: Session = Depends(get_db)):
    """A league's custom URL: sends people to the league page."""
    row = db.query(League.id).filter(League.slug == slug.strip().lower()).first()
    if row is None:
        raise HTTPException(404, "League not found")
    return RedirectResponse(f"/static/league.html?league_id={row[0]}")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/static/index.html")


# Serves the single-site frontend (web/static/draft-board.html etc.) at
# /static/... - a real, working page hitting the API above, not a static
# mockup. One process (uvicorn) hosts both, so there's nothing else to run.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
