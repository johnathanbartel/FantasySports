# CLAUDE.md — Fantasy Sports Company

Project context for Claude Code. Read this before making architectural or scoring decisions. This file is stable; day-to-day state lives in `progress.md`.

## What This Is

A fantasy sports platform built on one core bet: **realistic scoring**, using real play-by-play/EPA data instead of flat per-stat point values. Internal-first — Johnathan is running real leagues himself, starting with college football. Not a public product yet.

Sport-agnostic core, sport-specific layers on top. Football (college first, then NFL) is the current build target. Baseball has a separate, already-designed sibling system (locked-rotation mechanic) — not a template to force football into.

## Non-Negotiable Principles

These override convenience, user requests, and "just this once" exceptions. Check every feature/scoring decision against them before building.

1. **Scoring is realistic-only.** No customizable or "Classic" (ESPN-style flat) scoring modes, ever. No duplicate full credit to multiple players for one play, no flat kicker points. If a feature would require reintroducing flat scoring, don't build it.
2. **Credit the process, not just the finish.** A drive that stalls into a field goal means the offense earned most of the value; the kicker gets a small marginal finishing bonus. This attribution philosophy generalizes to every sport's scoring model, not just football.
3. **Derive target numbers from data, don't assume them.** Don't calibrate to a "feels fun" score range. Pull real data first, model it, let realistic outputs emerge (e.g., NFL ~23 PPG, NCAAF ~30 PPG came out of the data, not the other way around).
4. **Build sport-agnostic wherever possible.** Before finalizing any feature design (scoring, sim-recap, AI-owned teams, universe flexibility), ask: does this only work for football, or does the underlying mechanic generalize?
5. **Flexibility everywhere except the scoring engine.** League structure, universe-building, roster construction: flexible. Scoring engine: not flexible. This is the one place rigidity is intentional.

## Architecture

- Sport-agnostic core schema + sport-specific tables layered on top (22-table schema currently: core + football-specific).
- Alembic migrations for schema changes.
- FastAPI backend.
- `cfbd` (college football data pipeline) pins **pydantic v1** — permanently incompatible with the FastAPI layer's **pydantic v2**. This requires a permanent split virtualenv structure (`.venv-api` for the API layer, separate venv for the data pipeline). This is not a temporary workaround; treat it as a fixed constraint.
- Defense drafted as **position groups** (DL/LB/DB), not individual defensive players, not monolithic team D/ST. Open question: same draft pool as offense, or reserved rounds — unresolved, decide before draft-flow UI is built.
- Play-by-play data (not box scores) is required for any EP/EPA-style scoring model — box scores can't support field-position-based value modeling.

## Data & Modeling Standards

- Validate every scoring model against real outcomes: a fantasy team's weekly total should track reasonably close to that real team's actual points scored that week. Significant, consistent divergence means the model needs revision before shipping — don't ship and patch later.
- Heavy data/pipeline/model work happens in Claude Code with the relevant spec doc as context — not improvised inline in chat-based design conversations.

## Roster, League, and Draft Defaults (Football)

- Roster: 20 players (10 starters, 10 bench). Starters: QB, 2 RB, 2 WR, TE, FLEX, DL group, LB group, DB group.
- League size: customizable, 12 default.
- Draft: snake, 20 rounds, no salary cap in v1.
- Two universes (college, NFL) planned, plus a Dual Universe crossover mode (mechanics TBD).
- AI-owned teams with distinct personalities/traits to fill out leagues (design TBD).

## Sequencing / What NOT to Build Right Now

Current priority: **college football core build** (schema, scoring, draft, league mechanics). NFL follows using the same schema once college football is stable.

Explicitly out of scope until core mechanics are stable:
- Sim-recap animation (premium)
- Salary-cap drafts (premium)
- Rumor mill feature (premium/flavor — see parked spec)

When a premium/future idea comes up mid-build: write it as its own short spec and park it. Don't develop it inline while a current deadline is active.

## Conventions

- `docs/data_contract.md` is the primary handoff artifact between backend and frontend work. Frontend design work (Claude Design) should be scoped to what the current API already supports.
- Before finalizing any new feature: (1) check against the five Non-Negotiable Principles, (2) confirm it doesn't compete with current sequencing priority, (3) if premium/future, spec-and-park rather than build inline, (4) if it involves real data/modeling, hand to Claude Code with the relevant spec as context.
