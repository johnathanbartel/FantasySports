# NCAAB (college basketball) fantasy windows and flexing: specification

Status: windows optimizer built (`src/fantasy_sports/platform/ncaab_windows.py`, report `scripts/ncaab_window_curve.py`, checks `scripts/seed_ncaab_windows_smoke_test.py`). Flexing, database tables, API, UI, ingestion, scoring and the lock rule are **not built**. Ticket FS-038. Game, flex and window rules settled by the owner on 2026-11-26 (ticket FS-039, see "Settled rules" below).

Related: [hockey spec section 5](hockey_spec.md#5-the-3-game-weekly-schedule--multi-game-window-resolution) (what hockey already does with fixed windows), [basketball scoring decisions](basketball-scoring-decisions.md) (scoring), `analysis/ncaab_schedule/summary.md` (the schedule study this builds on; owner's local files, not in git).

---

## Settled rules (owner's decisions, 2026-11-26)

These six rules are decided. The rest of this document follows them.

1. **Which game counts.** As in hockey, when a player has more than one game in a window, the user picks which of those games counts for his starter slot. The pick must be made before the player's first game in that window starts. If the user makes no pick, the player's first game in the window counts.
2. **One counted game per slot.** Each starter slot counts at most one game per window. A player's other games in that window score nothing there; the only way to use them is to flex them (rules 4 and 6).
3. **Empty slots score 0.** A starter slot with no counted game in a window scores 0 for that window. There is no replacement-level credit in v1.
4. **Flex forward only.** A game can only be flexed into a later window, never an earlier one.
5. **One window plan per season.** Each NCAAB season has one window plan, shared by every NCAAB league; leagues do not choose their own number of windows. The default is 28 windows, taken from the optimizer. The plan is published and frozen before the season's first lock. A postponed game counts in whichever frozen window holds its new start time.
6. **Where a flexed game can go.** A flexed game can move to any later window. If the player already has a game in that window, the flexed game is the one that counts for his slot, and it replaces the user's pick or the first-game fallback from rule 1.

---

## Part 1. Plain-language summary

### What a "window" is

An NCAAB league plays **one head-to-head fantasy game per window**. A window is a stretch of real time, usually two to six days, and the fantasy team that scores more inside it wins that game. A season is simply a row of windows, back to back, from the first Monday of the season to the end of the regular season.

Hockey cuts every week into three fixed windows (Sun-Tue, Wed-Thu, Fri-Sat). College basketball does not fit a fixed shape: some weeks are packed (Thanksgiving tournaments), some are nearly empty (exams in early December, Christmas), and in conference season most teams play once midweek and once at the weekend. So for NCAAB the windows are **cut from the real schedule each season** by a program, the optimizer.

### Rosters

10 players: 5 starters (PG, SG, SF, PF, C) and 5 bench. A starter slot accepts its own position, plus G for the guard slots (PG, SG) and F for the forward slots (SF, PF), as in `ncaab_reference.py`.

### The trade-off the optimizer shows

Two things pull in opposite directions:

1. **Empty starter slots (bad, minimized).** If a window is short, some players' teams don't play in it, and a starter slot can end up with nobody who played; that slot scores 0 for the window (rule 3). More windows = shorter windows = more empty slots.
2. **Surplus games (also bad, but the other way).** If a window is long, many teams play twice or three times in it, but each starter slot counts only one game per window (rule 2). The extra games are "surplus": real games that score nothing unless they are flexed (see below). Fewer windows = longer windows = more surplus games.

There is no single right number of windows. Each season the program runs the real schedule for a range of window counts and shows both numbers side by side; **one point on the curve is used for the whole season, by every NCAAB league** (rule 5). The default is 28 windows, from the optimizer. Leagues do not choose their own window count.

For 2025-26, for example (per 10-player roster, per season):

| Windows | Empty starter slots, expected | Surplus games, expected |
|---|---|---|
| 24 | 0.4 | 70 |
| 28 | 0.9 | 39 |
| 31 | 2.5 | 23 |
| 34 | 6.3 | 15 |

Read it as: with 28 windows, a typical roster expects to field an empty starter slot about once all season, and its players play about 39 games that don't count in the window they were played in.

### Where the windows land

The optimizer is not told about holidays, but it finds them: it puts long windows over Thanksgiving week, the second week of December, the days around Christmas and the whole Nov 10 to Dec 21 stretch, where many teams have light schedules. In conference season it rediscovers a Friday cut on its own, giving a roughly 4-day midweek window and a 2-3-day weekend window.

### Season span

The platform provides **regular-season windows only.** Playoffs, conference tournaments and championships are set up by each league.

- A season starts on its first Monday at 4am Eastern: 2023-11-06, 2024-11-04, 2025-11-03.
- It ends at the Monday 4am after the last full Monday-to-Sunday week before the first conference-tournament game: 2024-03-04, 2025-02-24, 2026-03-02.

### Time

All fantasy time is US Eastern. A fantasy day starts at 4am Eastern, so a late West Coast game stays with the evening it was played. A window runs from one boundary up to, but not including, the next, and a game belongs to the window in which it **starts**.

### Which game counts, and flexing (rules decided, not built yet)

Flexing is how surplus games stop being wasted:

1. **One counted game per starter slot per window.** If a player's team plays several games in one window, only one counts for his starter slot in that window (rule 2). The user picks which one, before the player's first game in that window starts; with no pick, the first game counts (rule 1). A slot with no counted game scores 0 (rule 3).
2. **Forward only, to any later window.** One of the extra games may be moved to a later window, any later window that season (hockey only allows an open window later that same week). Never to an earlier window (rule 4), so a game never moves into a window that has already finished.
3. **Even into a window where the player already plays.** Unlike hockey, the target window does not have to be free: if the player already has a game there, the flexed game is the one that counts for that slot, replacing the user's pick or the first-game fallback (rule 6).
4. **Any eligible position.** The moved game can be placed in any position the player is currently eligible for.
5. **Start or backup.** The manager can either start the player with that game or set him as the backup for the slot.

In the simulation, flexing a surplus game forward cut the empty slots at 31 windows in the worst season from 5.1 to 1.5 per roster (moving only within the same slot) or 1.2 (guards sharing PG/SG and forwards sharing SF/PF); at 28 windows it leaves 0.04-0.16.

### Publishing and freezing a season's windows (rules decided, not built yet)

1. Before the season, once the schedule is published, the platform runs the optimizer over the season span and shows the curve.
2. The season's plan uses 28 windows by default (rule 5). There is one plan per season, shared by every NCAAB league; leagues do not choose their own window count.
3. The plan's boundaries are published to every NCAAB league and **frozen before the season's first lock.** After that, boundaries never move, so no result is ever rewritten.
4. The optimizer works from the real schedule. A postponed or added game after the freeze does not move the windows: it counts in whichever frozen window holds its new start time (rule 5).

### What the numbers do and don't tell you

- The simulation puts every roster player on a different real team and has no real player or position data. Real rosters cluster (several players from one team or one conference), which makes empty slots more likely, not less.
- The windows come from the final schedule. Postponements and added games shift where the gaps are, so a plan built in October will be slightly worse in practice than these numbers.

### Former open questions (all resolved 2026-11-26)

1. **Which game counts** when a player has several in one window: the first, the best, or the average? **Resolved 2026-11-26, rule 1:** the user picks, before the player's first game in the window starts; with no pick, the first game counts.
2. **Cap of one counted game per slot per window**: is it wanted for basketball, and is the surplus unevenness (rosters with many double-game players have more total games) acceptable once flexing exists? **Resolved 2026-11-26, rule 2:** yes, at most one counted game per starter slot per window; extra games are used only through flexing.
3. **A starter slot that ends the season empty**: replacement-level credit, or leave it as a loss? **Resolved 2026-11-26, rule 3:** a slot with no counted game in a window scores 0; no replacement-level credit in v1.
4. **Backward flexing**: may games also move backward in time, and how are rosters helped in the first windows, when nobody has a banked game yet? **Resolved 2026-11-26, rule 4:** no, forward only. There is no special help for the first windows in v1: an empty slot there scores 0 (rule 3).
5. **Publishing**: what gets published and when (the plan must be fixed before the first lock), how postponements are handled (frozen windows with a rule for a moved game), and the default point on the curve. **Resolved 2026-11-26, rule 5:** one plan per season for every NCAAB league, 28 windows by default, published and frozen before the season's first lock; a postponed game counts in the frozen window holding its new start time.
6. **Hockey's guardrail**: does "a surplus game can only go to a window where the player has no active game scheduled" also apply to basketball? **Resolved 2026-11-26, rule 6:** no. A flexed game may go to any later window; if the player already has a game there, the flexed game counts for the slot and replaces the pick or first-game fallback.

---

## Part 2. Technical appendix

### A. Decisions (settled by the owner)

1. Roster: 10 players, 5 starters (PG, SG, SF, PF, C) + 5 bench; G fills PG/SG, F fills SF/PF.
2. One window = one fantasy game (one head-to-head result). No matchup periods, no fixed weekly shape. A season is a sequence of windows over the regular season.
3. Fantasy time is US Eastern; a day starts at 4am. UTC is converted by hand-coded DST rules (no tz database on the machine): DST starts the second Sunday in March at 07:00 UTC and ends the first Sunday in November at 06:00 UTC.
4. One window plan per NCAAB season, shared by every NCAAB league; leagues do not set their own window count. The optimizer reports a curve each season and the plan uses N=28 by default. The plan is published and frozen before the season's first lock; a postponed game is assigned to the frozen window holding its new start time (Settled rule 5).
5. Regular-season span only (see Part 1). Playoffs and tournaments are league-configured.
6. Counting and flexing (Settled rules 1-4, 6): each starter slot counts at most one game per window. With several games in a window, the user's pick counts (pick locks at the player's first game in that window); with no pick, the earliest game counts. A slot with no counted game scores 0 (no replacement-level credit in v1). One extra game may move forward to any later window, never backward and never into a finished window; it fills any position the player is eligible for, as starter or backup. The target window may already hold a game for the player: the flexed game then counts for the slot and overrides the pick or earliest-game fallback (no hockey-style "free window" guardrail).

### B. Cost and surplus

For a window, let `p` be the share of Division I teams with at least one game in it and `s` the average over Division I teams of `max(0, games in window - 1)`.

- **Cost (minimized):** a starter slot is empty when neither of its two players' teams plays, so the expected empty starter slots in the window are `5 * (1 - p)^2`. The season cost is the sum over windows: *expected empty slot-windows per roster per season*.
- **Surplus (reported, not minimized):** with every one of the 10 players on a different team, expected surplus games in the window are `10 * s`. The season surplus is the sum over windows: *expected surplus games per roster per season*. Note `s = (team-games in window - teams that played) / teams`.

Why both: minimizing cost alone always prefers fewer, longer windows (with no length limit, one window for the whole season costs 0). Surplus is what that costs: games that don't count. The curve shows the exchange rate between the two, and that is the decision.

### C. Optimizer

- Candidate boundaries are a grid counted from the season start (Monday 4am Eastern) in steps of `step_hours` (default 4); the season end is always the last grid point.
- `coverage_table` computes, for every pair of grid points no more than `max_hours` apart, the share of teams that play, team-games per team and surplus per team. It sweeps the end point upward from each start point, adding one grid step of games at a time.
- `optimize_windows` is a dynamic program over suffixes: `best[k][i]` = least cost to cover `[grid[i], end)` with exactly `k` windows, each between `min_hours` (24) and `max_hours` (336, 14 days). One table serves every window count, so a whole curve costs one solve.
- **Tie-break:** costs are compared after rounding to 9 decimal places, and among equal plans the lexicographically smallest boundary list wins (earliest first interior boundary, then second, and so on). The plan is rebuilt forward, taking at each step the earliest boundary that keeps the remaining plan optimal. The same input always gives the same plan, regardless of input order.
- Runtime: about 0.3 s per season for the four acceptance counts on the 4-hour grid, about 0.5 s for a 20-36 curve (pure Python, no numpy).
- Note: this interval cost has the Monge property (union coverage is submodular and `(1-p)^2` is convex and decreasing), so optimal plans never need to cross. The smoke test therefore also checks the tie-break on a hand-built coverage table where a "smallest last boundary" rule would pick the wrong plan.

### D. Input rows

Only completed games (status `final`, both scores present), season type `regular`, with at least one Division I team, starting inside the season span. A game against a non-D1 opponent counts for the D1 team only. The team list is the season's CBBD Division I list. The loader is in `scripts/ncaab_window_curve.py`, which also recomputes the season span from the data (first regular-season game's Monday; Monday of the week holding the first conference-tournament game, using the same rule as the schedule study: tournament-type, same-conference, on or after Feb 25).

### E. Data findings and acceptance numbers

Data: CollegeBasketballData.com saved pulls, seasons 2023-24, 2024-25, 2025-26 (CBBD 2024, 2025, 2026). 362 / 364 / 365 Division I teams; 6,243 / 6,292 / 6,300 completed games (all season types, including non-D1 opponents). Teams play about 31 regular-season games (median 31, range 27-32, checked on the saved files). Measured over the fantasy season span (4am fantasy days): about 25% of teams play on a given day (26-27% from early January); Saturday holds 33-35% of team-games over the whole span and 42-46% in conference season (from early January), Thursday 12-14% and 16-17%. (The schedule study's shorthand "29% per day, Saturday 43%, Thursday 18%" is close for conference season on the weekday shares; the 29% per-day figure did not reproduce.)

Expected empty slot-windows per roster per season, 4-hour grid, 24-336 h windows (prototype figure in brackets):

| Season | N=24 | N=28 | N=31 | N=34 |
|---|---|---|---|---|
| 2023-24 | 0.43 (0.5) | 1.04 (1.2) | 2.58 (2.8) | 6.58 (7.0) |
| 2024-25 | 0.53 (0.6) | 1.69 (1.8) | 5.05 (5.2) | 10.45 (10.6) |
| 2025-26 | 0.38 (0.4) | 0.94 (0.9) | 2.49 (2.5) | 6.30 (6.3) |

The 2023-24 N=28/31/34 values sit just outside the tolerance (0.15 or 5%). The cause is in the prototype: it divided every season by 365 teams (the 2025-26 count) instead of that season's own list. Run with a 365-team divisor, this optimizer gives 0.53 / 1.22 / 2.85 / 6.96 for 2023-24 and 0.57 / 1.76 / 5.16 / 10.60 for 2024-25, matching the prototype. The numbers above use the correct per-season lists.

Expected surplus games per roster per season (`10 * s` summed):

| Season | N=24 | N=28 | N=31 | N=34 |
|---|---|---|---|---|
| 2023-24 | 69.2 | 39.3 | 23.6 | 15.5 |
| 2024-25 | 52.0 | 26.4 | 17.6 | 12.0 |
| 2025-26 | 69.7 | 38.7 | 22.8 | 15.2 |

The roster simulation's figures (41-56 at N=24, 24-33 at N=28, 17-21 at N=31) are lower because the simulation counted surplus per starter slot (the larger of the slot's two players' extra games), not for all 10 players. Both measures move the same way.

Fixed weekly shape for comparison (Mon-Fri and Fri-Sun windows plus a long holiday window, 30-32 windows): 5.3-6.4 empty slot-windows.

Caveats: players are placed on different teams at random with no position data; real clustering makes things worse; windows come from the final schedule, so postponements and added games shift them.

### F. What is not built

Windows or flex tables, migrations, API routes, flexing UI, NCAAB schedule and box-score ingestion, scoring, the NCAAB lock rule, conference tournament and championship brackets.
