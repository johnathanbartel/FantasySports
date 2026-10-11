# NHL weekly salary-cap redraft (FS-033)

An explicit league mode (`leagues.league_mode = 'weekly_redraft'`), separate from the multi-day series format. Each week every team builds a fresh six-player roster under a salary cap and faces one private-league opponent. There is no money in this feature: no entry fees, prizes, odds or payouts, and no external provider.

Code: `platform/weekly_redraft.py`, `api/routers/weekly_redraft.py`, migration `d6f0b4c8e2a5`, verification `scripts/verify_weekly_redraft.py`.

## Decision record (blocker)

**The cap adjustment formula is NOT decided and NOT implemented.** The spec says only "team salary caps adjust based on the previous week's performance" (docs/hockey_spec.md 7.3). No formula is recorded in the ticket or any decision doc, so this build uses the neutral fixed cap the ticket allows: `cap_policy = FIXED`, `base_cap` chosen by the commissioner (default `NEUTRAL_FIXED_CAP = 50000` salary units, a placeholder, not a tuned value). The cap is snapshotted onto each contest (`cap_applied`) and each entry, so adding an adjustment policy later cannot rewrite past weeks. To proceed the owner must record: the input (finish rank, points, margin), the adjustment per rank, floors and ceilings, and what happens in week 1.

Also open: where salaries come from. There is no projection or pricing source for NHL, so **salaries are an input**: the commissioner publishes a price per player per contest, and an unpriced player cannot be rostered. Nothing is estimated.

## Rules

- Roster: 1 C, 2 W (LW/RW accepted), 2 D, 1 G; a player once per roster; the pool is the league's active players.
- Cap: roster total <= the contest cap, enforced on every save and again at submit; the database also refuses an entry whose total exceeds its cap.
- Lifecycle: contest `OPEN` until `lock_at` then `LOCKED` (applied lazily on read). A saved roster is a `DRAFT` (replaceable, idempotent); `submit` makes it final (idempotent). A draft never submitted does not count and cannot be submitted after the lock. Nothing changes after the lock; salaries cannot change once any roster exists or after the lock.
- Pairing: circle method over team ids (as the series schedule), one round per week, so each team has exactly one opponent per week and opponents rotate; it needs an even team count.
- Privacy: before the lock a team cannot see its opponent's roster (only whether they have saved/submitted); after the lock the opponent's submitted roster is shown. AI-owned teams cannot be edited over HTTP and there is no auto-fill, so an AI team has no entry (not built).
- Mode exclusivity: a league with a series schedule cannot switch, and a weekly-redraft league cannot generate a series schedule.

## Routes

`POST/GET /leagues/{id}/weekly-redraft` (enable with `base_cap`, read), `POST /leagues/{id}/weekly-redraft/contests` (`start_date`, `end_date`, `lock_at` naive UTC), `POST /weekly-redraft/contests/{id}/salaries` (`{salaries: {assetId: salary}}`), `GET /weekly-redraft/contests/{id}?team_id=`, `PUT /weekly-redraft/contests/{id}/teams/{team}/roster` (`{slots: {C:[..], W:[..], D:[..], G:[..]}}`), `POST .../teams/{team}/submit`. Commissioner: enable, create, salaries. Owner: roster and submit. Errors are `{code, message}`: `CAP_EXCEEDED`, `DUPLICATE_PLAYER`, `INVALID_POSITION`, `UNPRICED_PLAYER`, `NOT_IN_POOL`, `TOO_MANY`, `INCOMPLETE`, `NO_SALARIES`, `NO_ROSTER`, `LOCKED`, `ALREADY_SUBMITTED`, `HAS_ENTRIES`, `HAS_SERIES`, `CAP_LOCKED`.

## Not built

Settlement (scoring a week's rosters from FS-026 credits and recording a winner), the cap adjustment, salary generation, AI team auto-fill, and any UI.
