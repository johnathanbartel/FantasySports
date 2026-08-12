from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routers import draft, leagues, rosters, scoring, teams

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="Fantasy Sports Platform API",
    description=(
        "Internal API for the football module (college + NFL). See "
        "docs/data_contract.md for the plain-English schema summary."
    ),
    version="0.1.0",
)

# Wide open for local dev only - this is a single-user internal tool with
# no auth yet. Tighten to specific origins before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(leagues.router)
app.include_router(teams.router)
app.include_router(draft.router)
app.include_router(rosters.router)
app.include_router(scoring.router)


@app.get("/health")
def health():
    return {"status": "ok"}


# Serves the prototype frontend (frontend/draft-board.html etc.) at
# /static/... - a real, working page hitting the API above, not a static
# mockup. One process (uvicorn) hosts both, so there's nothing else to run.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
