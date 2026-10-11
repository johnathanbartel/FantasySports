# NHL simulator scoring (FS-026)

Protected scoring work (Claude Code only). Code: `src/fantasy_sports/sports/hockey/simulator/`. It turns the pipeline's stored credits for one fantasy daily game into a tied-out box score and an FS-025 progressive script. Offline audit: `scripts/audit_hockey_simulator_scoring.py`.

## Audit of what already existed (assumptions)

- Attribution is `sports/hockey/pipeline.py` (FS-013/FS-015) and is **not changed**: a goal totals 1.0; with assists the shooter gets `1 - xG` and the assists share `xG` (one assist takes all, two split 2:1 primary:secondary); a blocked shot credits the estimated xG it prevented; goalie credit is GSAx = `pre_shot_xG - goal` per shot faced with empty-net shots excluded; hits are worth 0. Attribution has no randomness.
- Credits are read from the stored `shot_credits` / `event_credits` rows (written by `run_game_validation.py --write`); nothing is recomputed or fetched.
- A fantasy daily game is two lineups of six (pecking order resolved for the date). The lineup's players come from different real NHL games, so the "real clock" on a credit is only used to place the story in time.
- Skaters never have negative credit; only a goalie's goal-against line (`xG - 1`) can be negative.
- Only positive-value items can be a scoreboard goal: earned goal credit, or an inverted opponent error. Blocks and saves are ticker texture.

## Formulas

Per side, with `S` = signed sum of the side's credit lines (the box score), `P` = sum of positive lines, `N` = sum of absolute negative lines:

| Name | Definition |
|---|---|
| Offense `O` | `P_side + N_other` (own positive credit plus the opponent's negative credit, inverted) |
| Margin identity | `O_home - O_away = S_home - S_away` (inversion never changes the net margin) |
| Shares | `λ_home = T (O_home + p) / (O_home + O_away + 2p)`, `λ_away = T - λ_home` |
| Expected margin | `d = λ_home - λ_away = T (O_home - O_away) / (O_home + O_away + 2p)` |
| Error | `ε ~ N(0, σ²)` truncated to `[-C, C]`, seeded; `d' = clamp(d + ε, -T, T)` |
| Goals | `floor((T ± d')/2 + 0.5)` per side |
| Tie | level rounded goals -> the side with positive `d'` gets one deciding goal in the last minutes of the third period (`d' = 0` defers to the raw leader; a dead-level raw game with the error off stays level) |

Constants (`simulator/model.py`) are **provisional product defaults, not fitted values**, and need owner approval: `T = 6.0` (two ~3.0-goal NHL teams), `p = 1.0`, `σ = 0.5`, `C = 1.0`. Example from the spec: raw 5.60 vs 3.40 gives `d = 1.2`, i.e. about 4-2. Over random ledgers the mean combined goals is about 6.4 because level games get a deciding goal. Changing any constant changes every seed's output; bump `ADAPTER_VERSION`.

## Invariants (all checked by the audit)

1. Player points sum to the team total; role totals sum to the team total; `P - N = S`.
2. Inversion conserves value and the net margin.
3. The scoreboard winner has the larger `d'`. Because `|ε| <= C`, the raw leader can only lose when `|d| < C` (1 goal). Outside that window the leader always wins (2,400 seeded games in the audit).
4. For a fixed seed the goal margin is non-decreasing in the raw margin.
5. Displayed scores and shot counters never decrease (also enforced by the FS-025 validator); goal events equal the final scoreboard; shots >= goals.
6. Same inputs + seed + game id -> byte-identical script. Randomness is two SHA-256-derived streams (`noise`, `story`) keyed by `(seed, daily_game_id)`. The real-time window does not change the story. Default seed when publishing a game is its daily game id.
7. Box-score credit is never altered by normalization. Goals beyond the attributable items show as unattributed team goals; goal-sized items not drawn appear as shots or stopped rushes.

## Negative credit

A goalie's goal-against line (`-x`) stays negative in the box score, and becomes an opponent-positive `inverted` item of value `x` on the other side's ledger. If the story draws it as a goal it reads as an odd-man rush or power-play goal `caused_by` that goalie; otherwise it is a narrative chance.

## Provenance

The script's `provenance` (shown with the final snapshot) holds the adapter id and version, seed, input digest, constants, raw and offense totals, the full normalization record (`d`, `ε`, `d'`, upset, tie-break side) and the box score per side.

## Not done / needs owner decisions

Constants above; whether a level raw game should be allowed to end level; NHL hits (worth 0 today, so hitter credit rows raise an error if they ever carry points); using real team-level shot totals instead of the lineup's own attempts; fitting `T`, `σ` and `p` to data.
