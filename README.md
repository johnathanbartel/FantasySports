# Fantasy Sports Company

A single-site fantasy platform for major sports. Shared league mechanics live
once in the platform layer; each sport supplies only its provider integrations,
scoring rules, and sport-specific persistence.

Football (NCAAF first, NFL next) is the current working implementation.
Hockey (NHL) has a written scoring/roster spec and its data model stood up
(see below); the baseball, basketball, and hockey package boundaries are
otherwise ready for MLB, NBA, and further NHL work without duplicating site
infrastructure.

## Repository layout

```text
src/fantasy_sports/
  api/                     one API for the site
  platform/db/             shared users, leagues, drafts, rosters, and schedules
  sports/
    football/              shared football scoring plus college/pro adapters
    baseball/              MLB module boundary
    basketball/            NBA module boundary
    hockey/                NHL module boundary
web/static/                site assets served by the API
migrations/                one Alembic history for every sport
scripts/                   seeding and operational commands
docs/                      architecture and data contracts
```

See [docs/architecture.md](docs/architecture.md) for placement and branch rules,
[the football spec](src/fantasy_sports/sports/football/README.md) for EPA
scoring details, and [the hockey spec](src/fantasy_sports/sports/hockey/README.md)
for xG-based scoring and roster rules.

## Branch model

`master` is the single integration branch and deployable source of truth. Sport
work belongs in short-lived feature branches and returns to `master`; sports do
not get permanent branches. `origin/main` is a stale initial-commit branch and
should be removed by a repository administrator once protected-branch settings
and external integrations have been checked.

## Setup

Create the API environment and install the application in editable mode:

```powershell
python -m venv .venv-api
.venv-api\Scripts\Activate.ps1
pip install -e .
alembic upgrade head
uvicorn fantasy_sports.api.main:app --reload
```

The API and web UI run in the same process. Open the API documentation at
`http://127.0.0.1:8000/docs` or the current draft board at
`http://127.0.0.1:8000/static/draft-board.html`.

Football ingestion currently needs its separate legacy-compatible environment
because `cfbd` pins Pydantic below version 2:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "src"
```

Copy `.env.example` to `.env` and supply provider credentials before running
ingestion. Lookup data and Python packages are included by `pyproject.toml`.

## Local verification and demo data

Run the isolated API/database smoke test:

```powershell
.venv-api\Scripts\python.exe scripts/seed_smoke_test.py
```

Run the auction draft engine's smoke test (timed rounds, sealed bids, budget debits, tie-break):

```powershell
.venv-api\Scripts\python.exe scripts/seed_auction_smoke_test.py
```

Run the conference/program-size smoke test (baseline conferences, team placement, season goals, tier-balanced expansion):

```powershell
.venv-api\Scripts\python.exe scripts/seed_conference_smoke_test.py
```

Run the NHL stand-up smoke test (nhl universe, roster slot definitions, draft round-trip on the shared platform):

```powershell
.venv-api\Scripts\python.exe scripts/seed_hockey_smoke_test.py
```

To reset and seed the local demo league with real CFBD roster and game data:

```powershell
.venv\Scripts\python.exe scripts/seed_demo_league.py
```

The demo seed intentionally drops and recreates the configured local database.
It creates a completed draft and a second open draft for the live draft board.
