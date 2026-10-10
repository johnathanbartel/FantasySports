# NHL Series Platform Build Plan

This plan turns `docs/hockey_spec.md` into small, dependency-ordered builds that can be handed to coding agents one at a time. The repository uses one shared checkout, so builds must run sequentially and each ticket must start from the branch containing all of its prerequisites.

## Delivery order

| Order | Ticket | Build | Agent | Depends on | Release tier |
|---:|---|---|---|---|---|
| 1 | FS-020 | Series schema and league settings | Codex | hockey spec | MVP |
| 2 | FS-021 | Series schedule engine and APIs | Codex | FS-020 | MVP |
| 3 | FS-022 | Pecking-order backend and APIs | Codex | FS-020 | MVP |
| 4 | FS-023 | Schedule and lineup viewer | Codex | FS-021, FS-022 | MVP |
| 5 | FS-024 | Series standings, rivalry history, and Game Score | Codex | FS-021 | MVP |
| 6 | FS-025 | Progressive simulator state and event contract | Codex | FS-020, FS-021 | MVP |
| 7 | FS-026 | Hockey xG/GSAx scoring, normalization, and inversion | Claude Code | FS-025 | MVP, protected |
| 8 | FS-027 | Live simulator and reveal frontend | Codex | FS-023, FS-025, FS-026 | MVP |
| 9 | FS-028 | Off-day banking backend | Codex | FS-021, FS-026 | Post-MVP |
| 10 | FS-029 | Off-day banking UI | Codex | FS-023, FS-028 | Post-MVP |
| 11 | FS-030 | Slow/asynchronous draft controls | Codex | existing draft engine | Post-MVP |
| 12 | FS-031 | Draft broadcast room and deterministic commentary | Codex | FS-030 | Post-MVP |
| 13 | FS-032 | External media provider seams | Codex | FS-031 | Optional/integration |
| 14 | FS-033 | Weekly salary-cap redraft backend | Claude Code | FS-020, FS-026 | Later mode |
| 15 | FS-034 | NHL end-to-end hardening and deployment gate | Claude Code | all shipped tickets | Release gate |

## Milestones

### Milestone A: Playable series shell

FS-020 through FS-025 produce series records, daily games, automated lineups, schedule navigation, standings, and a progressive simulation state machine using synthetic events. No hockey scoring or attribution logic is changed.

### Milestone B: Scored NHL series

FS-026 is the protected scoring build. It connects xG/GSAx attribution, normalization, variance, and monotonic event inversion to the simulator contract. FS-027 then renders the live/replay experience.

### Milestone C: Convenience and draft experience

FS-028 through FS-032 add off-day banking, slow drafts, draft announcements, and provider-neutral integration points. Real AI, Spotify, and video/audio services remain disabled until the owner approves vendors, credentials, privacy terms, and external API usage.

### Milestone D: Alternate mode and release

FS-033 adds weekly salary-cap redraft as a separate league mode. FS-034 runs the complete NHL scenario, migration checks, regression scripts, and production-readiness audit.

## Cross-ticket rules

- Run `git status --short --branch` first. Stop if the tree is dirty or the ticket is already complete.
- Create the named `handoff/<provider>-<topic>` branch from the latest prerequisite branch.
- Work only in this repository. Never edit `.env`, database files, or protected scoring files unless the ticket is explicitly assigned to Claude Code.
- Do not install packages or call external APIs without owner approval.
- Every build adds or extends an offline verification script in `scripts/` and runs the relevant existing NHL smoke test.
- Review `git diff --stat <base>...HEAD` before reporting. Commit only on the handoff branch. Push and merge require separate owner confirmation.
- Do not begin FS-027, FS-028, FS-033, or FS-034 with a synthetic scoring substitute if FS-026 is incomplete.

## Deferred product decisions

The following are not safe to choose implicitly inside an implementation ticket:

- AI commentary vendor/model, budget, moderation, and retention policy.
- Video/audio provider and consent/privacy requirements.
- Spotify application credentials, playback entitlement, and account-linking UX.
- Production NHL schedule and event data providers.
- Exact weekly salary-cap adjustment formula.

FS-032 may create disabled interfaces and configuration validation, but must not activate a provider or place credentials in the repository.
