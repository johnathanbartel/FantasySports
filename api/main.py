from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import draft, leagues, rosters, scoring, teams

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
