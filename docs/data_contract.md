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
| `series_daily_games` | One independently scored game within a league series: a fantasy window (Sun-Tue, Wed-Thu, Fri-Sat) for NHL leagues, or a calendar day on legacy schedules | calendar_date, calendar_date_end, window_index, is_matchup_day, game_score_rating, home/away team and score, current_period, seconds_remaining, status, timestamps | belongs to one league_series; one row per series/date |

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

## Imported scoring (legacy formula)

The one exception to "the platform's only points formula is the realistic model" (FS-037, owner-approved). A league brought
over from another platform keeps its existing box-score formula.

- `leagues.scoring_system`: `'realistic'` (default, every league made here) or `'imported'`. Set by code at creation only;
  create-league refuses `'imported'`, and no route changes it. (The research doc's per-season `external:espn` label is a
  separate, later archive field; the league flag is just `'imported'`, with the source in `imported_scoring_items.source_platform`.)
- `imported_scoring_items` (league_id, source_platform, source_stat_id, stat_code, points, created_at; unique on
  league_id + stat_code): the frozen formula. Written only by `imported_scoring.create_formula`, which refuses a league that
  already has one. There is no update or delete path; a correction is an operator data fix with an audit trail.
- The formula is a flat list of stat code -> points per unit. Double-double (`DD`) and triple-double (`TD`) are derived from
  PTS/REB/AST/STL/BLK; a triple-double also counts as a double-double (`TRIPLE_DOUBLE_ALSO_COUNTS_DOUBLE`, unconfirmed against
  ESPN's own documentation, so flagged).
- Stat-line input shape: `{"PTS": 30, "3PM": 4, "REB": 10, ...}` per player-game. NBA box scores are not ingested yet, so
  `imported_scoring.week_stat_lines` returns none and `scoring.team_week_score` gives 0 for an imported league.
- Scale-dependent features (scoring-scale context, the AI poll, the realism audit) skip imported leagues.
- API: `LeagueOut.scoring_system` and `LeagueOut.imported_scoring` (read-only).

Per-league NBA layout: `roster_slot_definitions.league_id` (NULL = the universe default). A league with its own rows uses
them instead of the universe's (`lineups.league_slots`); `nba_reference.apply_league_layout` sets G/F/UTIL/BENCH/IR counts.
`IR` is never auto-filled and is not a roster spot (`league_features.roster_spots`).

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

**A working league + team creation wizard exists**: `web/static/create-league.html`, linked from the landing page. It offers NCAAF, NFL, and NHL. NFL and NHL reference rows are created idempotently on demand, so a fresh deployment can create those leagues without an operator seed step. NHL uses a six-position starting lineup (C, W, W, D, D, G) plus eighteen bench slots (a 24-player roster: four lines of C, W, W, D, D, G depth). The wizard creates a lightweight guest commissioner session when no session exists; this is prototype identity, not production credential authentication. See the draft_type decision above for its post-creation branch to `start-recruiting.html`, `roster.html`, or the pro-sport draft board.

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

Rules (`platform/series_schedule.py`): the league must contain the `nhl` universe and have an even team count of at least 2 (otherwise 400). Teams sorted by id are paired by the circle method, giving N-1 rounds per cycle; each round is one series of `series_length_days` (5, 7, or 10) consecutive dates and rounds run back to back, so every team has exactly one opponent per series. Within a series the days are grouped into fantasy windows (Sun-Tue, Wed-Thu, Fri-Sat; a series that starts or ends mid-window gets a shorter one) and each window is ONE game and one `series_daily_games` row (`window_index`, `calendar_date` to `calendar_date_end`); `window_rows=false` keeps the legacy one-row-per-day shape. `team_a` hosts the first, third... window. Extra cycles repeat the pairings with `team_a`/`team_b` swapped on odd cycles. `team_a` is home on days 1, 3, 5... and `team_b` on days 2, 4.... Every daily game is created `SCHEDULED` with `is_matchup_day=true`; real NHL slates are not consulted, so `is_matchup_day` is only the input a later step uses for off-day marking.

Regeneration is replace-only: a second `POST` returns 400 unless `replace=true`, and even then returns 400 once any series is not `SCHEDULED` or any game is not `SCHEDULED` with a 0-0 score. Replacing deletes and recreates all rows, and identical inputs produce identical rows.

### NHL series standings, rivalries and Game Score

Derived on read from `series_daily_games` (no new tables, no migration, no stored totals; `league_series.team_a_wins/team_b_wins` are not consulted). Code: `platform/series_standings.py`, `platform/game_score.py`, `api/routers/series_standings.py`. NHL leagues only (otherwise 400; unknown league 404).

- `GET /leagues/{id}/series-standings[?as_of=YYYY-MM-DD]` - ranked rows with W/L/T, goals for/against/diff and `h2h_wins`. Every day is its own game, so these are daily-game records; series winners do not exist in the regular season (they matter only in the playoffs).
- `GET /leagues/{id}/rivalries[?team_id=&as_of=]` and `GET /leagues/{id}/rivalries/{team_a}/{team_b}` - head-to-head series and daily records, goal differential and `intensity` (0-1). Pairs are keyed `(league_id, lower team id, higher team id)`; team ids are league-scoped, so seasons never collide. A cross-season franchise key does not exist yet, so history is per league.
- `GET /leagues/{id}/game-scores[?as_of=]` and `GET /series-daily-games/{id}/game-score` - integer 0-100 score with `margin`, `cutoff`, `rivalry`, `urgency` components and the `standings_as_of` date used.
- `POST /leagues/{id}/game-scores/publish` - commissioner only; writes `game_score_rating` for games that are not FINAL.

Counting rules: only FINAL games count; equal scores are a tie (neither W nor L). Ranking, best first: W-L record (wins desc, then losses asc), goal differential desc, head-to-head wins desc (daily wins over the other teams tied on W-L and goal differential; exposed as `h2h_wins`), goals for desc, goals against asc, team id asc; teams equal on all but team id share a rank. Series results never enter the standings; the rivalry endpoints still report them (a series is decided when all its games are FINAL, more daily wins takes it, equal wins is a tie) as history only.

**Tiebreakers and playoff rules (leagues columns, migration `b5e8d2a6c9f1`).** `tiebreakers` is the commissioner's ordered list of tie-breaks applied after points or win % (NULL = the default order: goal differential, head-to-head, goals scored, goals allowed). Keys: `head_to_head`, `division_record`, `wins`, `goal_differential`, `goals_for`, `goals_against`, `coin_flip` (a draw fixed by league and season; must be last). `playoff_byes` (top seeds that skip round one), `division_winners_qualify` (every division winner is in the field, the other spots are wild cards by rank) and `division_winners_seeded_first` (they take the top seeds). Set in the league wizard and League Settings; code: `platform/tiebreakers.py`, `platform/playoffs.py`, `StandingsRules` in `platform/series_standings.py`.

**Consolation matchups (`leagues.playoff_consolation`, migration `c7d1f3a9e5b2`).** False (the default) = a team's season ends when it is eliminated; True = eliminated teams play consolation matchups in the playoffs. Set in the wizard and League Settings (hockey leagues). It is a setting only until the playoffs are played, and consolation games are never part of the standings, so they cannot change elimination status.

**Mathematical elimination.** `platform/elimination_engine.py` proves, for every team, whether any way the remaining games can finish gets it into the playoff field (or a bye), searching the finishes exhaustively with the league's real tiebreakers; a team is eliminated only when none does. Margin tiebreakers stay open while either team has a game left, and the random draw until the league's last game, so the proof is conservative: it can lag a true elimination in those cases but never marks a team out by mistake. `platform/elimination.py` writes `teams.eliminated_season` right after each game finalises (`simulator/service._finalize`), which turns on the CFG-06 trade restrictions. Elimination is permanent unless the commissioner changes the standings or playoff rules, or voids a finished game, which re-proves everything. Sport support is an adapter in `elimination.ADAPTERS` (hockey only today: an adapter returns the played games, the unplayed games and the two rule sets). Verification: `scripts/verify_elimination_engine.py` (brute force) and `scripts/seed_elimination_smoke_test.py`.

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

### NHL game page (FS-027)

`web/static/nhl-game.html?daily_game_id=...` (logic in `web/static/nhl-game-logic.js`; linked from each game day in `league.html`) renders the FS-025/FS-026 contract: scoreboard (away left, home right), period clock and progress, shots, play-by-play ticker, spoiler veil, independent watch / skip controls, a skippable ~75 second replay driven by the stored events, reconnect with backoff, the attribution box score, and the data-provenance panel. It only displays values the API returns (event running totals, `GET .../final`); it never computes a score. Read-only API addition: `GET /simulator/games/{id}/state` now also returns `league_id`, `series_id`, `calendar_date`, `home_team` and `away_team` (not spoilers). `session.js` now surfaces `{code, message}` error details as `err.code`/`err.message`.

Screens: unpublished, scheduled, live-veiled, final-veiled, live (polls every 5 s; 1 s doubling to 30 s when disconnected, with a banner), replay (play/pause, next event, reveal everything, skip, "step manually" mode that never auto-plays, resumable cursor saved at goals and period ends), final (box score). Verification: `scripts/verify_hockey_simulator_ui.py` (real API payloads for every state through Node, plus page structure). Manual checks recorded 2026-10-10 in the in-app browser against a throwaway database: desktop veil -> live watch -> scoreboard and ticker updating; final hidden -> replay -> reload resumes -> skip to box score; a second viewer still veiled after the first skipped; unpublished, scheduled and unknown-game states; 375 px mobile width without horizontal scroll; manual (reduced-motion) mode did not advance for 2.5 s and advanced one event per press. Not manually checked: the OS-level `prefers-reduced-motion` setting itself (the page reads it at load; the manual toggle covers the same path), screen-reader output, and a real network drop (backoff is checked in logic only).

### NHL off-day banking (FS-028)

Table `off_day_banked_credits` (migration `b4d8f2a6c0e1`), model `BankedCredit`; rules in `platform/banking.py`; routes in `api/routers/banking.py`. Credits are scored upstream (FS-026) and only stored here. **There is no HTTP ingestion route** (a commissioner must not be able to mint points): the scoring side calls `banking.ingest_credit(...)`, which is idempotent on `(league_id, source_key)` (default key `asset:<id>:<date>`; first write wins, so re-runs never change an assigned credit). It requires an NHL league, a rostered asset, a positive finite credit, and a date that is an off day for the team (a series day with `is_matchup_day = false`; nothing yet *marks* off days, so that step is still to build).

Constants: cap `BANK_CAP = 1.50` per credit applied to a game (`applied_points`; `credit_points` keeps the earned value, `capped` is `credit_points > 1.5`); fallback `24` hours after banking; window `7` days after the source date.

- Status: `AVAILABLE` -> `ASSIGNED` (with `assignment_mode` `MANUAL`/`AUTO`, `assigned_daily_game_id`, `applied_points`) or `EXPIRED` (`status_reason` `NO_ELIGIBLE_GAME`). Terminal states never move; reassignment is rejected (409). Database checks enforce: at most one assigned credit per `(game, team)`, `applied_points <= 1.5`, and that ASSIGNED rows have a game and amount while the others have neither.
- Eligible games: the team's own `SCHEDULED` matchup-day games after the source date, within 7 days of it, not before today, not already holding a credit. `assign` rejects with a code: `NOT_YOUR_GAME`, `OFF_DAY`, `BEFORE_CREDIT`, `OUTSIDE_WINDOW`, `IN_THE_PAST`, `GAME_STARTED`, `GAME_HAS_CREDIT`.
- Recommendation (`GET /banked-credits/{id}/recommendation`): ranks eligible games on win-probability swing if every option has one, else on opponent strength (opponent's daily win rate then goal difference per game, standings as of the source date, needs 3 final games), else the neutral earliest game. `confidence` is `FULL`, `PARTIAL`, `NEUTRAL` or `NONE`; `inputs_available` states which of projected margin / opponent strength / win-probability swing exist and the `reasons` say what was not used. No projection source exists, so margin and swing are always `null` over HTTP (never estimated); `banking.recommend(..., projections=...)` accepts them from a future caller. Deterministic: ties break on date then game id.
- Fallback (`POST /leagues/{id}/banked-credits/process-fallback`, commissioner only, safe to repeat; `banking.process_fallback(db, now)` with an injected clock): every `AVAILABLE` credit at least 24 hours old goes to the immediately next eligible game (not the recommendation, per spec 5.2) or expires. Nothing schedules it; something must call it.
- Routes: `GET /teams/{id}/banked-credits[?status=]` (owner, or the commissioner with commissioner mode for an AI team), `POST /banked-credits/{id}/assign {daily_game_id}`. Errors are `{code, message}`.

Not done: applying `banking.assigned_for_game(...)` credits to the simulated scoreboard (needs a `banked` role in the protected FS-026 adapter), the job that ingests and calls the fallback, off-day marking. Verification: `scripts/verify_hockey_banking.py`.

### NHL banking UI (FS-029)

`web/static/league.html?tab=roster` (3-window roster & lines) consolidates the weekly schedule and pecking order lines. The previous series viewer `nhl-schedule.html` has been replaced with the 3-window UI.

- **Alert strip** above the zones: what is waiting, the points, the time left before the 24-hour automatic banking; turns urgent under six hours; **Review** opens the first credit.
- **Banked credits list** (matchup zone): per credit the player, off-day date, `Earned x` (and `· y applies` when capped), a status chip - Available, Assigned, Auto-assigned, Expired - plus a separate Capped chip; where it went (`Banked to Thu 10/16 vs BOS`, or that it banked automatically after 24 hours, or expired with no open game).
- **Recommendation card** under the chosen credit: the recommended game, a confidence chip with an honest note (`No projection data ... not a prediction`, `Partial basis ... not a win-probability estimate`, `Based on projections`), a "Why this game" disclosure listing the API reasons and which inputs were used (missing inputs read "Not available - not estimated"). **Accept recommendation** or **Choose another game** (radio list of ranked options with opponent record and sample size) -> **Confirm** step stating the points and that it cannot be changed. One request at a time (`ui.submitting`); the API also rejects a repeat with 409.
- **Errors** map API codes to plain text and reload the options when the credit or game changed under the user; network failures say nothing was changed.
- **Series tabs** badge each game that holds a credit (`+0.90 banked`, `+0.60 banked (auto)`).

Verification: `scripts/verify_hockey_banking_ui.py` (all five credit states and four recommendation shapes from the real API through Node, plus page structure and double-submit guard). Manual checks recorded 2026-10-10 in the in-app browser against a throwaway database: alert -> review -> neutral recommendation -> choose another -> confirm -> a triple click produced exactly one request and the tab badge/banner updated; all five states rendered; 375 px width without horizontal scroll; keyboard focus moved to the card heading on open and to Confirm after a choice. Not checked: screen-reader output, and the FULL-confidence card in a browser (covered in logic only, since no projection source exists).

### Slow / asynchronous snake draft clock (FS-030)

Migration `c5e9a3b7d1f4` adds clock columns to `drafts` (all nullable or defaulted, so existing drafts stay untimed): `pick_seconds`, `overnight_start_minute`, `overnight_end_minute`, `utc_offset_minutes`, `paused`, `paused_at`, `remaining_seconds`, `pick_started_at`, `deadline_at`. Rules in `platform/draft_clock.py` (pure, injected `now`), routes in `api/routers/draft.py`, UI in `web/static/draft-board.html` + `draft-clock-logic.js`.

- **Config** (`POST /drafts/start` body, commissioner only when any clock field is present; an untimed start is unchanged and still needs no sign-in): `pick_seconds` (10 s to 7 days), optional `overnight_start`/`overnight_end` ("HH:MM", may wrap midnight) and `utc_offset_minutes`. Mode is derived: untimed, live (< 1 h per pick), async (>= 1 h). The offset is fixed, with no time-zone database: it does not follow daylight saving, so change it when clocks change.
- **Clock rules**: a pick's allowance is spent in *active time* only. Overnight windows and commissioner pauses freeze the remaining time (a deadline that lands exactly on the window start stays at that instant). `deadline_at` (naive UTC) is the authority; it is null while paused or untimed.
- **Overdue**: at `now >= deadline_at` the server picks the first asset on the board (best last-season points, then name; deterministic), stamped with the deadline, and the next pick's clock starts at that missed deadline, not at `now`. So processing every few seconds or once after a long outage yields identical picks and timestamps; a restart changes nothing (state is in the database). Repeating the processing is a no-op, and the draft row is locked (`SELECT ... FOR UPDATE` on PostgreSQL) while it runs. Nothing schedules it: it runs lazily on `GET /clock`, `POST /pick`, `POST /autopick`, `POST /pause`, and `POST /process-overdue`, which the page calls when its countdown reaches zero.
- **Routes**: `GET /drafts/{id}/clock` (status `untimed|running|paused|overnight|complete`, `deadline_at`, `remaining_seconds` in active seconds, `overnight`, `resumes_at`, on-the-clock team), `POST /drafts/{id}/pause` and `/resume` (commissioner; idempotent; pausing an already-expired pick first auto-picks it), `POST /drafts/{id}/process-overdue` (no sign-in: it only applies what the clock already decided).
- **Existing behaviour**: untimed drafts behave exactly as before (autopick open to anyone). On a timed draft a manual pick that arrives late is refused with 409 ("ran out") after the autopick is made, picks and autopicks are refused with 409 while paused, and `/autopick` works at any time only for AI-owned teams (human teams only once overdue).

Not built (out of scope): a background scheduler, push notifications, AI commentary, draft-order randomization. Verification: `scripts/verify_async_snake_draft.py` (clock arithmetic incl. 3000 seeded round trips; live, async, pause, overnight, restart, double processing, authorization, untimed regression; page logic under Node).

### Draft broadcast room (FS-031)

Every completed snake-draft pick gets a versioned announcement with deterministic, local commentary. No external model, audio file or network call is involved. Code: `platform/ai/commentary.py` (provider-neutral `CommentaryProvider` protocol + `DeterministicCommentary`, swappable with `set_provider`), `api/routers/draft_broadcast.py`, UI in `web/static/draft-board.html` + `draft-broadcast-logic.js`. No tables or migrations: announcements are derived on read from the stored picks, so there is nothing to duplicate or lose on a restart.

- **Event** (`GET /drafts/{id}/announcements?after_pick=N&limit=`; version `1.0`): `event_id` (`draft:<id>:pick:<n>`, stable), `pick_number`, `round`, `team`, `player` (asset id, name, position), `is_autopick`, `picked_at`, `commentary` (`provider`, `text`, `tags`, `value_label` = value/fair/reach/unknown, `unavailable` = inputs that were missing, `inputs`) and `spoken_text`. Reading is idempotent; `next_pick` is the cursor; a client that reconnects asks after its last pick and gets exactly the rest.
- **Inputs used, and only these**: round and pick number, team, player and position, how many of that position the team and the league had already taken, and - when stored - the player's rank by last-season points in the draft pool. A pick is a "value" when it came at least 10 slots later than that rank, a "reach" at least 10 earlier (`VALUE_MARGIN`, a product default). With no stored points (every NHL draft today) the label is `unknown`, `unavailable` says "last-season points rank", and the text says there is nothing to measure against. No statistic is estimated; the audit checks every number in the text comes from the inputs. Wording varies by pick number only, so a pick always reads the same.
- **Page**: a Broadcast panel with the latest pick as a banner (value/reach/autopick labels, missing-input note), a polite screen-reader live region, **Sound** (off by default; optional browser speech via `speechSynthesis`, feature-detected, preference kept in `localStorage` inside try/catch) and **Replay last pick**. The first load and any reconnect backfill are shown but never spoken; when several picks land together only the newest is spoken. Polling is every 5 s with doubling backoff to 60 s on failure; duplicate event ids are dropped. A new announcement from another viewer refreshes the board and pick history.

Verification: `scripts/verify_draft_broadcast.py`. Manual check 2026-10-10 in the in-app browser (throwaway database, timed draft): a pick made by another user appeared as a banner, in the live region and in the history within one poll; sound stayed off; the countdown and Pause button from FS-030 rendered beside it. Not manually checked: actual speech output, muted/audio toggling with a screen reader, keyboard-only use beyond native button behaviour.

### Draft media provider seams (FS-032)

Disabled-by-default interfaces for AI commentary, shared music and video/audio chat; full rules, security/privacy requirements and the owner decision checklist are in `docs/draft_media_providers.md`. Code: `platform/media_providers.py` (protocols, status, fail-closed `get_provider`, `commentary_provider` fallback), `platform/media_fakes.py` (tests only), `api/routers/media.py` (`GET /media/capabilities`, `POST /media/{shared_music|av_chat}/session` -> 503 unless fully configured), "Draft room extras" panel in `draft-board.html`. The provider registry is empty, so nothing can be `available`; no vendor package, credential or outbound call exists. Verification: `scripts/verify_draft_media_seams.py` (blocks non-loopback sockets for the whole run).

### Weekly salary-cap redraft (FS-033)

Separate league mode with contests, pairings, per-contest salaries, draft/submitted rosters and a fixed cap; migration `d6f0b4c8e2a5` (adds `leagues.league_mode` and six tables). The cap-adjustment formula is an open product decision and is not implemented; salaries are a commissioner-supplied input. Full rules, routes and the decision record are in `docs/weekly_redraft.md`. Verification: `scripts/verify_weekly_redraft.py`.

### NHL ice time

Table `player_game_toi` (migration `b9e4a1c7d3f6`): one row per player per final real game - `game_id`, `draftable_asset_id`, `toi_seconds`. Unique on `(game_id, draftable_asset_id)`. Filled from the NHL boxscore (`/gamecenter/{id}/boxscore`, `playerByGameStats`) by `nhl_daily_sync.ensure_game_toi` while the slate is ingested, once the game is final. Every dressed player has a row (a backup goalie who never played has 0); a healthy scratch has none. A game with no rows has no stored boxscore and nobody counts as having sat out of it. `window_engine.sat_out` reads it: a starter with no ice time in his counting game is replaced by the next player on his pecking order (docs/hockey_spec.md section 6.5). The parser was written from the boxscore's documented shape and has only been exercised offline; the first live ingest should be checked.

### NHL series release gate (FS-034)

`scripts/verify_hockey_series_e2e.py` (12 teams, 24 rounds, 1 human + 11 AI, built from `alembic upgrade head`) and `scripts/run_hockey_release_gate.py` (full command matrix). Go/no-go, defects fixed, deferred items and deployment impact are in `docs/hockey_series_release_checklist.md`.

### Platform plans, deposits and trade review (FS-036)

Extends the CFG-06 trade eligibility rules (`platform/trade_eligibility.py`, still the one place trade rules live); migration `e4b8a1d6c3f9` (on top of `c7d1f3a9e5b2`). Nothing charges real money: payments are recorded by hand and the platform never holds league dues or prize money.

**Platform plans.** `leagues.platform_plan` is `AD_SUPPORTED` (default; we earn ad revenue) or `AD_FREE` (each person-managed team pays a flat per-season platform fee and sees no ads). The fee is a flat amount, never a percentage of dues. `platform_fees.PLATFORM_FEE_CENTS` (1200) is a placeholder until real ad data exists; it is snapshotted into `leagues.platform_fee_cents` when the plan becomes `AD_FREE`, so a later change to the constant doesn't alter a running league. Only the commissioner changes the plan (`PATCH /leagues/{id}/settings`), and not while a draft is in progress. Switching back to `AD_SUPPORTED` keeps every record: the credit belongs to the franchise (team row), so a replacement manager inherits it. AI-owned teams owe no fee (same as dues). `platform_fees.ads_enabled` is False only for an `AD_FREE` team whose current-season fee is secured; it is `ads_enabled` on each team in `GET /leagues/{id}/teams`.

**Records.** `platform_season_fees` mirrors `season_dues` (status `pending|paid|failed|refunded|disputed`, only `paid` counts, `payment_reference` unique per league, rows never deleted; `recorded_by` is the operator's name). There is no API route for it (no admin role): the operator runs `scripts/record_platform_fee.py`.

**The deposit.** In an `AD_FREE` league a person-managed team that sends a future pick for season S needs its season-S fee secured first. The deposit IS the season-S platform fee, collected early and credited to that season, so it is never an extra charge. It is charged once per team per season: once secured, later pick trades for S ask for nothing, and picks from another season need that season secured. Only the sending team pays. In a `PAID` + `AD_FREE` league both gates apply (dues for S and the fee for S); `PAID` + `AD_SUPPORTED` is unchanged (dues only). Unsecured seasons come back as `AWAITING_PAYMENT` with `payment_requirements[]`, each entry tagged `"kind": "league_dues"` or `"platform_fee"`. An `AD_FREE` league with no fee snapshot is `BLOCKED` (`PLATFORM_FEE_NOT_CONFIGURED`).

**FREE leagues can't trade future picks** (`FREE_LEAGUE_PICK_TRADING_LOCKED`), whatever the format or plan. FREE-league player trades are unchanged, so an eliminated team in a FREE league (which can only trade by receiving net future value) has no trade route left.

**Refund lock.** `season_dues.committed_by_trade_id` and `platform_season_fees.committed_by_trade_id` are set, inside the executing transaction, on every paid record that backed an executed trade (the sender's pick seasons, and an eliminated team's next-season dues). The first trade to use a record keeps it. A committed record can't move to `refunded` (the error names the trade), but can move to `disputed` or `failed` (chargebacks happen): the executed trade is never reversed, a warning is logged, and the next pick trade for that season is blocked by the normal gate until it is paid again.

**Trade review.** Settings `trade_review_hours` (0-72, default 24; 0 = no window) and `trade_review_mode` (`none|commissioner|league|both`, default `both`). When a counterparty accepts a person-to-person trade that is eligible, and the league has a window, the trade goes `in_review` with `review_ends_at` (trades already in `voting` are unchanged). In the window the commissioner can veto (`POST /trades/{id}/commissioner-veto`; recorded as a `trade_approvals` row of type `COMMISSIONER_VETO`; refused if the commissioner manages a team in the trade) and the league can veto (`POST /trades/{id}/veto`, rows in `trade_vetoes`). The league veto math: eligible teams are the person-managed teams not in the trade (AI teams never veto); the trade is vetoed once vetoes reach `n // 2 + 1` of them; with no eligible team the league veto is unavailable. Either manager can cancel during review. Nobody, the commissioner included, can approve early or shorten the window: `trades.execute_trade` refuses an `in_review` trade before `review_ends_at`.

**Executing.** `trade_review.process_review_queue(db, now, league_id=None)` is a plain function with an injected clock (the `banking.py` pattern); nothing schedules itself. It runs for a league whenever `GET /leagues/{id}/trades` or an in-review `GET /trades/{id}` is read, and on `POST /leagues/{id}/trades/process-review` (any league member). `execute_trade` rechecks eligibility, so a deposit disputed or refunded, or a deadline passed, during review makes the trade `blocked`. A player locked by a game in progress defers the trade (it stays `in_review` and is retried on the next run). Trade payloads carry `review_ends_at`, `league_veto_count`, `league_veto_needed` and `commissioner_vetoed`.

Verification: `scripts/seed_platform_fee_smoke_test.py`, `scripts/seed_trade_review_smoke_test.py`, `scripts/seed_trade_eligibility_smoke_test.py`.


**Replacing a manager (DUE-06).** `POST /leagues/{id}/teams/{team_id}/replace-manager` (commissioner only, body `{"user_id"}`; `platform/manager_replacement.py`) hands a person-managed franchise to a different existing user who doesn't already manage a team in the league. Dues and platform-fee records are keyed to the franchise, so the replacement inherits the prepaid credit by construction: nothing is moved, nothing is charged twice, and the response lists the paid credit. Offers still waiting for an answer (`proposed`) that involve the team are cancelled; trades already `in_review` or `voting` run on and are rechecked at execution. No audit table yet: the change is logged. Verification: `scripts/seed_manager_replacement_smoke_test.py`.
