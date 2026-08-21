# Repository architecture

This repository is one deployable site and one Python application. Sports are
feature modules inside that application, not separate products or branches.

```text
src/fantasy_sports/
  api/                 HTTP routes for the single site
  platform/            shared leagues, drafts, rosters, schedules, and database
  sports/
    football/          football models, scoring, and college/pro adapters
    baseball/          baseball adapter boundary
    basketball/        basketball adapter boundary
    hockey/            hockey adapter boundary
web/static/            browser assets served by the API
migrations/            one schema history for the whole platform
scripts/               repository-level operational commands
docs/                  architecture and cross-team contracts
```

## Ownership rules

- Put reusable league behavior in `platform`; never copy it into a sport.
- Put scoring, provider ingestion, and sport-only tables in `sports/<sport>`.
- Expose all user-facing behavior through `api`; there is one web deployment.
- Add schema changes to the single `migrations` history.
- Keep short-lived local data and credentials out of Git.

## Branch policy

`master` is the integration branch and the source of truth for the site. Feature
branches should be short-lived and merged back into `master`; do not maintain a
per-sport long-running branch. The stale remote `main` branch predates the active
application and can be removed from the remote after repository-admin review.

