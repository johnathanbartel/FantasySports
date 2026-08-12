# Fantasy Sports Company

Internal fantasy sports platform. Football module (NCAAF + NFL) is the current
build target; baseball uses a related but distinct locked-rotation mechanic
and is out of scope for now.

## Core idea

Standard fantasy football double-counts touchdowns (QB gets the full passing
TD, the receiver gets the full receiving TD) and rewards yardage that never
actually produced points. This platform scores players on **Expected Points
Added (EPA)** instead: every play's value is the change in a field-position-
based Expected Points model, split among the players involved in that specific
play. See `Football/README.md` for the full scoring spec.

## Layout

```
Football/
  common/        shared EPA model + credit-attribution logic (used by both College and Professional)
  College/       NCAAF data pipeline (CollegeFootballData.com)
  Professional/  NFL data pipeline (nflfastR) - not started yet
Baseball/        out of scope for v1
Basketball/      out of scope for v1
Hockey/          out of scope for v1

db/              SQLAlchemy models + Alembic migrations (sport-agnostic core + football-specific tables)
api/             FastAPI app - the API surface on top of the schema
scripts/         seed_smoke_test.py, seed_demo_league.py
docs/            data_contract.md - plain-English schema summary for design handoff
```

## Setup

Two separate environments - `cfbd` pins pydantic<2 and breaks under pydantic v2, while the DB/API layer wants modern FastAPI + pydantic v2. Two concerns, two venvs.

**Data pipeline** (CFBD/nflfastR ingestion, scoring scripts under `Football/`):

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # then fill in your API keys
```

**DB/API layer** (`db/`, `api/`):

```bash
python -m venv .venv-api
.venv-api\Scripts\activate
pip install -r requirements-api.txt
alembic upgrade head              # creates fantasy.db (SQLite, default)
python scripts/seed_smoke_test.py # round-trips fake data through the schema + API - quick sanity check
uvicorn api.main:app --reload     # http://127.0.0.1:8000/docs
```

**Demo data for local prototyping** (real CFBD rosters + games, not fake data - run from the *data pipeline* venv, `.venv`, not `.venv-api`; SQLAlchemy has no pydantic dependency so it installs cleanly there alongside `cfbd`):

```bash
.venv\Scripts\activate
python scripts/seed_demo_league.py  # resets fantasy.db and reseeds a full 12-team demo league
```

Seeds a 12-team league (4 human-owned, 8 AI-owned), a completed 240-pick snake draft drawn from real rosters across 16 real FBS teams (players + DL/LB/DB defense groups, same pool), and three weeks (2024, weeks 1-3) of real games run through the actual attribution pipeline - so lineups, scores, and standings all reflect real football, not placeholders. Safe to re-run any time to reset the local DB (drops and recreates everything - not incremental).
