# Scoring scale fit - prompt for the analysis chat

Paste everything below the line into the other chat.

---

You built the EPA attribution model (`pipeline.process_plays`, `shared/defense.py`). The app now uses it for
real: each player's / defense group's credit for a game is summed into `weekly_player_scores.points` (raw EPA
credit), and a fantasy team's weekly score is the sum of its starters' weekly scores (see
`src/fantasy_sports/platform/scoring.py`). Raw credit is not on a football-points scale yet - a 12-team league
currently shows team scores from about -30 to +23. I need you to fit the scale.

**What to deliver:** one function, `to_fantasy_points(raw_team_credit, context) -> float`, which the app applies
in `scoring.to_fantasy_points` to a team's lineup-summed credit for a week. Tell me exactly what `context` needs
(e.g. league size, number/type of starters, lineup slots) and how to compute it from data the app already has.

**Requirements (all hard, from the product owner):**

1. **League average = NCAA average.** Over a season, the average team score in a league must equal the NCAA's
   average points per game (per team per game, FBS). Scoring must therefore be effectively the same regardless of
   league size - a 6-team and a 20-team league should both average that number. The fit may be league-dependent
   (it may use league size, roster/lineup shape, which players are actually rostered) to achieve that. Say which,
   and why. Note that rostered players are not average players - drafted starters are better than the pool - so
   the scale has to account for who is actually in a lineup, not only the full-population mean.
2. **The EPA leader wins 100% of the time.** If team A's lineup-summed credit is higher than team B's in a week,
   team A must score more fantasy points than team B. The mapping must be strictly increasing. No randomness, no
   noise, no caps or floors that create ties, nothing invented: "we aren't inventing anything". Fantasy points are
   a monotone re-expression of the EPA replay, nothing else. (Ties in raw credit may tie.)
3. **Same mapping for every team in a league in a week**, so a single number can be compared across matchups.
4. Scores should be sensible football-point numbers (roughly 0-60, mean ~ the NCAA PPG). If the natural mapping
   can go negative for very bad weeks, say how you handle it without breaking (2) - a monotone shift is fine.

**Data available:** the live DB `user_run.db` has 2026 weeks 1-3 scored (games from 2026-09-03; `games`, `plays`,
`play_credits`, `weekly_player_scores`), each ~11 starters per lineup (QB, 2 RB, 2 WR, TE, FLEX, PK, and DL/LB/DB
defense groups - check `roster_slots` for the exact league slots), and a finished multi-season cache (2022-2024)
of the same pipeline output you can fit on. League 1 is an all-AI test league with drafted rosters and lineups for
weeks 1-3. Team-level results you already found (points ~ 21.4 + 0.76 x team EPA; EPA winner wins 89.8% of real
games) describe REAL teams; fantasy lineups are a different aggregation, so re-derive from lineup-level data.

**Please also report:** the fitted numbers, the average lineup credit vs. average fantasy score you get in a
simulated 8-, 12- and 20-team league (proving requirement 1), a check that requirement 2 holds for every matchup
in league 1 weeks 1-3, and anything in the credit-to-player matching that inflates or deflates totals (about 22%
of absolute credit in 2026 weeks 1-3 goes to FCS opponents' players who have no assets in the app, and ~1% to FBS
names we couldn't match, mostly first-name-only strings and "Shotgun #6 J.Richter (Toledo)"-style play text).
