# Basketball Scoring — Decisions & Data Sources

**Status:** Decided (design phase). Scoring point values NOT yet set — they come from the NBA analysis (to be run in Claude Code).

**Context:** Football pushed to next cycle (new NFL/NCAAF deadline: Sept 1, 2027; expected to become priority mid-summer 2027). Basketball is now in active design.

## 1. Scoring approach

- Realistic scoring only (per project Principle 1). No ESPN/Yahoo-style flat per-stat points.
- Possession-based expected-points model — basketball's equivalent of football EPA.
- Problems to solve vs. standard fantasy scoring: double credit (scorer + assister both fully credited for one basket), inflated steals/blocks, fantasy totals that don't track real team scores.
- Validation test: a fantasy team's total should track the real team's actual points.

## 2. One model for all basketball universes

- Same model for NBA and NCAAB. No NBA-only tracking-data model.
- Why: Dual Universe crossover requires NBA and NCAA points to mean the same thing; tracking data only redistributes credit (doesn't change team totals); NBA per-play tracking (Second Spectrum) is paid/unreliable to access.
- Rule: tracking data may only ever be added later as an optional layer that redistributes credit within a possession and never changes the team total.

## 3. Stats the model can use (reliable play-by-play only)

"Keep with caution" items were cut entirely — reliable data only.

- Made / missed 2-pt FG
- Made / missed 3-pt FG
- Made / missed free throw
- Assist
- Offensive rebound / defensive rebound (plus team & deadball rebounds, credited to no player)
- Steal
- Block
- Turnover (generic — no subtype)
- Personal foul committed; foul-out
- Technical and flagrant fouls
- Jump ball / possession arrow
- Timeouts
- Game clock + score margin at every event (clutch / garbage-time context)

**Cut:** shot type detail, minutes, plus/minus, fouls drawn, and-1 tagging, turnover subtypes, shot location/distance, charges drawn by defender, shot clock, transition vs. half-court, all tracking stats (screens, deflections, contests, hockey assists, box-outs, time of possession).

## 4. Data sources

### NBA

- **Live (in-game):** NBA Stats live play-by-play feed via `nba_api` (free, unofficial).
- **Final (after game):** official NBA Stats play-by-play via `nba_api` — locks scores.
- **Historical (for scoring analysis):** `shufinskiy/nba_data` archive, 1996–97 to 2024–25, incl. pbpstats possession data. 2025–26 pulled from `nba_api`.

### NCAAB

- **Live (in-game):** ESPN play-by-play feed via `sportsdataverse` / ESPN directly (free, unofficial).
- **Final (after game):** CollegeBasketballData.com (CBBD), free API key — locks scores. Play-by-play from 2005–06; also has substitutions/lineups. Solo, unofficial project; IDs map to ESPN.
- **Historical:** CBBD.

## 5. Live scoring rules

- Live scoring is a v1 requirement.
- Live scores are provisional; they lock to the final source after each game.
- If a game has no live play-by-play, its points post when final data arrives.
- Paid fallback: Sportradar (official, live, both leagues) — not needed until public launch or free feeds fail.

## 6. Open items / next steps

1. Get free CBBD API key.
2. Claude Code: low-major NCAAB coverage test — pull 10–20 games from the weakest conferences; confirm both ESPN live feed and CBBD final data exist and carry every stat in Section 3.
3. Claude Code: load NBA historical data and run the analysis to set point values.
