# Fantasy Sports Platform — Data Contract (Football: College + NFL)

For handoff to design work on UI mockups. Describes what's actually built and running (schema in `db/models/`, API in `api/`), not aspirational — every entity and endpoint below has been verified end to end against a real database (`scripts/seed_smoke_test.py` for the core/snake-draft path, `scripts/auction_smoke_test.py` for the Timed Auction Draft).

## How to read this

Two families of tables: **sport-agnostic** (leagues, teams, rosters, draft, calendar — reusable by baseball/basketball/hockey later without redesign) and **football-specific** (raw play-by-play, EPA credit attribution — genuinely football-shaped, doesn't generalize). A full OpenAPI schema is also available live at `/docs` (Swagger UI) whenever the API is running — that's the machine-readable version of everything below.

## Core concepts

- **Universe**: a specific league context — "college football" and "NFL" are two different universes of the same sport. A league usually belongs to one universe; a Dual Universe league (mechanics not yet built) can belong to two.
- **Draftable asset**: anything that can be drafted, rostered, or started. Either a real **player**, or a **defense group** (e.g. "Georgia DL" — defense is drafted as a position-group unit, not individual defenders). UI should treat these as one pickable pool, not two separate lists — see the draft-pool decision below.

## Sport-agnostic tables

| Table | Purpose | Key fields | Relates to |
|---|---|---|---|
| `users` | A person using the platform | email, display_name | owns leagues, owns teams |
| `universes` | Reference list of league contexts | sport, level, code ("ncaaf"/"nfl") | referenced by leagues, players, real teams |
| `leagues` | A fantasy league | name, size (default 12), draft_rounds (default 20), salary_cap_enabled (placeholder, unused) | has teams, has one or more universes |
| `league_universes` | Which universe(s) a league covers | — | join table; 2 rows = Dual Universe |
| `teams` | A fantasy team within a league | name, owner_user_id (null if AI-owned), is_ai_owned, personality_profile (JSON, AI trait hook — trait system itself not built) | belongs to a league; owned by a user OR AI |
| `real_teams` | A real-world team (Georgia, Chiefs, ...) | name, conference, classification | scopes players, games, plays |
| `players` | A real player | full_name, position, universe_id | scoped to exactly one universe — never spans college/NFL |
| `defense_groups` | A real team's draftable DL/LB/DB unit | real_team_id, position_group | one row per real team per group |
| `draftable_assets` | Unifies players + defense groups into one pickable pool | asset_type ("player"/"defense_group") | referenced by draft picks, roster entries, lineup entries |
| `roster_slot_definitions` | Defines starting-lineup structure per universe, as data | slot_code (QB/RB/WR/TE/FLEX/DL/LB/DB/BENCH), eligible_positions, slot_count | a different sport gets its own rows here, no schema change |
| `drafts` | One draft event for a league | status | has many draft_picks |
| `draft_picks` | One pick slot, pre-generated in snake order at draft creation | round, pick_number, team_id, draftable_asset_id (null until picked) | fills in as the draft runs |
| `roster_entries` | A team's season-long 20-man roster | team_id, draftable_asset_id | populated by draft picks |
| `weeks` | Maps a fantasy week to a universe's real calendar | universe_id, season_year, week_number | scopes games, lineups, scores |
| `lineup_entries` | A team's starters for one specific week | team_id, week_id, draftable_asset_id, roster_slot_id | separate from roster_entries — this is what changes weekly |
| `games` | Real-world schedule | home/away real_team_id, scores, status | scopes plays |
| `weekly_matchups` | Team vs. team for a week | team_a/b_id, team_a/b_score, winner_team_id | drives standings |
| `weekly_team_scores` | A team's total for a week | total_points | sum of that team's active lineup's player scores |
| `auction_sessions` | One Timed Auction Draft "Recruiting Period" | draft_id, total_rounds, round_seconds, minimum_round_seconds | hangs off one `drafts` row, so auction wins are ordinary draft picks |
| `auction_tiers` | Budget tiers by program size, snapshotted per session | code (large/moderate/small), starting_budget | a sport with different tier names gets its own rows, no schema change |
| `auction_budgets` | One team's wallet in one auction | team_id, auction_tier_id, starting_budget, spent_budget | debited when that team wins a bid |
| `auction_rounds` | One per-slot round and its bidding window | round_number, status, opens_at, closes_at | windows tile the Recruiting Period back to back |
| `auction_bids` | One owner's single blind bid in one round | auction_round_id, team_id, draftable_asset_id, amount, status | unique per (round, team) - the one-bid rule made structural |
| `auction_round_results` | One asset's outcome in a resolved round | winning_team_id, winning_amount, tie_break_used, tied_team_ids | as many rows per round as there were assets bid on |

**Standings are not a stored table** — computed on read from `weekly_matchups` (wins/losses/points for/against), so they can never drift out of sync with the games that produced them.

## Football-specific tables

| Table | Purpose | Key fields |
|---|---|---|
| `plays` | Raw play-by-play, one row per real play | down, distance, yard_line, yards_gained, play_type, play_text, ppa (the EPA-equivalent value of the play) |
| `play_credits` | How one play's `ppa` is split among the players/groups involved | play_id, draftable_asset_id, role (passer/receiver/rusher/dl/lb/db/kicker), credit_points |
| `weekly_player_scores` | A player/group's total for a week | draftable_asset_id, week_id, points — aggregate of play_credits, recomputable, not independently authoritative |
| `weekly_real_team_epa_totals` | Validation-only: a real team's modeled total vs. its actual real score that week | Not part of live fantasy scoring — this is the spec's "does the model track reality" check, persisted |

A player's fantasy score for a play is never the full play value credited twice — `play_credits` rows for the same play sum back to that play's actual `ppa`, by construction (see `src/fantasy_sports/sports/football/shared/attribution.py` for the underlying logic this schema stores the output of).

## Decision: DL/LB/DB draft pool

**Same pool as offensive players**, not reserved rounds. A defense group can go in round 4 right alongside a WR2 if a manager values it that highly. Reasoning: matches the platform's own stated intent (real strategic trade-offs), and is the *simpler* schema — reserved rounds would need an extra table locking specific rounds to specific slot types, which same-pool doesn't need. Verified working in the smoke test (a defense group and a player were drafted back-to-back with no special-casing).

## Decision: Timed Auction Draft defensive position bidding granularity

**Bid on individually, per group** — never bundled by real team. In the Timed Auction Draft, each defensive position group is its own auction line: a manager can bid on and win Georgia's DB group and Notre Dame's DL group as two separate, independent bids, and a single real team's DL/LB/DB groups are never combined into one bid line. This is consistent with the `DL/LB/DB draft pool` decision above, which already establishes each `defense_groups` row (one per real team per position group — DL, LB, DB) as an atomic `draftable_assets` entry; this decision confirms that same per-group unit is also the atomic bidding unit in the auction's round resolution logic. No additional bundling table or bundling logic is required.

**Now implemented.** The round resolution engine applies this rule as written - see [docs/timed_auction_draft.md](timed_auction_draft.md). Because the atomic bidding unit is a single `draftable_assets` row, defense groups travel the same bid/reveal/debit/tie-break code path as players, and no bundling logic exists anywhere in the engine.

## Key API actions

All implemented and verified against a real database; full request/response shapes at `/docs`.

| Action | Endpoint |
|---|---|
| Create a league | `POST /leagues` |
| View league settings | `GET /leagues/{id}` |
| View standings | `GET /leagues/{id}/standings` |
| View a team | `GET /teams/{id}` |
| View a team's roster | `GET /teams/{id}/roster` |
| View available free agents | `GET /teams/{id}/free-agents` |
| Start a draft (generates the full snake pick order) | `POST /drafts/start` |
| View the draft board (what's left) | `GET /drafts/{id}/board` |
| Make a draft pick | `POST /drafts/{id}/pick` |
| View pick history | `GET /drafts/{id}/picks` |
| Autopick (AI-owned teams) | `POST /drafts/{id}/autopick` |
| Open a Timed Auction Draft (Recruiting Period) | `POST /auctions/start` |
| View auction state + current round clock | `GET /auctions/{id}` |
| View the revealed auction pool | `GET /auctions/{id}/board` |
| View every team's tier and remaining budget | `GET /auctions/{id}/budgets` |
| Submit one blind bid | `POST /auctions/{id}/bids` |
| View a round (bid count only while open, full reveal after) | `GET /auctions/{id}/rounds/{n}` |
| Resolve a round (reveal -> debit -> advance) | `POST /auctions/{id}/rounds/{n}/resolve` |
| View everything won in the auction so far | `GET /auctions/{id}/results` |
| Set a weekly lineup | `PUT /teams/{id}/lineup` |
| View a weekly lineup | `GET /teams/{id}/lineup` |
| View a player's weekly score breakdown (per-play detail) | `GET /players/{id}/weekly-score` |
| View a team's weekly total | `GET /teams/{id}/weekly-score` |
| View a matchup result | `GET /matchups/{id}` |

**Note for design purposes:** there's no "create team" / "join league" endpoint yet — teams are currently created directly, not through the API. If mockups need a "join league" flow, flag it back; it's a natural next endpoint, just not built in this pass.

## What's real vs. stubbed

Built and working: the full schema, snake draft generation and picking, lineup setting, standings, and reading back a play-level score breakdown — all backed by real data (`scripts/seed_demo_league.py` seeds a 12-team league with real CFBD rosters and three weeks of real games run through the actual attribution pipeline, `src/fantasy_sports/sports/football/college/pipeline.py`, not synthetic placeholders).

Also built and working: the **Timed Auction Draft** - blind per-slot rounds, tiered budgets, the reveal/debit/advance cycle, and the random "player choice" tie-break (see [docs/timed_auction_draft.md](timed_auction_draft.md)). It is covered by `pytest` and by `scripts/auction_smoke_test.py`, which runs a whole Recruiting Period through the real HTTP routes. The CFBD-seeded demo league (`scripts/seed_demo_league.py`) still creates snake drafts only, so an auction has not yet been run over a full real roster set.

Explicitly stubbed (endpoints exist and respond, but don't enforce yet — noted with TODOs in the code):
- Draft-turn enforcement (any team can currently make the next open pick, not just the team "on the clock")
- Lineup legality validation (position-eligibility against `roster_slot_definitions`, roster ownership)
- AI personality-driven autopick (currently just takes the first available board asset — `personality_profile` isn't consulted)
- Bidder identity in the auction (any caller can bid as any team, same posture as draft-turn enforcement above). Blind bids are not readable by anyone while a round is open, but they are overwritable by an impostor until real users exist

**Fixed: kickers now have a starter slot.** `roster_slot_definitions` has a `K` slot (`slot_count=1`, `eligible_positions=["PK"]` — CFBD's own position code for kickers, not `"K"`; a slot filtering on `"K"` would silently never match a real player). Verified against the reseeded demo league: all 36 team-weeks (12 teams × 3 weeks) fill exactly 11 starters including a real kicker.

**New consequence worth resolving before lineup-legality validation gets built:** adding an 11th starter without shrinking `BENCH` (still `slot_count=10`) means the slot definitions now want 21 total roster spots (11 starters + 10 bench), while the roster itself is still 20 players (`draft_rounds` is unchanged at 20). Nothing breaks today since legality isn't enforced, but once it is, this needs a decision: shrink `BENCH` to 9, or grow the roster past 20.

Out of scope per the original brief: salary-cap draft logic (the `salary_cap_enabled` field exists and is still unused - the auction uses its own `auction_budgets` rather than the salary cap), sim-recap animation data, Dual Universe crossover mechanics, baseball's locked-rotation tables.
