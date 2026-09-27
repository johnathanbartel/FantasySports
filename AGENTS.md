# AGENTS.md

Operational guidelines and context for AI coding agents in this repository.

## Layout

- `src/fantasy_sports/api`: FastAPI HTTP routes and application endpoints.
- `src/fantasy_sports/platform`: Shared platform logic, database models and session management, drafts, auctions, rosters, schedules.
- `src/fantasy_sports/sports`: Sport-specific packages (`src/fantasy_sports/sports/football`, `src/fantasy_sports/sports/baseball`, `src/fantasy_sports/sports/basketball`, `src/fantasy_sports/sports/hockey`).
- `web/static`: Web frontend assets served directly by the API.
- `migrations`: Alembic schema migrations for the platform.
- `scripts`: Seeding, ingestion, operational, and mock verification scripts.
- `docs`: Architecture documentation and data contracts (`docs/architecture.md`, `docs/data_contract.md`).

## Environments

Two separate virtual environments exist and must never be merged:
- `.venv`: Data pipeline environment.
  - Interpreter: `.venv/Scripts/python.exe`
  - Dependencies: `requirements.txt`
  - Usage: CFBD data ingestion, sports data processing, demo league seeding.
  - Note: `cfbd` pins `pydantic<2`, safe alongside SQLAlchemy (no Pydantic dependency).
- `.venv-api`: FastAPI and web platform environment.
  - Interpreter: `.venv-api/Scripts/python.exe`
  - Dependencies: `requirements-api.txt`
  - Usage: FastAPI server, SQLAlchemy 2.0 ORM, Alembic migrations, modern `pydantic>=2`, TestClient mock tests.

## Scripts

All scripts reside in `scripts`. Run using the specified Python interpreter from the repository root:

| Script | Purpose | Venv | External API / .env | Writes DB |
|---|---|---|---|---|
| `scripts/delete_all_leagues.py` | Deletes leagues and child records from SQLite DBs (dry run default; `--yes` executes) | `.venv-api` | No (unverified in docstring) | Yes (destructive with `--yes`) |
| `scripts/ingest_current_season.py` | Ingests FBS rosters, season calendar weeks, and prior season ESPN points | `.venv` | Yes (CFBD API; requires `CFBD_API_KEY`) | Yes (`DATABASE_URL`) |
| `scripts/ingest_season_points.py` | Ingests full-season stat lines from CFBD and stores ESPN standard points | `.venv` | Yes (CFBD API; requires `CFBD_API_KEY`) | Yes (`DATABASE_URL`) |
| `scripts/ingest_week_scores.py` | Scores real games for season weeks (games, plays, credits, weekly scores) | `.venv` | Yes (CFBD API; `CFBD_API_KEY` unverified in docstring) | Yes (`DATABASE_URL`) |
| `scripts/run_mock_auction_draft.py` | Runs timed auction draft end-to-end via FastAPI TestClient | `.venv-api` | No (unverified in docstring) | Yes (throwaway copy DB) |
| `scripts/run_mock_commissioner_mode.py` | Tests commissioner mode entitlement, draft simulation, and lineup rules | `.venv-api` | No (unverified in docstring) | Yes (throwaway copy DB) |
| `scripts/run_mock_e2e.py` | Orchestrates all mock test stages end-to-end | `.venv-api` | No (unverified in docstring) | Yes (via child stages) |
| `scripts/run_mock_league_home.py` | Tests league home tabs, free agency, transactions, and stats table | `.venv-api` | No (unverified in docstring) | Yes (throwaway copy DB) |
| `scripts/run_mock_recruiting_period.py` | Runs mock recruiting draft with timed periods and sealed bids | `.venv-api` | No (unverified in docstring) | Yes (throwaway copy DB) |
| `scripts/run_mock_schedule_rankings.py` | Tests preseason schedule stage, week-10 championships, and rankings | `.venv-api` | No (unverified in docstring) | Yes (throwaway copy DB) |
| `scripts/run_mock_scoring.py` | Audits scoring, matchups, standings, and retroactive lineup edits | `.venv-api` | No (unverified in docstring) | Yes (throwaway copy DB, or live DB if `--keep-live`) |
| `scripts/seed_auction_smoke_test.py` | Smoke test for timed sealed-bid auction draft engine round-trip | `.venv-api` | No (unverified in docstring) | Yes (throwaway test DB) |
| `scripts/seed_conference_smoke_test.py` | Smoke test for conference tiers, team placement, and tier expansion | `.venv-api` | No (unverified in docstring) | Yes (throwaway test DB) |
| `scripts/seed_demo_league.py` | Seeds demo league with real CFBD rosters, 240-pick draft, and 3 scored weeks | `.venv` | Yes (calls CFBD API, requires `CFBD_API_KEY`; unverified in docstring) | Yes (resets `DATABASE_URL`) |
| `scripts/seed_hockey_smoke_test.py` | Smoke test for the NHL universe, roster slot definitions, and draft round-trip | `.venv-api` | No (unverified in docstring) | Yes (throwaway test DB) |
| `scripts/seed_hockey_universe.py` | Idempotently adds the "nhl" universe + roster slot definitions to whatever `DATABASE_URL` points at, so NHL leagues can be created from the same site already hosting CFB leagues | `.venv-api` | No | Yes (`DATABASE_URL`; additive-only, safe to re-run) |
| `scripts/seed_smoke_test.py` | Smoke test round-tripping schema and API endpoints via TestClient | `.venv-api` | No (unverified in docstring) | Yes (throwaway test DB) |
| `scripts/serve_demo.py` | Runs FastAPI server via Uvicorn against local demo dataset | `.venv-api` | No (unverified in docstring) | Yes (copies demo DB) |

## Verification

There is no pytest test suite. Offline verification commands (PowerShell from repository root):
- `.\.venv-api\Scripts\python.exe` `scripts/seed_smoke_test.py`
- `.\.venv-api\Scripts\python.exe` `scripts/seed_auction_smoke_test.py`
- `.\.venv-api\Scripts\python.exe` `scripts/seed_conference_smoke_test.py`
- `.\.venv-api\Scripts\python.exe` `scripts/seed_hockey_smoke_test.py`
- `.\.venv-api\Scripts\python.exe` `scripts/run_mock_e2e.py`
- Path verification: run `Test-Path` in PowerShell for referenced repository file paths.

## Agent rules

- **Branching**: Always create a new branch per task (`git switch -c <branch> <base>`).
- **Git boundaries**: Never push, merge, or commit to main (or master).
- **Protected files**: Never edit `.env` or any `*.db` database files.
- **Scoring and attribution**: Never modify scoring or attribution code (`src/fantasy_sports/sports/`, `src/fantasy_sports/platform/scoring.py`, `src/fantasy_sports/platform/scoring_scale.py`). All scoring and attribution work must be routed to Claude Code.
- **Package management**: No package installs without asking the user.
- **External requests**: No paid or external API calls; never run scripts that fetch live external data.
- **Scope**: Work only inside this repo. Never touch `C:\Users\johna\BuildLab`, any other repo, or the home folder (`C:\Users\johna`). Start every agent from this repo's root, never from the home folder.
- **Stop conditions**: If the working tree is not clean, or the task's premise is already done or wrong, stop and report instead of improvising.

## Handoff routine

Every task follows the same steps, whichever agent runs it:
1. **Pick the agent by task size** (see Routing). Large or scoring work stays with Claude Code.
2. **Write a self-contained prompt** with these sections: GOAL, WORKING DIRECTORY AND BRANCH, FILES IN SCOPE, FILES OFF-LIMITS, BACKGROUND, STEPS, HOW TO VERIFY (exact command and what passing looks like), BOUNDARIES, REPORT BACK. Check its claims against the repo first; stale background wastes a run.
3. **Branch**: the agent creates `handoff/<provider>-<topic>` from the current feature branch, where provider is `codex`, `antigravity` or `claude`.
4. **Run one agent at a time** in this checkout. Two agents switching branches in the same folder will clash.
5. **Verify**: run the stated commands, and commit on the handoff branch only.
6. **Review the diff** (`git diff --stat <base> <handoff-branch>`) and confirm only the files in scope changed. A human decides whether to merge. Agents never merge or push.
7. **Record usage**: note the quota bars before and after (Codex Plan limits panel; Antigravity `/usage`) so the routing shares stay accurate. Full routing and limits are in `claude/model-routing-guide.md`.

Sandbox note: Codex cannot read the global git ignore file, so a stray file that your terminal hides can show up as untracked for Codex and make it stop. Fix such files locally in `.git/info/exclude`.

## Routing

- **Codex**: Bounded medium tasks (isolated features, self-contained endpoints or modules).
- **Antigravity**: Small or high-volume tasks (quick edits, documentation, linting, verification, file management).
- **Claude Code**: Large, cross-cutting tasks, architectural refactoring, or scoring and attribution work.

## Known doc drift

- **Integration branch**: `docs/architecture.md` and `README.md` name master as the integration branch; the only active repository branches are main and feature/timed-auction-draft-mock-run (plus handoff branches). The master branch does not exist in git.
- **Starter slots vs roster count**: `src/fantasy_sports/sports/football/README.md` documents 10 starters, but `docs/data_contract.md` and schema add an 11th starter slot (kicker), requiring 21 roster slots (11 starters + 10 bench) while drafts remain 20 rounds until bench sizing is finalized.
- **Alembic has two migration heads** (`a3f7c1e9d2b4` and, as of the hockey stand-up, `c3d8e1a5f9b2`): `a3f7c1e9d2b4_league_draft_type_and_schedule.py` and `f6c4e0b8d2a3_recruiting_periods.py` both branch off `d4f2a8c1b6e9`, and nothing has merged them back. `alembic upgrade head` fails with "Multiple heads" until a human reconciles the branch (a merge migration, or rebasing one branch onto the other) - this predates the hockey work and isn't caused by it. Target a specific revision id (not the bare `head`) when testing migrations against a throwaway DB until it's fixed.
