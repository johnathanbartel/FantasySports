# progress.md — Fantasy Sports Company

Current state tracker. Update this as work lands; keep `CLAUDE.md` for stable principles/architecture.

_Last updated: 2026-08-13_

## Current Focus

College football core build (schema, scoring, draft, league mechanics). Not yet ready for onboarding/frontend polish — two backend gaps block that (see below).

## Done

- Sport-agnostic core + football-specific schema scaffolded (22 tables) via Claude Code.
- Alembic migrations in place.
- Data contract document: `docs/data_contract.md` (primary backend↔frontend handoff artifact).
- 15 API endpoints built.
- Smoke test script in place.
- Split virtualenv structure identified as a permanent constraint (`cfbd` pydantic v1 vs. FastAPI pydantic v2) — documented in CLAUDE.md, not treated as a bug to fix.

## In Progress / Immediate Next Steps

1. **Build create-team endpoint** — missing, blocks onboarding flow design.
2. **Build join-league endpoint** — missing, blocks onboarding flow design.
3. **Begin Claude Design frontend pass**, scoped to screens the current API already supports (do this in parallel with #1–2, not after):
   - League view
   - Draft board
   - Roster management
   - Lineup-setting
   - Weekly score breakdown
   - Standings
   - (Onboarding screens deferred until #1–2 land)

## Open Decisions (Unresolved)

- Defense draft pool: same pool as offense, or reserved rounds for DL/LB/DB groups? Needs a decision before draft-flow UI is built.
- Dual Universe crossover mechanics: TBD.
- AI-owned team personality/trait design: TBD.

## Parked (Do Not Build Yet)

- **Sim-recap animation** (premium) — blocked on realistic scoring being stable enough to produce believable highlight reels.
- **Salary-cap drafts** (premium) — v1 is snake draft only.
- **Rumor mill** (premium/flavor) — AI-generated fake journalism tied to team events, user-submitted fake leaks as bluffs, owner-refute mechanic. Sport-agnostic by design. Has a drafted phased timeline (Spec → Park → Design → Build → Validate) with a flat checklist ready to paste into a to-do app. Fairness design note: three rumor tiers (real analysis-driven, user-submitted fake, AI flavor-text) must stay distinguishable to avoid competitive information leakage. **Status: Parked**, not started.

## On the Horizon (Post Core-Stability)

- NFL support — same schema, follows college football stabilization.
- Baseball module — locked-rotation mechanic already designed as a sibling system; not a football template retrofit.
