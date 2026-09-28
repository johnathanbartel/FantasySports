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

NFL v1 leagues draft a real team's DL, LB, and DB units as three separate
assets. The intended scoring invariant is that defensive credit reflects
expected points removed from the opponent and is not also awarded in full to
multiple units.

Professional defensive attribution is not implemented yet. Consequently the
seeded NFL universe advertises group defense only and does not advertise the
schema-supported `individual` option. Enabling individual NFL defense requires
a separately reviewed attribution implementation and validation; merely
having defender identifiers in nflfastR is not sufficient.

### Weekly totals and realism gate

`play_credits` is the source of truth. `weekly_player_scores` is its
recomputable player/asset-week aggregate. A fantasy team's raw weekly total is
the sum of its starters' weekly asset scores; bench scores do not count.

The live team-score transform is currently fitted to college football and
must not be reused as an NFL calibration. NFL needs its own fitted scale (and
universe-aware scale selection in the platform) before an NFL matchup is
treated as scored. The raw-credit storage model does not need to change for
that work.

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

The v1 roster has 20 assets: 11 starters and 9 bench assets. Shrinking the
bench to nine resolves the prior 21-slot-versus-20-round mismatch while
retaining the kicker starter.

| Slot | Eligible asset position | Count | Starter |
|---|---|---:|---|
| QB | QB | 1 | yes |
| RB | RB | 2 | yes |
| WR | WR | 2 | yes |
| TE | TE | 1 | yes |
| FLEX | RB, WR, TE | 1 | yes |
| K | K, PK | 1 | yes |
| DL | drafted team DL unit | 1 | yes |
| LB | drafted team LB unit | 1 | yes |
| DB | drafted team DB unit | 1 | yes |
| BENCH | any v1 offense, kicker, or group-defense asset | 9 | no |

The default league has 12 fantasy teams and a 20-round snake draft. Offensive
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
`defense_mode` is `group`; an `individual` request is rejected because the v1
NFL universe does not advertise that capability.

`scripts/seed_nfl_smoke_test.py` verifies, entirely in memory, that the API can
create the NFL league and teams, expose the 20 roster slots, generate and fill
all 40 picks of a two-team/20-round snake draft, and reject premature
individual-defense league creation.

## Remaining NFL finish-line work

- ingest NFL teams, players, schedule, and nflfastR play-by-play into the
  shared database;
- implement and validate professional group-defense attribution;
- fit an NFL team-score scale and route scale selection by universe instead of
  using the current college-only parameters;
- persist a scored regular-season week and run the realism gate above;
- run the end-to-end scored-week/matchup/standings flow on that real week.
