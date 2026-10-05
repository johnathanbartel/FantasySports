# NFL v1 scoring, roster, and architecture specification

## Status and scope

This document is the NFL v1 contract. It records decisions for league setup
and the data shape that already supports professional football. It does not
claim that NFL ingestion or a scored NFL week is production-ready.

## Codebase decision

NFL stays in this repository and uses the existing multi-sport platform. It is
another football `Universe` (`sport="football"`, `level="pro"`, `code="nfl"`),
not a separate service or repository.

This follows the established split:

- shared league, team, draft, roster, schedule, and scoring aggregation logic
  lives under `src/fantasy_sports/platform`;
- football-shaped play and credit models live under
  `src/fantasy_sports/sports/football`;
- level-specific ingestion and attribution adapters live under `college` and
  `professional` within the football package.

That structure avoids duplicating league and draft behavior while keeping
sport-specific event data out of the platform models. A separate NFL repo
would create duplicate schemas and API behavior without providing isolation
that the existing `Universe` boundary does not already provide.

## Scoring contract

NFL scoring uses the football module's non-duplicative expected-points model.
The raw unit is expected points added (EPA), not conventional stacked bonuses
for yards, receptions, and touchdowns.

### Offensive and kicking attribution

- A rush assigns the play's full EPA to the rusher.
- A completed pass splits EPA between passer and receiver using nflfastR's
  `yac_epa`: the receiver receives the after-catch component and the passer
  receives the remainder. The shares must sum back to the play EPA.
- A sack or interception assigns the offensive play's negative EPA to the
  passer in v1.
- An incomplete pass uses the shared drop-rate split when a target is known;
  otherwise the passer receives the play's credit.
- A field-goal attempt assigns the kicker only the marginal expected-points
  change of the kick outcome. It does not stack a flat three-point award on
  top of the drive's value.
- Plays without a usable EPA value are not scored. Unsupported special-teams
  events are excluded rather than assigned speculative credit.

The existing implementation is
`src/fantasy_sports/sports/football/professional/pipeline.py`. Its output roles
map directly to `play_credits.role` (`rusher`, `passer`, `receiver`, and
`kicker`).

### Defense

A league chooses one of three ways to draft defense (`league_universes.defense_mode`):

| Mode | Asset | Scores |
|---|---|---|
| `team` | one whole-defense asset per team (`defense_groups.position_group = DEF`) | everything the team's defense does |
| `group` (default) | a team's DL, LB or DB unit | the sum of that unit's players' credit, plus unnamed credit by a play-type rule (pass to DB, run to LB) |
| `individual` | a named defender (an ordinary player asset) | that player's own credit |

All three come from one set of play-level credits
(`professional/defense.py`) and always sum to the play's -EPA, so no play is
awarded in full to more than one asset of the same mode. Each named defender
shares the play in proportion to weights fitted from nflfastR data
(`data/defense_event_weights.json`, from `scripts/fit_defense_weights.py`; never
hand-set). Credits are centered by `data/defense_baseline.json`.

Team and group assets are keyed to the team and unit: roster changes on the real
team do not change what a drafter owns. An individual asset is keyed to the
player's nflverse id: a drafted player who is traded stays on the fantasy
roster and keeps scoring for his new team.

The mode-specific pool is enforced by `platform/defense_modes.py`; a league never
sees assets that belong to another mode. The real-week points-allowed
validation (`professional/scripts/run_week_validation.py`) and the three-mode
tie-outs run on 2024 week 1; `scripts/run_nfl_scored_week.py` runs the audit on a drafted league.

### Weekly totals and realism gate

`play_credits` is the source of truth. `weekly_player_scores` is its
recomputable player/asset-week aggregate. A fantasy team's raw weekly total is
the sum of its starters' weekly asset scores; bench scores do not count.

The NFL has its own fitted scale (`platform/scoring_scale.py` picks it by universe and, for the NFL, by the
league's defense mode; `professional/data/scoring_scale.json`). It is the college method run on NFL data
(`docs/scoring_scale_fit_report.md`): simulated snake drafts on the prior season's credit per game, lineups set from
the same ranking, scored on the season's actual weekly credits, for 4-24 team leagues, one block per defense mode.
It is fitted on the 2019-2024 regular seasons (2018 is the first prior season) and maps a lineup's summed credit to
points so a league's season average equals the NFL's 22.84 points per team-game (sd 9.96), strictly increasing so
the credit leader always wins. Rebuild and refit with `professional/scripts/build_weekly_credits.py` then
`professional/scripts/fit_scoring_scale.py` (network for the first, offline for the second). Held out one season at
a time, a fitted scale's league-average score lands within about 1-1.5 points of that season's real average (the
season-to-season drift of the NFL's own scoring, 21.8-24.8, is most of the gap). A single week is noisier: 2024
week 1 runs about 29 for prior-ranked lineups because every starter is healthy, and the season settles near 22.

Before NFL scoring is called complete, one regular-season week must pass all
of these checks using real data:

1. Every credited play ties back to its source play and non-duplicative role
   shares reconcile to the play value.
2. Player-week aggregates tie to their underlying play credits.
3. Fantasy team totals tie to the configured starters and the weekly matchup
   results/standings.
4. Modeled real-team totals have positive correlation with actual points
   scored, and excluded event classes plus unattributed plays are reported.
5. The fantasy-team mean is no more than 1.5 times the NFL's real team-game
   mean for that audited week. This is a guardrail against inflated scoring,
   not a calibration target.

No external-data validation is performed by the offline smoke test in this
ticket.

## Roster and draft

Offense is the same in every mode: QB 1, RB 2, WR 2, TE 1, FLEX 1 (RB/WR/TE),
K 1. The bench is 70% of the starter count (rounded to the nearest asset), so it
also depends on the defense mode:

| Mode | Defense slots | Starters | Bench | Roster assets |
|---|---|---:|---:|---:|
| group | DL 1, LB 1, DB 1 (team units) | 11 | 8 | 19 |
| team | DEF 1 (whole defense) | 9 | 6 | 15 |
| individual | DL 3, LB 3, DB 4, DFLEX 1 (any of DL/LB/DB) | 19 | 13 | 32 |

Individual defenders fill slots by their unit (DE/DT/NT/EDGE = DL,
ILB/MLB/OLB = LB, CB/S/SS/FS = DB). A league's `draft_rounds` is its roster size:
`POST /leagues` derives it from the mode's slots unless the request sets it
explicitly.

The default league has 12 fantasy teams and a snake draft of roster-size rounds
(19 in the default group mode). Offensive
players, kickers, and defense groups share one draft pool; there are no
reserved defensive rounds. The existing sealed-bid draft engine can use the
same asset pool, but snake is the NFL v1 default.

## Data model

No NFL-only schema migration is needed. The existing model represents NFL as
data and already separates shared entities from football events:

| Concern | Existing model |
|---|---|
| NFL identity and capability flags | `Universe` |
| A league's selected universe and defense mode | `LeagueUniverse` |
| NFL teams and players | `RealTeam`, `Player` |
| Pickable player or group-defense unit | `DraftableAsset` |
| DL/LB/DB team units | `DefenseGroup` |
| NFL roster shape | `RosterSlotDefinition` rows seeded for `nfl` |
| Real games and calendar | `Game`, `Week` |
| nflfastR play data | `Play` with `source="nflfastR"` |
| Per-play attribution | `PlayCredit` |
| Recomputable weekly aggregate | `WeeklyPlayerScore` |
| Realism audit record | `WeeklyRealTeamEpaTotal` |

The original schema migration already creates the football play, credit, and
weekly-score tables. `scripts/seed_nfl_universe.py` supplies the missing NFL
reference row and roster-slot rows; it does not create a parallel schema.

## League creation

After the NFL reference data is seeded, the existing `POST /leagues` endpoint
creates an NFL league with `universe_codes: ["nfl"]`. The default
`defense_mode` is `group`; `team` and `individual` are also accepted.

`scripts/seed_nfl_smoke_test.py` verifies, entirely in memory, that the API can
create NFL leagues in all three defense modes, expose each mode's roster slots
and derived draft rounds, fill a two-team snake draft, and keep each league's
pool to its own mode's defense assets.

## Remaining NFL finish-line work

- ingest NFL teams, players, schedule, and nflfastR play-by-play into the
  shared database;
- implement and validate professional group-defense attribution;
- persist a scored regular-season week and run the realism gate above;
- run the end-to-end scored-week/matchup/standings flow on that real week.
