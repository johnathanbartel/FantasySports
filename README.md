# Fantasy Sports Company

A single-site fantasy platform for major sports. Shared league mechanics live
once in the platform layer; each sport supplies only its provider integrations,
scoring rules, and sport-specific persistence.

Football (NCAAF first, NFL next) is the current working implementation. The
baseball, basketball, and hockey package boundaries are ready for MLB, NBA, and
NHL modules without duplicating site infrastructure.

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
scoring details, and
[docs/timed_auction_draft.md](docs/timed_auction_draft.md) for the Timed
Auction Draft's round resolution flow.

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
`http://127.0.0.1:8000/docs`, the snake draft board at
`http://127.0.0.1:8000/static/draft-board.html`, or the Timed Auction Draft
board at `http://127.0.0.1:8000/static/auction-board.html`.

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

Run the automated test suite (unit plus API tests, against a throwaway
in-memory database):

```powershell
pip install -e ".[dev]"
.venv-api\Scripts\python.exe -m pytest
```

Run the isolated API/database smoke tests:

```powershell
.venv-api\Scripts\python.exe scripts/seed_smoke_test.py       # core + snake draft
.venv-api\Scripts\python.exe scripts/auction_smoke_test.py    # full Recruiting Period
```

To reset and seed the local demo league with real CFBD roster and game data:

```powershell
.venv\Scripts\python.exe scripts/seed_demo_league.py
```

The demo seed intentionally drops and recreates the configured local database.
It creates a completed draft and a second open draft for the live draft board.
