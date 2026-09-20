# Football Module — Scoring & Structure Spec (v1)

## Core design principle

Fantasy points should approximate the real point-value that a player's
production actually contributed to their team's real score - not a flat
per-stat bonus system that double-counts touchdowns and rewards yardage that
never produced points.

## Expected Points (EP) model

For a given down/distance/yard-line situation, EP is the historical
probability-weighted point value of drives that started from that (or a
similar) situation. A player's fantasy points for a play = the **EP Added
(EPA)** by that specific play, split among the players involved (see Credit
Attribution below). EPA is additive and non-duplicative across a drive by
construction.

- Field position matters correctly (a 2-yard gain at the 3 is worth far more
  than a 2-yard gain at your own 6).
- Kickers are valued correctly: the marginal EPA of the FG itself, not a flat
  3 points, since the drive's EP accumulation already captured most of the value.
- No double-counting: a completion's EPA is split by role, not credited in
  full to both the QB and the receiver.
- Defense/special teams score is points *removed* from the opponent: a
  negative-EPA play for the offense is a positive for the defense.

## Credit attribution (within a play)

- **Passing plays**: split between QB and receiver, weighted toward air
  yards (QB's share) vs. yards after catch (receiver's share).
- **Rushing plays**: full EPA to the ball carrier. Offensive line scoring is
  out of scope for v1.
- **Defensive plays**: negative EPA on a stopped/negative play attributed to
  the position group responsible (DL / LB / DB). Exact attribution rules
  still need to be built and validated against play-level data.

## Roster (20 players: 10 starters, 10 bench, no IR in v1)

| Slot | Count |
|---|---|
| QB | 1 |
| RB | 2 |
| WR | 2 |
| TE | 1 |
| FLEX (RB/WR/TE) | 1 |
| DL (group) | 1 |
| LB (group) | 1 |
| DB (group) | 1 |

Defense is drafted as position **groups** (DL/LB/DB), not individual players
and not a monolithic team D/ST. Open question: same draft pool as offense, or
reserved rounds (IDP-style)? Leaning same pool - not yet decided.

## League

12-team default, customizable size. College and NFL universes at launch,
plus a Dual Universe mode (mechanics TBD). AI-owned teams with distinct
personalities fill out incomplete leagues (design TBD).

## Draft

Snake, 20 rounds. A timed sealed-bid auction draft is also available
(`platform/auction/`, `POST /auctions/start`) as an alternative to the snake
draft - see the "Decision: auction draft budget & round resolution" section
of `docs/data_contract.md`.

## Data pipeline

1. Pull play-by-play for a recent season (NFL: nflfastR; NCAAF:
   CollegeFootballData.com).
2. Build/derive the EP model by down/distance/yard-line if not using a
   pre-built EPA field.
3. Compute EPA per play, split among involved players per Credit Attribution.
4. Aggregate to player-week and team-week totals.
5. Validate: compare team-week fantasy totals against that team's actual real
   points scored that week. This is where real PPG gets *derived*, not assumed.

## Out of scope for v1

Salary-cap scoring itself (the auction draft's per-team budget is separate
from the still-unused `League.salary_cap_enabled` field), offensive line
scoring, individual defensive player drafting, public/multi-tenant release,
full Dual Universe mechanics, AI owner personality details.
