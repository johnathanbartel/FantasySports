# Hockey Module — Scoring & Structure Spec (v1)

## Core design principle

Same principle as [the football module](../football/README.md): fantasy
points should approximate the real point-value a player's production
actually contributed to their team's actual goals scored/allowed, not a flat
per-stat bonus system that rewards low-value volume shooting or a goalie's
shot count instead of the shots they stopped.

## Expected Goals (xG) model

Every shot on goal has an **xG** value: the historical probability, given
shot location, shot type, and strength state (5v5, power play, shorthanded),
that a shot like it becomes a goal. A player's fantasy points scale with
value created relative to that expectation, not raw counting stats:

- A **goal** is worth 1.0 point, split into two shares: `xG` (the value of
  the chance itself - crediting the possession/passing play that created
  it) and `1 - xG` (the finishing value of actually beating the odds). This
  mirrors the football model's EPA split between the play that created the
  opportunity and the player who converted it.
- An **assist** earns a share of the same goal's `xG` portion: primary
  assist weighted higher than secondary (2:1 split of the `xG` share when
  both exist), the same asymmetric-credit idea as football's air-yards/YAC
  split between a QB and receiver.
- **No double-counting**: a goal's total credited value across shooter +
  assist(s) always sums to exactly 1.0 point, by construction - never a
  flat per-goal bonus stacked on top of per-assist bonuses.
- **Goalie** credit is Goals Saved Above Expected (GSAx): for every shot
  faced, `pre_shot_xg - (1 if goal else 0)`, summed across the game. A
  shutout against a high-danger-chance-heavy opponent is worth more than a
  shutout in a game with few high-quality shots against - the goalie
  equivalent of football's "defense score is points removed from the
  opponent."

## Credit attribution (within a scoring play)

- **Goal, unassisted**: full 1.0 point to the shooter (both the `xG` and
  `1 - xG` shares).
- **Goal, one assist**: `1 - xG` to the shooter, full `xG` share to the
  primary assist.
- **Goal, two assists**: `1 - xG` to the shooter, `xG` share split 2:1
  between primary and secondary assist.
- **Goalie**: GSAx accumulated per shot faced across the game. Empty-net
  goals against are excluded from GSAx (a team's own empty-net decision,
  not a goalie failure) - open question: how to treat delayed-penalty and
  shootout-decision goals, still needs a ruling, same status as football's
  "exact attribution rules still need to be built and validated against
  play-level data" for defensive credit.
- **Hits, blocked shots, penalties drawn/taken, faceoffs**: out of scope
  for v1, same as offensive line scoring being out of scope for football
  v1.

## Roster (20 players: 12 starters, 8 bench)

| Slot | Eligible positions | Count |
|---|---|---|
| C | C | 2 |
| LW | LW | 2 |
| RW | RW | 2 |
| D | D | 4 |
| UTIL (flex skater) | C/LW/RW/D | 1 |
| G | G | 1 |
| BENCH | any | 8 |

20 total matches `League.draft_rounds`'s existing platform-wide default (see
`platform/db/models/core.py`), so a snake draft for an NHL league fills
every roster slot in exactly 20 rounds with no leftover picks, same as the
football module's 20-round default.

Open questions (mirrors football's own open-roster-question style):
same draft pool for all positions, or goalie-specific draft rounds -
leaning same pool, not yet decided. A second starting-goalie slot (many
public hockey leagues start 2 goalies) is deferred to v2 pending scoring
validation against real GSAx variance.

## League

12-team default, customizable size, using the same League/Team/Conference
mechanics the football module already exercises - nothing hockey-specific
needed there since that layer is sport-agnostic
(`platform/db/models/core.py`). Single `nhl` universe at launch
(`level="pro"`); no minor-league/prospect universe yet (open question,
mirrors football's "Dual Universe mode (mechanics TBD)" note).

## Draft

Snake, 20 rounds (matches the 20-slot roster above). The timed sealed-bid
auction draft (`platform/auction/`, `POST /auctions/start`) is
sport-agnostic and works unmodified for an NHL league once a universe and
roster slot definitions exist - no NHL-specific auction work is needed.

## Data pipeline

MoneyPuck is the selected v1 source. It publishes free season-summary roster
CSVs and season ZIPs of shot events with a pre-built `xGoal` probability. To
ingest the 2024-25 season into the normal local runtime database:

```powershell
$env:DATABASE_URL = "sqlite:///./user_run.db"
.venv/Scripts/python.exe scripts/ingest_nhl_rosters_and_shots.py --season 2024
```

The importer is idempotent. It updates changed team/player metadata, creates
missing draftable player assets, and keys every shot by MoneyPuck season,
game, and shot ID so a rerun inserts no duplicates. MoneyPuck's shot ZIP does
not carry game dates, so imported games use the season's week-number-0
container rather than inventing fantasy-week boundaries. Credit MoneyPuck for
uses of its data and review its published terms for uses beyond the free
non-commercial/journalistic allowance.

Shot rows also carry `shooter_asset_id` and `goalie_asset_id` (MoneyPuck
`shooterPlayerId` / `goalieIdForShot` mapped through the player's draftable
asset; null for ids absent from the roster files, which is about 0.1% of
shots, or for an empty net). Reruns backfill players onto shots that lack
them. MoneyPuck has no assist columns, so assists are not stored and v1
attribution folds the assist share into the shooter.

Each game's `home_score`/`away_score` is set from the shot data: goals scored
in regulation and overtime, plus one for the winner of a shootout (MoneyPuck
has no shootout shots; a game tied on shot goals must have gone to a shootout
and `homeTeamWon` names the winner). The 2024 file also contains 86 playoff
games (`isPlayoffGame`), which the importer does not flag.

Pipeline stages:

1. Pull season-correct NHL skater and goalie rosters and the matching
   MoneyPuck shot-event archive.
2. Use a pre-built xG field if the source provides one (MoneyPuck does);
   otherwise derive one from shot location/type/strength state, the same
   sequencing the football module used for EPA.
3. Compute goal/assist/GSAx credit per shot per Credit Attribution above.
4. Aggregate to player-game and team-game totals.
5. Validate: compare team-game fantasy totals against that team's actual
   goals scored/allowed that game - the same reality-check step as the
   football module's step 5.

## Out of scope for v1

Hits/blocks/penalties/faceoff scoring, empty-net and shootout-decision
edge cases in GSAx, a second starting-goalie slot, individual
zone-start/quality-of-competition adjustments, public/multi-tenant release,
and ingestion from sources other than MoneyPuck.
