# NHL series release gate and go/no-go checklist (FS-034)

Scope: the NHL series flow as built by FS-020 to FS-033 (series schedule, pecking order, viewer, standings and Game Score, progressive simulator, hockey scoring adapter, simulator UI, off-day banking and UI, slow/async draft clock, draft broadcast room, disabled media seams, weekly redraft backend). Nothing here was deployed, and no production data, credential or external API was touched.

## How to run the gate

```
.venv-api/Scripts/python.exe scripts/run_hockey_release_gate.py            # everything below, one matrix
.venv-api/Scripts/python.exe scripts/verify_hockey_series_e2e.py           # the 12-team, 24-round scenario alone
```

`verify_hockey_series_e2e.py` builds its database with `alembic upgrade head` (so the migrations are under test), then runs one deterministic scenario: league creation (12 teams, 24 rounds, 1 human + 11 AI) -> timed snake draft (288 picks) -> 12 pecking orders -> 66-series schedule -> 7 days of stored shot credits -> 7 published, started, watched/skipped and finished simulated games -> standings, rivalry and Game Score tie-outs -> off-day banking -> authorization, idempotency, missing-table and PostgreSQL-portability audits.

## Command matrix (offline)

| Check | Command | Passing means |
|---|---|---|
| One Alembic head | `python -m alembic heads` | exactly `d6f0b4c8e2a5` (the plan's `b7c3e9a1d4f8` and AGENTS.md's `e7a3c5d1f9b8` are both stale) |
| Schema = migrations | e2e script | every ORM table/column exists after `alembic upgrade head`, except the 2 known football tables below |
| PostgreSQL portability | e2e script | all 59 tables compile as PostgreSQL DDL; the whole migration chain renders as PostgreSQL SQL offline; no check constraint compares a boolean with 0/1; no SQLite-only SQL |
| Series schedule / pecking order / standings / viewer | `verify_hockey_series_schedule.py`, `verify_hockey_pecking_order.py`, `verify_hockey_series_standings.py`, `verify_hockey_schedule_viewer.py`, `verify_hockey_series_schema.py` | each prints its "All ... passed" line |
| Simulator core, scoring, UI | `verify_simulator_core.py`, `audit_hockey_simulator_scoring.py`, `verify_hockey_simulator_ui.py` | exact tie-outs and seeded replays pass |
| Banking | `verify_hockey_banking.py`, `verify_hockey_banking_ui.py` | pass |
| Draft clock, broadcast, media seams | `verify_async_snake_draft.py`, `verify_draft_broadcast.py`, `verify_draft_media_seams.py` | pass |
| Weekly redraft | `verify_weekly_redraft.py` | pass |
| Shared regression | `seed_smoke_test.py`, `seed_hockey_smoke_test.py`, `seed_hockey_league_creation_smoke_test.py`, `seed_duplicate_pick_smoke_test.py`, `seed_auction_smoke_test.py`, `seed_conference_smoke_test.py`, `run_mock_e2e.py` | pass |
| Hygiene | `python -m py_compile` on changed modules, `git diff --check` | clean |

## Defects found by the end-to-end run and fixed

1. **A 360-player draft pool cost ~1000 lazy queries per request**, so a 288-pick draft took many minutes. `platform/defense_modes.league_pool` now eager-loads the player, universe and defense group (perf-only; same results). Football/NFL pools benefit equally.
2. (Test-harness only) Session autoflush is off, so an in-session off-day flag must be flushed before the banking service reads it; production paths commit first.

## Gate result (2026-10-11)

`run_hockey_release_gate.py --skip-e2e`: **24 of 25 passed**; `verify_hockey_series_e2e.py` run separately: **passed** (all checks, including 288 picks, 7 revealed games, standings tie-outs, banking, authorization sweep, missing-table behaviour and PostgreSQL portability). The one failure, `run_mock_e2e.py`, is **pre-existing and environmental**: it copies the local `user_run.db`, which predates FS-020 (`table leagues has no column named series_length_days`) and fails the same way on `main`. AGENTS.md already says older local databases must be upgraded with `alembic upgrade head`; this ticket may not edit `*.db` files, so it was left unchanged. It is a football script and does not exercise the NHL flow.

## Findings that are NOT fixed (decisions or out-of-scope work)

| Item | Why it matters | Needed |
|---|---|---|
| Two ORM tables, `player_season_points` and `player_recruit_ratings`, have no Alembic migration | An Alembic-built database lacks them. Code that reads season points already tolerates this (draft board, broadcast); the football recruiting pages that read them would fail on such a database | Owner decision: add migrations, or confirm those football features are not deployed on the migrated database |
| Platform autopick is position-blind (takes the first asset on the board) | AI teams can end up unable to fill a starter slot (no goalie). In this scenario's pool none did (checked), but it is luck of the pool, not a guarantee | Product decision on AI drafting (the existing TODO in `routers/draft.py`) |
| Banked credits are stored and assigned but **not applied to the simulated scoreboard** | The UI promises "banked to Thursday" but the Thursday game does not use the points yet | Add a `banked` credit role to the protected hockey adapter (scoring owner) |
| Off days are never *marked* (every series day is a matchup day) | Nothing creates off-day credits in practice | Build the off-day marking step and the job that ingests credits and calls the 24-hour fallback |
| Nothing publishes or starts simulated games on a schedule | Games exist only when `loader.publish_game` and `/simulator/games/{id}/start` are called | A scheduler/job decision (none exists by design) |
| Standings expose finished game results regardless of a viewer's spoiler veil | Standings read FINAL games league-wide | Product decision on veiling standings |
| Hockey scoring constants (`T=6.0`, `p=1.0`, `sigma=0.5`, cap `1.0`) are provisional defaults | Every scoreboard depends on them | Owner approval or fitting (`docs/hockey_simulator_scoring.md`) |
| Weekly-redraft cap adjustment formula and salaries source | Mode runs with a fixed cap and commissioner-published salaries only | Owner decision (`docs/weekly_redraft.md`) |
| Draft clock time zone is a fixed UTC offset | Does not follow daylight saving | Change the offset when clocks change, or add tz support |
| Media (AI commentary, music, video chat) | Interfaces only; nothing can be enabled | Vendor, privacy, cost decisions (`docs/draft_media_providers.md`) |
| Manual browser checks so far covered desktop/mobile layouts and keyboard/focus basics, not screen readers, real speech output, or a real network drop | Accessibility sign-off | A manual pass with assistive technology |

## Go / no-go

**Go for an internal, owner-only pilot of the shipped series flow** (create league, draft, pecking orders, schedule, publish and reveal simulated games, standings) on a fresh or migrated database, **provided**:

1. `alembic upgrade head` is run against every database before deploying this code (new tables and columns: simulator, banking, draft clock, weekly redraft). The Neon database is not upgraded automatically.
2. The owner accepts the provisional scoring constants, or approves changes.
3. Someone (or a later ticket) publishes and starts games; there is no scheduler.

**No-go for** treating banking as live (points not applied), media features (disabled by design), weekly redraft as a product (backend only, no UI, no settlement), and any public launch before the football-table migration question is answered.

## Deployment impact

This work adds five migrations (`a1c5e9b3d7f2`, `b4d8f2a6c0e1`, `c5e9a3b7d1f4`, `d6f0b4c8e2a5` on top of `e7a3c5d1f9b8`) and new static files. A push to `main` would trigger the Vercel deploy; **do not push or deploy without a new, explicit owner confirmation** (AGENTS.md "Push and merge"). The branches are local and stacked: `handoff/codex-progressive-simulator-core` -> `handoff/claude-hockey-simulator-scoring` -> `handoff/codex-hockey-simulator-ui` -> `handoff/codex-hockey-off-day-banking` -> `handoff/codex-hockey-banking-ui` -> `handoff/codex-async-snake-draft` -> `handoff/codex-draft-broadcast-room` -> `handoff/codex-draft-media-provider-seams` -> `handoff/claude-hockey-weekly-redraft` -> `handoff/claude-hockey-series-e2e`.
