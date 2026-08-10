from fastapi import FastAPI

from api.routers import draft, leagues, rosters, scoring, teams

app = FastAPI(
    title="Fantasy Sports Platform API",
    description=(
        "Internal API for the football module (college + NFL). See "
        "docs/data_contract.md for the plain-English schema summary."
    ),
    version="0.1.0",
)

app.include_router(leagues.router)
app.include_router(teams.router)
app.include_router(draft.router)
app.include_router(rosters.router)
app.include_router(scoring.router)


@app.get("/health")
def health():
    return {"status": "ok"}
