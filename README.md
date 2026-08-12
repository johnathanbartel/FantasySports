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
frontend/        Real prototype pages (draft-board.html, ...), served by the API itself - no separate frontend server
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

Also starts a second, fresh draft (`draft_id=2`) for the same league via a real `POST /drafts/start` call: `draft_id=1` from the seed is already 100% complete (240/240 picks), so there's nothing left to interactively draft against it - `draft_id=2` has all 240 picks open, for demoing the live pick/autopick flow. If you reset the DB with `seed_demo_league.py` again, re-create it:

```bash
curl -X POST http://127.0.0.1:8000/drafts/start -H "Content-Type: application/json" -d "{\"league_id\": 1}"
```

## Viewing the prototype

Real, working pages - not static mockups - served by the API itself, so there's nothing else to run beyond `uvicorn` (see DB/API layer setup above):

- **Draft board**: `http://127.0.0.1:8000/static/draft-board.html` - live board, pick history, make-pick and autopick, all hitting the real API against `draft_id=2` by default (override with `?draft_id=`).

Styled with the Broadsheet design system pulled from the Claude Design project (`frontend/styles.css`) - tokens and component classes only; the design's decorative CMYK print-separation animation was left out as unused on this screen.
