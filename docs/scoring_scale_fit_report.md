# Scoring scale fit - report

Answers `docs/scoring_scale_fit.md`. Code: `platform/scoring_scale.py` (the function),
`sports/football/college/data/scoring_scale.json` (fitted numbers),
`sports/football/college/scripts/{build_weekly_credits,fit_scoring_scale,validate_scoring_scale}.py` (how it was fitted and checked).

## 1. The function and its context

```python
from fantasy_sports.platform.scoring_scale import ScaleContext, to_fantasy_points, context_for_league

ctx = context_for_league(db, league_id)          # once per league
points = to_fantasy_points(lineup_credit_sum, ctx)
```

`ScaleContext` has exactly two fields:

| field | meaning | where it comes from |
|---|---|---|
| `n_teams` | teams in the league | `db.query(Team).filter_by(league_id=...).count()` (count real rows; `league.size` is what was requested) |
| `starter_slots` | starting slots as `{slot_code: count}`, e.g. `{"QB":1,"RB":2,"WR":2,"TE":1,"FLEX":1,"DL":1,"LB":1,"DB":1,"K":1}` | `RosterSlotDefinition` rows with `is_starter`, summed `slot_count` by `slot_code` (bench excluded) |

`context_for_league` does both. Nothing else is needed: no per-player data, no other teams' scores.

**Wiring note - important.** Today `scoring.lineup_breakdown` calls `to_fantasy_points(raw)` *per starter* and
`team_week_score` sums those. The scale is an affine-plus-floor map of the **team total**, so applying it per starter
and summing would add the baseline eleven times. Apply it once, to the summed credit:

```python
def team_week_score(db, team_id, week_id):
    lines = lineup_breakdown(db, team_id, week_id)              # StarterLine.raw_points stays raw credit
    return to_fantasy_points(sum(l.raw_points for l in lines), context_for_league(db, league_id))
```

For a per-player display, show `raw_points * b` (b ~ 0.97 in `scoring_scale.json`) as each starter's contribution; the
difference to the team score is the league baseline. Cache the context per request (it is two small queries).

## 2. What the scale is, and why

```
z      = 28.11 + b * (lineup_credit - E[lineup credit for THIS league]) + c
points = 3 * ln(1 + exp(z / 3))                                   # smooth floor, no clip
```

**Requirement 1 - league average = NCAA average.** `28.11` is the NCAA FBS points per team per game (regular season, all
games involving an FBS team, 2022-24, n = 4,831 team-games; sd 14.17). The scale is centred on what a lineup like *this
league's* normally scores in credit, `E[lineup credit]`, which is the only league-dependent part. It depends on:

- **league size** - a drafted lineup averages ~+20 credit a week, not ~0 (the full-population mean); the deeper a
  league drafts, the weaker its starters: **+24.7 (4 teams) -> +20.2 (12) -> +17.8 (24)**. Ignoring this would make a
  6-team league average ~31 points and a 20-team league ~26.
- **lineup shape** - the expectation is the sum of a per-slot table (`QB#1`, `RB#1`, `RB#2`, ... `K#1`), so the scale
  follows the league's slots. A shape it was not fitted for raises `ValueError` rather than guessing.
- **who is actually rostered** - via the two above: the table is the expected credit of the starters a league of that
  size *ends up with*, measured by simulating drafts (snake, value over replacement, position caps, forced starter
  fills, drafters ranking by *prior*-season production plus noise) on real 2023-24 seasons and scoring the lineups on
  that season's *actual* weekly credit - so regression to the mean, byes and injured/departed players (0 weeks) are in.
  Drafter skill barely matters (12 teams: 20.0-20.6 for perfect through very noisy drafters).

**b = 0.972** matches the spread of fantasy scores to the NCAA spread of team points (lineup credit sd 14.58 vs NCAA
14.17): one credit is ~one football point. It is one number for all league sizes (lineup sd stays 13.3-15.2 across 4-24
teams and drafter skill).

**Requirement 4 - negatives.** A plain affine map puts 1.7% of team-weeks below 0 (min about -32). The floor is a
**softplus** (`A = 3`): strictly increasing everywhere, always > 0, indistinguishable from the line above ~6 points,
compressing only the extreme lower tail into (0, ~3). No clip, so no ties. `c = -0.159` re-centres it so the mean is still
exactly 28.11. Cost: a display rounded to one decimal shows two hopeless weeks as `0.0`; the stored floats stay ordered.
Upper tail is uncapped: 2.0% of team-weeks are >= 60 vs 1.9% of real NCAA team scores.

## 3. Fitted numbers (`scoring_scale.json`)

| | |
|---|---|
| NCAA points mean / sd | 28.114 / 14.174 |
| `b` (points per credit) | 0.97191 |
| lineup credit sd (around the league-size mean) | 14.58 |
| softplus `A` / `c` | 3.0 / -0.15925 |
| fitted on | 2023, 2024 seasons, 200 simulated leagues per size per season, 10-week season |

Mean credit of one starter, by slot and league size (linear in size between the points, flat outside 4-24):

| teams | 4 | 6 | 8 | 10 | 12 | 14 | 16 | 20 | 24 |
|---|---|---|---|---|---|---|---|---|---|
| QB#1 | -0.67 | -0.72 | -0.77 | -0.82 | -0.94 | -0.98 | -1.02 | -1.02 | -0.96 |
| RB#1 | 4.33 | 3.86 | 3.52 | 3.42 | 3.24 | 3.07 | 2.98 | 2.90 | 2.76 |
| RB#2 | 2.96 | 2.60 | 2.53 | 2.35 | 2.29 | 2.35 | 2.30 | 2.15 | 2.04 |
| WR#1 | 2.85 | 2.83 | 2.78 | 2.81 | 2.77 | 2.79 | 2.78 | 2.74 | 2.72 |
| WR#2 | 2.87 | 2.80 | 2.76 | 2.67 | 2.69 | 2.68 | 2.63 | 2.57 | 2.48 |
| TE#1 | 2.06 | 1.98 | 1.85 | 1.79 | 1.75 | 1.71 | 1.65 | 1.60 | 1.52 |
| FLEX#1 | 2.69 | 2.50 | 2.48 | 2.47 | 2.55 | 2.56 | 2.55 | 2.43 | 2.37 |
| DL#1 | 1.87 | 2.08 | 1.97 | 1.90 | 1.80 | 1.77 | 1.72 | 1.56 | 1.37 |
| LB#1 | 1.53 | 1.41 | 1.30 | 1.22 | 1.11 | 1.08 | 1.04 | 1.01 | 0.96 |
| DB#1 | 3.87 | 3.29 | 3.25 | 2.89 | 2.59 | 2.47 | 2.32 | 2.14 | 2.12 |
| K#1 | 0.33 | 0.33 | 0.36 | 0.36 | 0.39 | 0.40 | 0.40 | 0.40 | 0.38 |
| **lineup** | **24.68** | **22.95** | **22.02** | **21.07** | **20.22** | **19.90** | **19.35** | **18.47** | **17.76** |

## 4. Requirement 1 - evidence

**Shipped parameters on 1,200 fresh simulated leagues** (drafts the fit never saw; 200 leagues x 2 seasons per size):

| teams | avg lineup credit | avg fantasy score | vs NCAA 28.11 | sd of a league's season-avg score | score sd |
|---|---|---|---|---|---|
| 8 | 22.17 | **28.24** | +0.13 | 1.10 | 14.0 |
| 12 | 20.25 | **28.15** | +0.04 | 0.90 | 14.0 |
| 20 | 18.48 | **28.12** | +0.01 | 0.82 | 13.7 |

Average credit differs by 3.7 across these leagues; average score by 0.1.

**Out of sample** (fit on one season only, tested on the other, `validate_scoring_scale.py A`): league averages land
within about **+/-0.7** of the NCAA number for 12-20 teams and +/-1.7 for 5-6 teams. The residual is a **season effect**,
not a league-size effect: fit-on-2023/test-2024 runs ~0.6 low and fit-on-2024/test-2023 ~0.6 high. A single league's
season average also wanders by draft luck (sd ~0.9 points at 12 teams, ~1.6 at 5-6). So "equals the NCAA average" holds
in expectation and to roughly a point per league - not exactly - see 7.

## 5. Requirement 2 - evidence

The map is strictly increasing by construction (`b > 0`, softplus' > 0) and depends only on the league, so equal credit
ties and nothing else does. Checked (`validate_scoring_scale.py B, C`):

- league 1, weeks 1-3 (`user_run.db`, read-only): all 198 team pairs (66 x 3 weeks) - **0 order violations**, 0 raw ties;
  all 18 scheduled matchups - **0 winner flips**. Same on the credits re-scored with the fixed parser (section 6).
- dense grid over [-150, +150] credit at 11 league sizes: strictly increasing, always > 0, deterministic.

## 6. Credit-to-player matching: what inflates or deflates totals

Checked against 2026 weeks 1-3 re-scored offline from CFBD (read-only; the DB was not modified) with the app's own
matching. **The biggest problems are upstream of the scale and were making the stored credits wrong, not just noisy.**

### 6.1 The 2026 play text was not being parsed (fixed in `pipeline.py` / `defense.py`)
CFBD's 2026 text is a different format from 2022-24 (`"Shotgun #9 D.Gardner rush middle for 5 yards gain"`,
`"... pass complete short right to #2 D.Roebuck caught at ..."`, `"#15 W.Stone field goal attempt from 34 yards GOOD"`).
The parser only knew the old phrasing, so in the stored 2026 data:

| play type | unattributed before -> after |
|---|---|
| Rush | 98% -> 0% |
| Pass Reception | 99% -> 0% |
| Rushing / Passing TD | 91% / 83% -> 0% / 1.6% |
| Field goals (kicker credit) | 88% -> 0.1% |

Only the *negative* plays (incompletions, sacks, interceptions) parsed, so QBs were credited with the losses and none of
the gains: **stored league-1 lineup credit averages ~0 (fantasy score ~8-12); with the fix it averages 24-30**, in line
with the same weeks in the 2023/24 simulation. Names are now normalised at the source (`"D.Gardner"`, not
`"No Huddle-Shotgun #9 D.Gardner"`). 2022 output is byte-identical to before; 2024 differs by 97 plays (a few teams already
used the new format). Defenders named as `#7 C.Kennedy` now resolve to DL/LB/DB: sacks 1% -> 75%, interceptions ~0 -> 96%,
forced fumbles 71%, breakups 28%. **All of it needs 2026 weeks 1-3 re-ingested to take effect.**

### 6.2 Kickers can never score (app, `ingest_scores.py` - NOT changed)
CFBD gives field goals `ppa = None`, and `score_weeks` only creates a `Play` row when `ppa` is present, then drops any
credit whose play has no row. All 429 PK assets have zero score rows. Fix: create the `Play` row (or record the credit)
for plays that carry a kicker credit even when `ppa is None`. Effect on the scale is small (a K averages ~0.4 a week), but
that lineup slot is dead today.

### 6.3 FCS opponents: 19.7% of absolute credit (you measured ~22%)
Not a bias on rostered players: it is the *other side's* credit, and FCS players have no assets. It is concentrated in weeks
1-3 (full-season it is 6.5%). It matters in one way: FBS teams look better against FCS teams, which is why early weeks run high (7).

### 6.4 Unmatched FBS names: 2.3% today, recoverable to 0.9%
Higher than your ~1% *because* rushes and completions now parse (more names to match). Causes, by share of absolute credit:
initial not matched to any pool player 1.48%, ambiguous initials 0.39%, unparseable 0.47%. They are real starters in the pool:
`J.Richter` (Toledo) is `John Alan Richter` and `J.Sagapolutele` is `Jaron-Keawe Sagapolutele`; the app's
`full_name.partition(" ")` makes their "last name" `alan richter` / `keawe sagapolutele`. `D.Moore` (Oregon) is both QB Dante
Moore and WR Dakorien Moore, so both lose it. The `"Shotgun #6 J.Richter (Toledo)"` form was the parser leaving the formation
prefix on the name (fixed) - the residual is the multi-part first name. **Fix (measured offline):** also index each player by
`(first initial, LAST TOKEN of the name)`, and when initials are ambiguous pick the candidate whose position fits the credit's
role (passer -> QB, receiver -> WR/TE/RB, kicker -> PK). **Unmatched FBS credit 2.35% -> 1.07% -> 0.88%.**

### 6.5 Structural things in the credit itself (not bugs; they shape scores)
- **QBs are net-negative by construction.** Over a season passers sum to about -13.7k credit and receivers +23k: the
  receiver's after-catch share (an NFL-derived lookup) is larger than what is left of a completion. A rostered QB
  averages -0.7 to -1.0 a week vs +2.8 for a WR. The scale re-centres it (so QB is not penalised in the *total*), but QBs
  are worth less than WRs to every lineup.
- **Two slots carry 48% of lineup variance**: DB group 27.6% and QB 20.0%, vs WR + TE 8%. Defense-group credit is the whole
  team defense split three ways, so it swings.

## 7. Limits and decisions for you

1. **Requirement 1 holds over a season, not week by week.** Weeks 1-3 average ~28-43 points and weeks 7-10 ~22-27 (12 teams,
   2023/24), because early schedules are full of FCS and non-conference games. That is real replay signal, and re-scored 2026
   weeks 1-3 match it. If you want *every* week to average 28, add `week_number` to the context and a weekly offset table
   - but week 1 differs by ~8 points between 2023 and 2024, so that table would be noisy, and it erases real schedule
   effects. I did not build it; say if you want it.
2. **The year effect is +/-0.6 points and the league draw ~+/-1** (larger below 8 teams). Exact equality would need a
   true-up from the league's own realised credit (season-to-date mean), which changes every past week's score as the
   season goes on. I chose the stable ex-ante map; the true-up is a small addition if you want it.
3. **Assumptions in the simulated leagues**: lineups fixed all season and set by prior-season ranking (real managers will
   sometimes do better); 10-week season (`platform/season.py`); a pool of the season's roster. League sizes are fitted for
   4-24 teams; outside that the map is held flat at the nearest fitted size.
4. **The fit is on 2023-24 credits and the 2026 data are re-parsed**, so the 2026 check is consistent but is only three weeks.
   Refit (`build_weekly_credits.py` then `fit_scoring_scale.py`) when 2025/2026 seasons are cached.

## 8. Reproduce

```
.venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/fetch_season_plays.py --years 2022 2023 2024
.venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/build_weekly_credits.py
.venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/fit_scoring_scale.py
.venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/validate_scoring_scale.py
```
