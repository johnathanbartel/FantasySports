# NCAAB Coverage Test — Do the Free Data Sources Cover the Weakest Conferences?

**Verdict: Yes, good enough for launch — with one real gap (about 1 game in 15 had no play-by-play at all) and one thing to know (the "final" source is a copy of the "live" source, not a second opinion).**

Test run 2 Nov 2026. Checked against the stat list in `docs/basketball-scoring-decisions.md`, Section 3.

## What I tested

- **15 regular-season games from 2025-26**, three each from SWAC, MEAC, NEC, Southland and Big South. Both teams in every game belong to that conference. I picked early-, mid- and late-season games.
- For each game I pulled the play-by-play from **ESPN** (the planned live source) and from **CollegeBasketballData.com, "CBBD"** (the planned final source).
- I counted every event type in both and checked that each stat on the keep list shows up.
- **CBBD requests used: 42 of the 75 allowed.** (Separate reads of CBBD's public help pages did not use the key.)

## Results by game

"Missing stats" means keep-list items that never appear in that game. Several keep-list items don't happen in every game: a technical foul or a player fouling out is rare. Those are marked "none happened", not "missing".

| Game | Conference | ESPN play-by-play | CBBD play-by-play | Missing stats |
|---|---|---|---|---|
| Grambling at Florida A&M, 13 Jan | SWAC | **No** (0 events) | **No** (0 events) | **Everything — no play-by-play in either source** |
| Jackson State at Arkansas-Pine Bluff, 9 Feb | SWAC | Yes (418) | Yes (418) | None |
| Southern at Alabama State, 4 Mar | SWAC | Yes (478) | Yes (481) | None |
| South Carolina State at Coppin State, 13 Jan | MEAC | Yes (552) | Yes (552) | None |
| Delaware State at Norfolk State, 14 Feb | MEAC | Yes (467) | Yes (467) | None |
| Coppin State at Howard, 3 Mar | MEAC | Yes (449) | Yes (449) | None |
| Central Connecticut at Le Moyne, 10 Jan | NEC | Yes (468) | Yes (468) | None |
| St. Francis (PA) at Mercyhurst, 6 Feb | NEC | Yes (511) | Yes (511) | None |
| Fairleigh Dickinson at Le Moyne, 27 Feb | NEC | Yes (419) | Yes (419) | None |
| Nicholls at UT Rio Grande Valley, 31 Dec | Southland | Yes (441) | Yes (441) | None |
| McNeese at SE Louisiana, 28 Jan | Southland | Yes (528) | Yes (528) | None |
| Nicholls at Lamar, 24 Feb | Southland | Yes (421) | Yes (421) | None |
| High Point at Charleston Southern, 10 Jan | Big South | Yes (604) | Yes (604) | None |
| Winthrop at Radford, 5 Feb | Big South | Yes (550) | Yes (550) | None |
| UNC Asheville at Gardner-Webb, 27 Feb | Big South | Yes (433) | Yes (433) | None |

(Numbers in brackets are events in the game.) **Totals: ESPN 14 of 15 games, CBBD 14 of 15 games.** The one game missing from ESPN is missing from CBBD too.

## Does every keep-list stat appear? (the 14 games that have play-by-play)

| Keep-list item | In ESPN? | In CBBD? | Notes |
|---|---|---|---|
| Made / missed 2-point shot | Yes | Yes | 496 made, 478 missed — identical in both |
| Made / missed 3-point shot | Yes | Yes | 196 made, 417 missed — identical |
| Made / missed free throw | Yes | Yes | 450 made, 187 missed — identical. Quirk: both sources label every free throw "made free throw"; whether it went in is only in the text ("makes" / "misses"), so our code must read the text |
| Assist | Yes | Yes | 351 in both. ESPN writes it in the description ("…(Name assists)"); CBBD also gives it as a separate field, which is easier to use |
| Offensive / defensive rebound | Yes | Yes | 313 / 679 in both |
| Team and deadball rebounds (no player) | Yes | Yes | 89 deadball team rebounds, plus 103 other rebounds with no player named (team rebounds). Handled the way the doc says |
| Steal | Yes | Yes | 197 in both |
| Block | Yes | Yes | 101 in both |
| Turnover (generic) | Yes | Yes | 350 in both. 16 were team turnovers with no player |
| Personal foul | Yes | Yes | 523 in both |
| **Foul-out** | **Not an event** | **Not an event** | Neither source marks a foul-out. It can be worked out by counting: 9 players reached a 5th foul across the 14 games, identical in both. See "Things to know" |
| Technical foul | Yes | Yes | 4 in both (3 games). Some are on a bench or team with no player named |
| **Flagrant foul** | **Never appeared** | **Never appeared** | Zero in 14 games, so I can't confirm either way. See "Things to know" |
| Jump ball / possession arrow | Yes (jump balls) | Yes (jump balls) | 42 in both. The arrow itself is not an event; it shows up as who gets the ball after a jump ball |
| Timeouts | Yes | Yes | 192 in both (TV, short and regular) |
| Game clock and score on every event | Yes | Yes | **Zero events missing a clock, period or score in either source**, across all ~6,700 events |

**Score check:** In all 14 games, the score on the last event matches the real final score, in both sources.

## Step 3 — Do CBBD's player records carry ESPN player IDs?

**Yes.** Each CBBD player record has a field called `sourceId`, and it is the ESPN player ID.

I confirmed this on real players rather than assuming it:
- I lined up the same plays in ESPN and CBBD (CBBD's play IDs are ESPN's play IDs) and paired up 268 players.
- Not one player paired with two different ESPN IDs.
- I then pulled the CBBD team lists for one team from each of 13 games and checked 145 players: **all 145 had a `sourceId` equal to their ESPN ID. Zero mismatches.**

The catch: CBBD's *play-by-play* names players by CBBD's own number (for example 13681), not the ESPN ID. So matching live to final is easy but takes one extra step — look up each player's `sourceId` from the team list once per team. No guessing or name-matching needed.

## Step 4 — Can our CBBD key reach CBBD's live play-by-play?

**No.** CBBD does not offer a live play-by-play feed. Its published list of 40 endpoints has no live route, and the only live-style one, `/scoreboard`, answered "requires a Patreon subscription at Tier 1 or higher" when I tried it with our key. The free key does reach CBBD's play-by-play for finished games. Live play-by-play has to come from ESPN, which matches the plan. I did not upgrade anything.

(Our key is the existing CollegeFootballData key from `.env`. It works on CBBD too. `.env` has no separate CBBD key.)

## Things to know

1. **One game in 15 had no play-by-play anywhere.** Grambling at Florida A&M (13 Jan, SWAC) has a final score and a box score but zero plays in both ESPN and CBBD. That's the "no live play-by-play" case the scoring doc already plans for ("points post when final data arrives"), but it means we'd have no play-level data to score it from. With one miss in 15, I can't say how often this happens. A wider scan of SWAC games would tell us.
2. **CBBD is not an independent second source.** Its play IDs are ESPN's play IDs, and event counts match ESPN exactly in 13 of 14 games (the 14th differs by 3 substitutions, which don't affect scoring). So CBBD works as a saved copy of ESPN's feed, with extra fields, rather than a cross-check. If ESPN has a wrong or missing play, CBBD will most likely have it too. "Lock to the final source" still works, but it won't catch ESPN mistakes.
3. **Foul-out has to be calculated.** Count each player's personal fouls and treat the 5th as the foul-out. Both sources give the data to do it.
4. **Flagrant fouls are unconfirmed.** None happened in my 14 games, so I can't say how the sources label them. Worth checking against a bigger sample, or a known flagrant foul, before relying on it.
5. **Free-throw and 3-point detection reads text.** Both sources label every free throw "made". Misses show in the description, and 3-pointers show as "three point" in the description (ESPN also gives a points-attempted number). Both are easy but must be done on purpose.

## Method

Scripts live in the session scratch folder, outside the repo, so nothing was added to the codebase. The API key was read from `.env` at run time and was never printed or saved to any file. No app code was changed and no database was touched. Only the two docs were added: this file and `docs/basketball-scoring-decisions.md`.
