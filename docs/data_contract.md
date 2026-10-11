# Fantasy Sports Platform — Data Contract (Football: College + NFL)

For handoff to design work on UI mockups. Describes what's actually built and running (schema in `src/fantasy_sports/platform/db/models/`, API in `src/fantasy_sports/api/`), not aspirational — every entity and endpoint below has been verified end to end against a real database (`scripts/seed_smoke_test.py`).

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
| `leagues` | A fantasy league | name, size (default 12), draft_rounds (default 20), salary_cap_enabled (placeholder, unused), program_size_mode ("team_choice"/"dynamic" — "dynamic" stubbed, rejected at creation), draft_type ("recruit"/"snake", default "snake"), draft_scheduled_at (nullable, "snake" leagues only), series_length_days (nullable; NHL: 5/7/10, default 7) | has teams, has one or more universes, has conferences and multi-day series |
| `league_universes` | Which universe(s) a league covers | — | join table; 2 rows = Dual Universe |
| `conferences` | Groups a league's teams by program_size tier | league_id, program_size, name | every league starts with 3 (one per tier); grows one full tier-set at a time |
| `teams` | A fantasy team within a league | name, owner_user_id (null if AI-owned), is_ai_owned, program_size ("large"/"moderate"/"small" — sets auction starting budget + conference + season_goal, default "small"), conference_id, personality_profile (JSON, AI trait hook — trait system itself not built) | belongs to a league and a conference; owned by a user OR AI |
| `real_teams` | A real-world team (Georgia, Chiefs, ...) | name, conference, classification | scopes players, games, plays |
| `players` | A real player | full_name, position, universe_id | scoped to exactly one universe — never spans college/NFL |
| `defense_groups` | A real team's draftable DL/LB/DB unit | real_team_id, position_group | one row per real team per group |
| `draftable_assets` | Unifies players + defense groups into one pickable pool | asset_type ("player"/"defense_group") | referenced by draft picks, roster entries, lineup entries |
| `roster_slot_definitions` | Defines starting-lineup structure per universe, as data | slot_code (QB/RB/WR/TE/FLEX/DL/LB/DB/BENCH), eligible_positions, slot_count | a different sport gets its own rows here, no schema change |
| `drafts` | One snake draft event for a league | status | has many draft_picks |
| `draft_picks` | One pick slot, pre-generated in snake order at draft creation | round, pick_number, team_id, draftable_asset_id (null until picked) | fills in as the draft runs |
| `auction_drafts` | One timed sealed-bid auction draft for a league | status, round_seconds, nomination_order, next_nominator_index | has many auction_rounds; has one auction_team_budgets row per team |
| `auction_team_budgets` | A team's auction budget, snapshotted from its program_size at auction start | starting_budget, remaining_budget, assets_won | debited as that team wins rounds |
| `auction_rounds` | One nominated asset up for bid, with its timed window | round_number, draftable_asset_id, opens_at/closes_at, status (open/resolved/unsold), winning_team_id, winning_bid_amount, was_tie_broken_randomly | has many auction_bids |
| `auction_bids` | A team's single sealed bid for a round, overwritable while open | auction_round_id, team_id, amount | hidden from other teams until the round resolves |
| `roster_entries` | A team's season-long 20-man roster | team_id, draftable_asset_id | populated by draft picks |
| `weeks` | Maps a fantasy week to a universe's real calendar | universe_id, season_year, week_number | scopes games, lineups, scores |
| `lineup_entries` | A team's starters for one specific week | team_id, week_id, draftable_asset_id, roster_slot_id | separate from roster_entries — this is what changes weekly |
| `games` | Real-world schedule | home/away real_team_id, scores, status | scopes plays |
| `weekly_matchups` | Team vs. team for a week | team_a/b_id, team_a/b_score, winner_team_id | drives standings |
| `weekly_team_scores` | A team's total for a week | total_points | sum of that team's active lineup's player scores |
| `league_series` | One 5-, 7-, or 10-day fantasy matchup between the same two teams | league_id, team_a/b_id, start/end_date, total_days, team_a/b_wins, status, timestamps | belongs to a league; has daily games |
| `series_daily_games` | One independently scored calendar day within a league series | calendar_date, is_matchup_day, game_score_rating, home/away team and score, current_period, seconds_remaining, status, timestamps | belongs to one league_series; one row per series/date |

**Standings are not a stored table** — computed on read from `weekly_matchups` (wins/losses/points for/against), so they can never drift out of sync with the games that produced them.

### NHL series compatibility

NHL leagues replace disconnected weekly matchups with a fixed opponent series lasting 5, 7, or 10 days. `POST /leagues` accepts `series_length_days` for any league containing the `nhl` universe and defaults it to 7; the selected value is returned in `LeagueOut`. The column is nullable so existing non-NHL leagues and all weekly football behavior remain unchanged. Supplying the setting for a league without NHL is rejected rather than silently changing that league's format. During migration, existing NHL leagues receive the 7-day default and all other existing leagues remain null.

`league_series.status` is `SCHEDULED`, `IN_PROGRESS`, or `FINAL`. `series_daily_games.status` is `SCHEDULED`, `LIVE_SIMULATING`, or `FINAL`; its score, three-period clock, and 0–100 game-score rating are persisted independently for each day. Database checks enforce valid lengths, status values, score/rating/clock ranges, distinct opponents, and date ordering. Uniqueness on `(series_id, calendar_date)` prevents two games for the same series teams on one date, with an additional team/date uniqueness constraint guarding exact duplicate fixtures. These tables are persistence only: schedule generation, simulation/scoring, and series standings calculation are not implemented here (standings and Game Score are derived in the section below), and `weekly_matchups` is unchanged.

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

## Decision: auction draft budget & round resolution

**Sealed-bid, not live ascending.** A nominated asset gets one fixed-length bidding window (`round_seconds`); every team submits one hidden bid (overwritable while the window is open); at close, all bids reveal at once, the highest wins, and their `auction_team_budgets.remaining_budget` is debited by the winning amount. Ties for the top bid are broken by **random choice among the tied teams** — there's no secondary tiebreaker (earlier bid, nomination order, etc.) by design, to keep the rule simple and unexploitable. A round with zero bids resolves as `"unsold"` and the asset returns to the board for a future nomination, rather than forcing the nominating team to take it at a floor price.

**Budget comes from `Team.program_size`, not the league.** Each team picks/is assigned a size tier (large/moderate/small — see `platform/auction/constants.py` for the dollar amounts) and that team alone gets that budget, snapshotted onto `auction_team_budgets` when the auction starts so a later `program_size` change can't retroactively alter a budget already being spent. This is deliberately narrow: program_size is described (by the person commissioning this feature) as eventually also driving each team's conference placement and season goals, plus a commissioner-configurable "dynamic mode" where a team starts small and has to build up across multiple seasons — none of that is built here, only the auction-budget piece.

**Defense groups share the same auction pool as players** — same decision as the snake draft's DL/LB/DB same-pool call above, just applied to `auction.engine.available_assets()` instead of `drafts/{id}/board`. No reserved rounds, no separate budget bucket; a defense group can be nominated and bid on like any player.

**Round resolution currently trusts the caller**, same gap as the snake draft's `pick`/`autopick` endpoints: `nominate` and `bids` take `team_id` in the request body with no authentication behind it. `resolve` has a `force=true` override (meant for a commissioner) to close a round before its timer expires — normal play should let the timer run out.

## Decision: conferences & season goals

**Every league starts with exactly 3 conferences** — one per program_size tier (large/moderate/small) — created automatically at `POST /leagues`. A team is placed into its tier's **least-populated** conference at creation (simple load balancing; there's no draft/rebalancing once assigned). Conference count only ever grows **one full tier-set at a time** (`POST /leagues/{id}/conferences/expand` adds one large + one moderate + one small conference, never a single one) — this is the commissioner's stated rule that conference count must grow in multiples of however many program-size tiers are in use. Per-league tier subsetting (a league that only offers 2 of the 3 tiers, so its multiple would be 2 instead of 3) isn't implemented — every league gets all 3 tiers today.

**Season goals are a fixed, informational label per tier** — Large: "Win the championship," Moderate: "Make the playoffs," Small: "Win the conference" (`Team.season_goal`, a computed property, not a stored column — see `platform/team_tiers.py`). They don't feed into scoring or standings. The commissioner's stated longer-term intent is that in a multi-season league, reaching or missing a goal could raise or lower a team's budget or tier the next season — that needs a season-rollover concept this schema doesn't have at all yet, so it's explicitly out of scope here.

**"Dynamic" program_size_mode is schema-only, rejected at the API layer.** A league's `program_size_mode` accepts `"team_choice"` (teams pick their own size, the only usable option today) or `"dynamic"` (every team starts "small" and is promoted/relegated by performance) — but `POST /leagues` rejects `"dynamic"` with a clear "not implemented yet" error, because dynamic mode was explicitly described as multi-season-only and there's no multi-season concept to hang promotion/relegation off of. The column accepts the value now so turning dynamic mode on later doesn't need another migration.

**The first real "create team" endpoint.** Every team before this was inserted directly via the ORM (seed scripts, smoke tests) — `POST /teams` is the first one a real client (the team-creation screen the commissioner described) could call. It takes `program_size` and handles conference placement; it does not yet handle "join an existing league" flows, auth, or a real user-session concept.

## Decision: league + team creation wizard, and branching by draft_type

**`draft_type` is chosen once, at league creation, and never changes.** `POST /leagues` now takes `draft_type` ("recruit" or "snake", default "snake") and, for "snake" leagues, an optional `draft_scheduled_at`. It's a plain column on `leagues`, not a separate table, because unlike `drafts`/`auction_drafts` (one row per actual draft *event*), this is a standing league setting decided before any draft event exists — it's what tells the wizard which event to point a newly created team at.

**"Recruit" is the user-facing name for the existing auction draft** (`auction_drafts`/`auction_rounds`/etc.) — no new backend draft engine, just new terminology and a new entry screen (`web/static/start-recruiting.html`) in front of `POST /auctions/start`. Renamed here because "recruiting period" is how the commissioner described this draft type to the wizard, not "auction."

**The wizard (`web/static/create-league.html`) is a two-step form**: league details (including `draft_type`) via `POST /leagues`, then team details via `POST /teams` using the league id from step one. After team creation it branches purely on the just-created league's `draft_type`:
- `"recruit"` → `start-recruiting.html?league_id=&team_id=`, a screen that starts the recruiting period (`POST /auctions/start`) on demand and hands off to `auction-board.html`.
- `"snake"` → `roster.html?league_id=&team_id=`, a new roster-viewing screen (the dead `Roster` nav link on the other screens now points here too) that shows `draft_scheduled_at` if the commissioner set one, or a "not yet scheduled" message otherwise.

**`draft_scheduled_at` is stored and returned as naive UTC**, same convention as every other timestamp on this platform (see the auction round timer note below) — `roster.html` has to explicitly treat the API's timestamp as UTC before formatting it for display, rather than handing it to `new Date(...)` as-is, or it renders in the wrong hour for any browser not in UTC.

## Decision: Timed Auction Draft defensive position bidding granularity

**Bid on individually, per group** — never bundled by real team. In the Timed Auction Draft, each defensive position group is its own auction line: a manager can bid on and win Georgia's DB group and Notre Dame's DL group as two separate, independent bids, and a single real team's DL/LB/DB groups are never combined into one bid line. This is consistent with the `DL/LB/DB draft pool` decision above, which already establishes each `defense_groups` row (one per real team per position group — DL, LB, DB) as an atomic `draftable_assets` entry; this decision confirms that same per-group unit is also the atomic bidding unit in the auction's round resolution logic. No additional bundling table or bundling logic is required.

## Key API actions

All implemented and verified against a real database; full request/response shapes at `/docs`.

| Action | Endpoint |
|---|---|
| Create a league (auto-creates 3 baseline conferences; sets draft_type) | `POST /leagues` |
| View league settings | `GET /leagues/{id}` |
| View a league's conferences | `GET /leagues/{id}/conferences` |
| Grow conferences (adds one per tier, commissioner action) | `POST /leagues/{id}/conferences/expand` |
| View standings | `GET /leagues/{id}/standings` |
| Create a team (picks program_size, placed into a conference) | `POST /teams` |
| View a team | `GET /teams/{id}` |
| View a team's roster | `GET /teams/{id}/roster` |
| View available free agents | `GET /teams/{id}/free-agents` |
| Start a draft (generates the full snake pick order) | `POST /drafts/start` |
| View the draft board (what's left) | `GET /drafts/{id}/board` |
| Make a draft pick | `POST /drafts/{id}/pick` |
| View pick history | `GET /drafts/{id}/picks` |
| Autopick (AI-owned teams) | `POST /drafts/{id}/autopick` |
| Start a timed auction draft (budgets snapshotted from program_size) | `POST /auctions/start` |
| View auction budgets | `GET /auctions/{id}/budgets` |
| View the auction board (what's left) | `GET /auctions/{id}/board` |
| Nominate an asset, opening a timed bid round | `POST /auctions/{id}/nominate` |
| Place/overwrite a sealed bid | `POST /auctions/{id}/rounds/{round_id}/bids` |
| View bids (sealed while open, revealed once resolved) | `GET /auctions/{id}/rounds/{round_id}/bids` |
| View the currently open round, if any | `GET /auctions/{id}/rounds/current` |
| Resolve a round (bid → reveal → debit → next round) | `POST /auctions/{id}/rounds/{round_id}/resolve` |
| Set a weekly lineup | `PUT /teams/{id}/lineup` |
| View a weekly lineup | `GET /teams/{id}/lineup` |
| View a player's weekly score breakdown (per-play detail) | `GET /players/{id}/weekly-score` |
| View a team's weekly total | `GET /teams/{id}/weekly-score` |
| View a matchup result | `GET /matchups/{id}` |

**Note for design purposes:** `POST /teams` covers "pick a program size, get placed on a team" but there's still no "join league" flow (no auth/user-session concept exists yet — `owner_user_id` is passed in the request body, trusted as-is). If mockups need real "join league" semantics, flag it back.

**A working auction draft UI exists**: `web/static/auction-board.html` (linked from the landing page and the snake draft board's nav). It's a separate page from `draft-board.html`, not a mode toggle on it — the two draft types have different enough concepts (budgets, sealed bids, a countdown, nomination turn) that reusing the snake board's markup would have meant branching most of it. Since there's no auth, it has an "Acting as" team switcher so one browser tab can play every team's side of the auction (nominate as the team on the clock, bid as any team) — the same trust gap `POST /auctions/{id}/nominate` and the bid endpoint already document.

**A working league + team creation wizard exists**: `web/static/create-league.html`, linked from the landing page. It offers NCAAF, NFL, and NHL. NFL and NHL reference rows are created idempotently on demand, so a fresh deployment can create those leagues without an operator seed step. NHL uses a six-position starting lineup (C, W, W, D, D, G) plus eight bench slots. The wizard creates a lightweight guest commissioner session when no session exists; this is prototype identity, not production credential authentication. See the draft_type decision above for its post-creation branch to `start-recruiting.html`, `roster.html`, or the pro-sport draft board.

## What's real vs. stubbed

Built and working: the full schema, snake draft generation and picking, lineup setting, standings, and reading back a play-level score breakdown — all backed by real data (`scripts/seed_demo_league.py` seeds a 12-team league with real CFBD rosters and three weeks of real games run through the actual attribution pipeline, `src/fantasy_sports/sports/football/college/pipeline.py`, not synthetic placeholders).

Explicitly stubbed (endpoints exist and respond, but don't enforce yet — noted with TODOs in the code):
- Draft-turn enforcement (any team can currently make the next open pick, not just the team "on the clock")
- Lineup legality validation (position-eligibility against `roster_slot_definitions`, roster ownership)
- AI personality-driven autopick (currently just takes the first available board asset — `personality_profile` isn't consulted)

**Fixed: kickers now have a starter slot.** `roster_slot_definitions` has a `K` slot (`slot_count=1`, `eligible_positions=["PK"]` — CFBD's own position code for kickers, not `"K"`; a slot filtering on `"K"` would silently never match a real player). Verified against the reseeded demo league: all 36 team-weeks (12 teams × 3 weeks) fill exactly 11 starters including a real kicker.

**New consequence worth resolving before lineup-legality validation gets built:** adding an 11th starter without shrinking `BENCH` (still `slot_count=10`) means the slot definitions now want 21 total roster spots (11 starters + 10 bench), while the roster itself is still 20 players (`draft_rounds` is unchanged at 20). Nothing breaks today since legality isn't enforced, but once it is, this needs a decision: shrink `BENCH` to 9, or grow the roster past 20.

Out of scope per the original brief: salary-cap draft logic (field exists, unused), sim-recap animation data, Dual Universe crossover mechanics, baseball's locked-rotation tables.

### NHL series schedule

Routes (NHL leagues only; football weekly scheduling in `platform/schedule.py` is untouched):

- `POST /leagues/{id}/series-schedule` - commissioner only. Body: `start_date` (required), `cycles` (1-4, default 1), `replace` (default false). Returns the full schedule.
- `GET /leagues/{id}/series-schedule` - every series with its daily games, ordered by date.
- `GET /teams/{id}/series-schedule` - that team's series in date order: opponent, series status, and per-day `calendar_date`, `home_away`, `status`, and `is_matchup_day`.

Rules (`platform/series_schedule.py`): the league must contain the `nhl` universe and have an even team count of at least 2 (otherwise 400). Teams sorted by id are paired by the circle method, giving N-1 rounds per cycle; each round is one series of `series_length_days` (5, 7, or 10) consecutive dates and rounds run back to back, so every team has exactly one opponent per series and one game on every date. Extra cycles repeat the pairings with `team_a`/`team_b` swapped on odd cycles. `team_a` is home on days 1, 3, 5... and `team_b` on days 2, 4.... Every daily game is created `SCHEDULED` with `is_matchup_day=true`; real NHL slates are not consulted, so `is_matchup_day` is only the input a later step uses for off-day marking.

Regeneration is replace-only: a second `POST` returns 400 unless `replace=true`, and even then returns 400 once any series is not `SCHEDULED` or any game is not `SCHEDULED` with a 0-0 score. Replacing deletes and recreates all rows, and identical inputs produce identical rows.

### NHL series standings, rivalries and Game Score

Derived on read from `series_daily_games` (no new tables, no migration, no stored totals; `league_series.team_a_wins/team_b_wins` are not consulted). Code: `platform/series_standings.py`, `platform/game_score.py`, `api/routers/series_standings.py`. NHL leagues only (otherwise 400; unknown league 404).

- `GET /leagues/{id}/series-standings[?as_of=YYYY-MM-DD]` - ranked rows with W/L/T, goals for/against/diff and `h2h_wins`. Every day is its own game, so these are daily-game records; series winners do not exist in the regular season (they matter only in the playoffs).
- `GET /leagues/{id}/rivalries[?team_id=&as_of=]` and `GET /leagues/{id}/rivalries/{team_a}/{team_b}` - head-to-head series and daily records, goal differential and `intensity` (0-1). Pairs are keyed `(league_id, lower team id, higher team id)`; team ids are league-scoped, so seasons never collide. A cross-season franchise key does not exist yet, so history is per league.
- `GET /leagues/{id}/game-scores[?as_of=]` and `GET /series-daily-games/{id}/game-score` - integer 0-100 score with `margin`, `cutoff`, `rivalry`, `urgency` components and the `standings_as_of` date used.
- `POST /leagues/{id}/game-scores/publish` - commissioner only; writes `game_score_rating` for games that are not FINAL.

Counting rules: only FINAL games count; equal scores are a tie (neither W nor L). Ranking, best first: W-L record (wins desc, then losses asc), goal differential desc, head-to-head wins desc (daily wins over the other teams tied on W-L and goal differential; exposed as `h2h_wins`), goals for desc, goals against asc, team id asc; teams equal on all but team id share a rank. Series results never enter the standings; the rivalry endpoints still report them (a series is decided when all its games are FINAL, more daily wins takes it, equal wins is a tie) as history only.

Game Score (a hand-built deterministic formula, not a learned model). A game on date D is rated from standings of games on or before D - 7 days, so it is published a week ahead and is stable once those games are final. Components, each in [0, 1]: `margin = 1/(1+|wins A - B|/L)`; `cutoff` = mean over both teams of `1/(1+|wins - line|/L)` with wins = daily wins and L = series length in days (so a one-series gap halves the term), where the line is the midpoint of the last playoff team and the first team out (top `max(1, size//2)` of the team's conference, or of the league if no conferences), 0 if everyone qualifies; `rivalry = n/(n+10) * (1 - |daily wins A - B|/n)` over n prior head-to-head daily games, 0 with none; `urgency = 1 - series rounds remaining after this one / total rounds`. `score = round(100 * (0.15 rivalry + 0.25 urgency + urgency * (0.30 margin + 0.30 cutoff)))`, clamped to 0-100. Weights sum to 1 so the maximum is exactly 100; the score is monotonic non-decreasing in every component, and standings terms are scaled by urgency so opening-week games rate low. Divisions are modeled by `teams.conference_id` when set. Verification: `scripts/verify_hockey_series_standings.py`.

### NHL pecking order

Table `team_pecking_order` (migration `e7a3c5d1f9b8`): one row per ranked slot - `team_id`, `position_group` (`C`/`W`/`D`/`G`), `priority` (1-based), `draftable_asset_id`, `updated_at`. Unique on `(team_id, position_group, priority)` and on `(team_id, draftable_asset_id)`; a check limits priority to 1-4 for C/G and 1-8 for W/D. A team may rank fewer players than the maximum and may leave roster players unranked.

Routes (NHL teams only, otherwise 400; all ids are **Player ids**):

- `GET /teams/{id}/pecking-order` - per group: `max_depth`, `starters`, `ranked` (priority order) and `unranked` roster players. LW/RW count as W.
- `PUT /teams/{id}/pecking-order` - body `{"assignments": {"W": [playerId, ...], ...}}`, highest priority first. Replaces each named group (others untouched), all-or-nothing. 400 for an unknown group, a list over the depth, a duplicate id (also across groups), or a player who does not exist, is not on this team's roster, or is not eligible for the group. Authority matches `PUT /teams/{id}/lineup` (owner; or the commissioner with commissioner mode for an AI team).
- `POST /teams/{id}/pecking-order/resolve` - read-only. Body `{"scheduled_player_ids": [...], "confirmed_goalie_ids": [...]}`. Returns `starters` per group (with priority) and `vacancies` (`position_group`, `slot`, `reason`).

Resolver rules (`platform/pecking_order.py`, pure): a day dresses 1 C, 2 W, 2 D, 1 G. Each group takes its highest-priority players that are in `scheduled_player_ids`; W and D are pooled across all eight ranks. The goalie must also be in `confirmed_goalie_ids`. Vacancy reasons: `NO_RANKED_PLAYERS` (nobody ranked), `INSUFFICIENT_RANKED` (everyone ranked starts, still short), `NO_ACTIVE_PLAYER` (ranked players exist but are not scheduled), `NO_CONFIRMED_GOALIE` (an active ranked goalie exists but none is confirmed). Activity is caller-supplied; nothing is fetched from an NHL service and no scoring is involved. Players no longer on the roster are ignored.

### Progressive simulator (FS-025)

Sport-agnostic reveal engine for simulated games. Code: `platform/simulator/` (`contract.py` versioned contract and validator, `state.py` pure clock/reveal arithmetic, `service.py` persistence), `api/routers/simulator.py`. Tables (migration `a1c5e9b3d7f2`): `simulator_games` (one per `series_daily_games` row: shape, status, `started_at`, `script_digest`, `provenance`), `simulator_events` (immutable ordered events), `simulator_reveals` (per user and game: `mode` `WATCHING`/`SKIPPED`, forward-only `cursor_seq`). Nothing here values a goal; hockey scoring arrives with FS-026 through the `SportSimulatorAdapter` protocol.

**Contract v1.0** (`CONTRACT_VERSION`; a script is accepted when its major version matches). A `SimScript` has `contract_version`, `sport`, `periods` (1-9), `period_seconds` (1-1200), `window_seconds` (real seconds a live viewer takes to see the game), `events`, and free-form `provenance`. Each event: `seq`, `kind` (`period_start`, `period_end`, `shot`, `save`, `score`, `penalty`, `narrative`, `game_end`), `period`, `game_second` (elapsed across the whole game), `team_side` (`home`/`away`/null), the running totals *after* the event (`home_score`, `away_score`, `home_shots`, `away_shots`), `headline`, `detail`. Validation: seq contiguous from 1; `game_second` and `period` never go backwards and stay inside the period; totals never decrease; only `score` events change a score (upward, own side only); only `shot`/`save`/`score` change shots; exactly one `game_end`, last, at `periods*period_seconds`, totals unchanged. Scripts are published server-side with `service.publish_script` (idempotent by digest; a different script for the same game is refused; events are immutable through the ORM). There is no HTTP publish route.

**States.** Game: `SCHEDULED` -> `LIVE` (commissioner `start`, or `service.start_game`) -> `FINAL` once `window_seconds` have passed; a live game reveals the events whose `game_second` is <= `elapsed*total/window`. `service.sync` applies the transition lazily and idempotently before every read (no scheduler), mirroring the public state onto `series_daily_games` (status `LIVE_SIMULATING`/`FINAL`, forward-only scores, period, clock) and finalising the series when every day is final. Viewer: no reveal row = spoiler veil (lifecycle only, no scores or events); `watch` follows a live game or replays a finished one (the cursor is the resume point, saved with `progress`, never rewinds; a live viewer's cursor follows what they have been shown); `skip` jumps to the box score of a FINAL game and is terminal. One viewer's choice never changes another's. The finished game's score is still league-visible through standings (see platform/series_standings.py); the veil protects the live/replay experience.

Routes (all need a signed-in user): `GET /simulator/contract`; `GET /simulator/games/{daily_game_id}/state`; `GET .../events?after_seq=&limit=` (idempotent polling; `next_seq`, `available_through`, `has_more`); `POST .../watch`; `POST .../progress {cursor_seq}`; `POST .../skip`; `GET .../final` (needs `skip` or a fully watched replay); `POST .../start` (league commissioner). Errors carry `{code, message}`: `NOT_PUBLISHED`, `NOT_STARTED`, `NOT_FINAL`, `REVEAL_REQUIRED`, `AHEAD_OF_GAME`, `BAD_CURSOR`. Verification: `scripts/verify_simulator_core.py` (synthetic events only, injected clock).

### NHL simulator scoring (FS-026)

`sports/hockey/simulator/` implements the `SportSimulatorAdapter` for hockey: stored `shot_credits`/`event_credits` for each lineup's starters (resolved from the pecking order for the daily game's date) -> signed box score -> inversion of goalie negatives into opponent-positive events -> seeded, bounded normalization to an NHL-shaped scoreboard -> FS-025 events. `loader.publish_game(db, daily_game)` builds and publishes a game; there is no HTTP route. No tables or migrations were added. Formulas, invariants, constants and open decisions are in `docs/hockey_simulator_scoring.md`; verification is `scripts/audit_hockey_simulator_scoring.py`.
