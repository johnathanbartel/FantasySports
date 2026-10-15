# NHL v1 Universal Simulator, Series Matchup, and Pecking Order Specification

## 1. Status and Architectural Scope

This specification establishes the hockey (NHL) design and operational contract within the multi-sport platform, integrating the **Cross-Sport Game Simulator — Universal Platform Specification (v1.0.0)** and the **Points-Only Series & Pecking Order Platform Directive**.

The architecture maintains the platform's established separation of concerns:
- **Core Platform Engine** (`src/fantasy_sports/platform`): League scheduling, multi-day series generation, calendar management, draft engines, user entitlements, spoiler veil state, and asynchronous finality tracking.
- **Sport-Specific Adapter** (`src/fantasy_sports/sports/hockey`): Play-by-play ingestion (MoneyPuck/NHL API), xG credit attribution, goalie GSAx calculations, score normalization, simulation error/randomness modeling, and animation event scripting.
- **Frontend 2D Arcade Engine & UI** (`web/static/simulator`, `web/static/`): Canvas/SVG 2D ice rink renderer, real-time progressive period simulation viewer, 7-day series tabs with Game Score heatmap, and live draft broadcast room.

---

## 2. League Format: The Multi-Day "Series" Model

Hockey eliminates disconnected single-game weeks in favor of authentic **multi-day "Series"**.

### 2.1 Series Structure
- **Opponent Continuity**: Throughout a series, a team battles the **same opponent** every day for **5, 7, or 10 days** (default: **7-day series**, aligning with a standard calendar week).
- **Daily Head-to-Head Games**: Each scheduled Matchup Day within the series is an independent daily game featuring its own:
  - 6-man starting lineup (resolved automatically via the Pecking Order).
  - Progressive 3-period simulation.
  - Final normalized score and Win/Loss result.
- **Series & League Standings**:
  - Primary standings track **Series Wins/Losses** (e.g., winning 4 out of 7 daily games claims the series).
  - Tiebreakers and secondary standings track **Daily Game W/L records** and **Net Goal Differential**.

---

## 3. Scoring Contract: Strict Points-Only Requirement

**Points leagues are mandatory.** The platform does not offer head-to-head categories, rotisserie, or subjective scoring formats.

### 3.1 Expected Goals (xG) Goal-Value Model
Fantasy scoring is anchored strictly to real point-value contributions toward goals scored and prevented:
- **Goals (1.0 Total Points)**:
  - Unassisted: 1.0 point to the shooter ($xG$ chance creation + $1 - xG$ finishing value).
  - Assisted: Shooter receives $1 - xG$; assists share the $xG$ chance value (single assist receives full $xG$; two assists split $xG$ 2:1 primary:secondary).
  - Skater points on a team always sum to the team's real regulation/OT goals by construction.
- **Defense & Blocks**:
  - Blocked shots earn credit based on the estimated $xG$ of the shot attempt prevented (`pipeline.py`).
- **Goalie Credit**:
  - Goals Saved Above Expected: $\text{GSAx} = \text{pre\_shot\_xg} - (1 \text{ if goal else } 0)$, accumulated across every non-empty-net shot faced.

---

## 4. Lineup Architecture: 6 Positions & The 3-Line Pecking Order

To eliminate daily lineup churn, hockey implements a **3-Line Pecking Order System**.

### 4.1 The 6-Position Starting Unit
Every daily game features exactly 6 active starters per team:
- **1 Center (C)**
- **2 Wingers (W)** (LW and RW eligible)
- **2 Defensemen (D)**
- **1 Goalie (G)**

### 4.2 The 3-Line Depth Chart (18 Roster Spots)
Each team carries **18 total player slots**, formatted as 3 full forward/defense lines and goalie tandems (6 starters + 12 bench):

| Line / Depth | Center (C) | Winger 1 (W) | Winger 2 (W) | Defense 1 (D) | Defense 2 (D) | Goalie (G) | Priority Status |
|---|---|---|---|---|---|---|---|
| **Line 1** | C1 | W1 | W2 | D1 | D2 | G1 | **Starting Lineup** |
| **Line 2** | C2 | W3 | W4 | D3 | D4 | G2 | Priority Backup 1 |
| **Line 3** | C3 | W5 | W6 | D5 | D6 | G3 | Priority Backup 2 |

### 4.3 AI Monte Carlo Roster & Coverage Math
In the 3-window weekly schedule (Sun–Tue, Wed–Thu, Fri–Sat), an average of 88.8% of NHL teams play in every fantasy window:
- **Full Roster Fill Rate**: 96.6% across all 78 regular season games (only 2.7 empty slots per team per year).
- **Skater Coverage (1 C, 2 W, 2 D)**: 99.2% full skater lineup fill rate with 3 lines of depth (3 C, 6 W, 6 D).
- **Goalie (1 starter, depth 3)**: Yields 97.4% netminder starting coverage across all windows.
- **Pecking Order Auto-Resolution**: At puck drop, the engine resolves the starting lineup without requiring daily manager intervention.

---

## 5. The 3-Game Weekly Schedule & Multi-Game Window Resolution

Hockey standardizes the regular season into **3 distinct game windows per week**, completely eliminating off-day roster voids while mirroring the real NHL cadence.

### 5.1 The 3-Window Weekly Schedule
Every fantasy week is partitioned into three fixed multi-day game windows:
- **Window 1 (Game 1)**: **Sunday – Monday – Tuesday** (14–23 real NHL games scheduled across the league).
- **Window 2 (Game 2)**: **Wednesday – Thursday** (11–18 real NHL games scheduled across the league).
- **Window 3 (Game 3)**: **Friday – Saturday** (13–20 real NHL games scheduled across the league).

Each window runs as a standalone fantasy game within the week's series, producing **3 games per week** and **78 games per season** across 26 regular-season weeks.

### 5.2 Multi-Game Window Resolution & Scoring Rules
In the NHL, teams play 3 to 4 games a week, meaning a player frequently has 1 real game in every window, and occasionally plays 2 real games within the same window (e.g. Edmonton plays both Sunday and Tuesday in Window 1).

1. **The One-Game-Per-Window Invariant**:
   A player can contribute **at most one game score** to any single fantasy window.
2. **Automated Default ("Best Matchup")**:
   When a player plays multiple real NHL games within the same window:
   - The counting game is the one against the opponent with **fewer NHL standings points**, measured at lock time. Fantasy points play no part in the choice, so the pick can never be made with hindsight.
   - **Lock**: the lock time is the `kickoff_at` of the player's **earliest game in that window**. After the lock the selection cannot change.
   - **Tiebreak**: equal standings points go to the earlier `kickoff_at`, then the lower game id, so the result never depends on input order.
   - **Standings snapshot**: the standings are fetched once per calendar day from the NHL API (`standings/{date}`, in `nhl_daily_sync`) before that day's first puck drop and stored insert-only in `nhl_standings_snapshots` (one row per team per date). The selection reads the snapshot dated the lock day, so it can be reproduced later; the choice records the lock time and each candidate's opponent points in the script provenance (`window.chosen[...].lock_at` / `.opponent_points`). A missing snapshot is an error, never a guess.
   - The interface highlights the counting matchup (`Better Matchup ★`).
   - **Zero User Maintenance**: Casual or inactive managers get the rule applied automatically, without having to log in or configure settings.
3. **Optional Surplus Game Flex (Reassignment)**:
   For managers who want active tactical control:
   - A manager can see which matchup is the better matchup and send **either matchup** to count for an open target window (**Window 2 or Window 3**).
   - Whichever matchup is not moved remains to count in the originating window.
   - *Example*: McDavid plays on Sunday vs SJS and Tuesday vs BOS (Window 1), but Edmonton is idle on Thursday (Window 2). The manager can send Tuesday's matchup vs BOS to Window 2 while keeping the favorable Sunday matchup vs SJS in Window 1!
   - *Lock*: a move must be submitted **before the same lock** (the kickoff of the player's earliest game in the source window); a move submitted at or after it is refused.
   - *Guardrail*: A surplus game can only be assigned to a window where the player does not already have an active game scheduled.
   - *Direction*: a surplus game moves **forward only** (Window 1 to 2 or 3, Window 2 to 3), so a game never rewrites a window that has already finished. One moved game per target window, and at least one game always stays where it was played.
   - *"Has a game"* means the player's real team plays in that window, whether or not he earned credit in it: a scheduled game occupies its window. A scheduled game he earned nothing in competes for Best Matchup like any other (only the opponent decides) and counts as 0.00 if chosen.
   - *Ties*: equal standings points go to the earlier `kickoff_at`, then the lower game id, so the result never depends on input order.
   - *A moved surplus game* keeps its real `kickoff_at`, so in its target window its periods sort first in the chronological division (section 6.1).
4. **Off-Day Point Banking**:
   On rare league-wide freeze days (e.g. Christmas Eve/Day, 3-day holiday breaks), any points from isolated games enter the off-day bank (`off_day_banked_credits`) and can be applied as a bench spark (capped at +1.50 pts).

---

## 6. Game Simulator: Real-Time Progression, Normalization & Randomness

### 6.1 Chronological Period Division and Real-Time Progressive Simulation
A matchup is one window (Sun–Tue, Wed–Thu or Fri–Sat). Each side has 6 starters resolved by the pecking order, so a full matchup has 12 counting games. The three fantasy periods are **not** a game-clock overlay of each player's own real periods. They are a chronological division of all the real periods in the matchup, computed per matchup:

1. Each counting game contributes **3 real periods** with nominal start times: P1 = `kickoff_at`, P2 = `kickoff_at` + 60 min, P3 = `kickoff_at` + 120 min.
2. The real periods of **both sides** are pooled (team affiliation is ignored) and sorted by nominal start ascending. Ties break by game id, then asset id (then period number), so the result never depends on input order.
3. N is the number of pooled periods. A full matchup has 36; N is always a multiple of 3 and an empty slot only lowers it (one empty slot: 33). The sorted list is cut into 3 consecutive chunks of N/3: these are fantasy P1, P2 and P3.
4. Each matchup has its own division and its own clock. Different matchups can be in different periods at the same moment.
5. A moved surplus game uses its real `kickoff_at`, so it sorts first in its target window.

*Example*: 6 starters kick off at 4:00 and 6 at 7:00 on the same day. Fantasy P1 = the 4:00 players' real P1 and P2; fantasy P2 = the 4:00 players' P3 plus the 7:00 players' P1; fantasy P3 = the 7:00 players' P2 and P3. If every starter kicks off together, real Pn is fantasy Pn.

**Overtime.** OT minutes and OT events belong to the player's real P3, in whichever fantasy period that P3 landed. Shootouts are excluded (the scoring contract; they are never ingested).

**Clock and event placement** (one shared function, `sports/hockey/simulator/division.py`; only nominal times are used, never live event timestamps):
- Real period progress at time t = clamp((t − nominal_start) / 60 min, 0, 1). Fantasy period k progress = the mean progress of the real periods in chunk k.
- Clock display: `Period k — MM:SS left`, remaining = 20:00 × (1 − progress).
- An event at offset s seconds into real period p has an estimated wall time of nominal_start(p) + (s / 1200) × 60 min. Its fantasy `game_second` = (k − 1) × 1200 + 1200 × progress_k(event wall time), kept inside 1–1199 within period k.
- OT events sit at the end of their fantasy period, in their relative order.
- Events with no real clock (inverted opponent errors, unattributed team goals, the tie-break goal) are drawn inside the fantasy periods from the seeded stream. The monotonic scoreboard validator (FS-025) still applies.

The division, with every real period's player, fantasy team, game, real period, nominal start, OT flag and points, is stored in the script provenance (`provenance.division`) and returned by `GET /simulator/games/{id}/final`, behind the same spoiler veil as the rest of the box score (section 9). *Not yet wired*: the live service still paces a published script over its real-time window (`window_seconds`); driving the live clock from the division's nominal times is a follow-up.

Simulated games do not execute as a single instantaneous batch calculation. Instead, games **progress in real time** throughout the game window:
- **Anytime Status Checking**: When a manager opens the app, the interface displays:
  - Current period and time remaining (e.g., `Period 2 — 14:18 left`).
  - Shots on goal and current normalized score.
  - Real-time on-ice event ticker (goals, power plays, big saves).
- **Post-Game Viewing**: Managers can watch the full climactic 2D animation highlights (1:15 runtime) at the end of the matchup or the following morning, or skip directly to the verified final box score.

### 6.2 Score Normalization
Raw fantasy matchup totals (e.g., 5.60 xG vs. 3.40 xG) are dynamically normalized to authentic, realistic NHL scoreboards:
- Raw points map to realistic hockey goal totals (e.g., `4 - 2`, `3 - 2`, `5 - 3`).
- The underlying performance margin and scoring leader are strictly preserved.

### 6.3 Simulation Randomness & The Error Model
To reflect real hockey reality—where an underdog can occasionally steal a game behind an unscreened wrister or a hot goalie—the simulator introduces bounded stochastic variance:
- **The Error Term**: A Gaussian noise factor $\epsilon \sim \mathcal{N}(0, \sigma^2)$ is applied to simulated scoring chances.
- **Underdog Upset Window**: An underdog within a 1-goal margin can occasionally claim a surprise `2 - 1` upset victory.
- **Season-Long Regression**: Because each series spans 5–10 games and the season spans ~77 games, single-game variance evens out, ensuring true goal differential and xG skill dictate playoff qualification.

### 6.4 Strict Monotonicity & Inversion
- In-simulation scoreboards strictly climb upward ($Score_{t+1} \ge Score_t$).
- Negative player outputs (goals allowed by goalies under GSAx, penalties taken) are inverted ($|-x|$) and credited to the opposing team as Power-Play Goals or Odd-Man Rushes.

---

## 7. Draft Day Experience

Drafts transition from static pick boards into dynamic, multimedia live events.

### 7.1 Draft Flexibility
- Supports both **Live Timed Drafts** (clock-based rounds) and **Slow / Asynchronous Drafts** (hours per pick, pausing overnight), allowing drafts to last as long as necessary.

### 7.2 The Live Interactive Draft Room
- **Announced Picks**: Picks are announced with audio cues and visual fanfare rather than silently populating the board.
- **AI Sports Commentators**: Generative AI reporters provide live draft analysis after every selection:
  - *Value praise*: *"Love this pick — Pastrnak gives them an elite Line 1 sniper with unmatched xG finishing!"*
  - *Reach critique*: *"Huge reach on a backup netminder in Round 6 — not sure what they're thinking with Swayman still on the board."*
- **Social Features**:
  - Integrated **Live Video/Audio Chat** directly inside the draft room.
  - **Shared Spotify Queue**: League members sync to a single shared playlist (playing arena anthems, goal horns, and walk-up tracks).

### 7.3 Weekly Redraft (DFS Salary Cap Mode)
- **Salary Cap Structure**: Available for hockey leagues seeking weekly variation without gambling/sportsbook mechanics.
- **Dynamic Cap Adjustments**: Team salary caps adjust based on the previous week's performance.
- **Private Group Matchups**: Managers build fresh weekly rosters and compete head-to-head against an opponent within their private division/group, rather than against thousands of anonymous entries.

---

## 8. Rivalries, Divisional Continuity & The Game Score

### 8.1 Divisional Continuity & Historical Records
- Leagues feature persistent divisions (e.g., 2 divisions of 6 teams) with fixed intra-division scheduling.
- The platform tracks all-time rivalry metrics:
  - Historical Series W/L record.
  - Lifetime Daily Game W/L record.
  - Cumulative Net Goal Differential.
  - Past playoff knockouts and championship head-to-heads.

### 8.2 "The Game Score" (0–100 Leverage Metric)
A proprietary playoff-implication algorithm assigns every upcoming matchup a **Game Score** from 0 to 100:
- **Calculation Factors**:
  $$\text{Game Score} = f(\text{Division Standings Margin}, \text{Playoff Cutoff Proximity}, \text{Rivalry Multiplier}, \text{Weeks Remaining})$$
- **Advance Notice**: Game Scores are published in the Schedule Viewer one week in advance.
- **Dynamic Heatmap Visualization**: Displayed in a colored gradient from **neutral black** (Game Score 0–30, routine mid-season game) to **bright warning red** (Game Score 85–100, must-win divisional playoff clash).

---

## 9. Schedule & Lineup Viewer UI Specification

The frontend Schedule and Lineup interface is structured into three dedicated functional zones:

```
+---------------------------------------------------------------------------------------------------------------+
|  ROSTER WINDOW TABS (TOP NAVIGATION)                                                                          |
|  [ Game 1: Sun 10/12 – Tue 10/14 vs BOS (GS: 72) ]   [ Game 2: Wed – Thu vs BOS (GS: 85) ]   [ Game 3: Fri – Sat (GS: 96) ] |
+---------------------------------------------------------------------------------------------------------------+
|  POSITION PECKING ORDER (4 LINES GRID)                       |  ACTIVE SIMULATION / MATCHUP PANEL             |
|                                                              |                                                |
|  LINE 1 (STARTERS)                                           |  PERIOD 2 — 14:22 REMAINING    SHOTS: 24 - 19  |
|  C : Connor McDavid  (EDM)  Tue vs BOS  ✦ 2 Games [⇄ Flex]   |  TORONTO MAPLE LEAFS           BOSTON BRUINS   |
|  W : Cole Caufield   (MTL)  Mon @ NYR                        |             [ 3 ]                   [ 2 ]      |
|  W : Dylan Guenther  (UTA)  Sun @ CHI                        |                                                |
|  D : Evan Bouchard   (EDM)  Tue vs BOS                       |  ON-ICE TICKER:                                |
|  D : Zach Werenski   (CBJ)  Tue vs PIT                       |  12:04 P2 - GOAL! McDavid one-timer (Assists:   |
|  G : Jakub Dobes     (MTL)  Mon @ NYR (Confirmed Start)      |             Bouchard)                          |
|                                                              |  08:15 P2 - POWER-PLAY SURGE! Werenski point-  |
|  LINE 2 (PRIORITY 2)                                         |             shot deflected home (+1.10 pts)    |
|  C : Nathan MacKinnon (COL) Mon @ DAL                        |                                                |
|  ...                                                         |  [ Watch Live Highlights ]    [ Box Score ]    |
+---------------------------------------------------------------------------------------------------------------+
```

### 9.1 UI Elements
1. **Roster Window Tabs (Top Bar)**:
   - Three persistent tabs for each weekly matchup:
     `[ Game 1: Sun–Tue ]  [ Game 2: Wed–Thu ]  [ Game 3: Fri–Sat ]`.
   - Each tab displays the date range, opponent team, and **The Game Score** in its dynamic heat color (`#FF1E27` for marquee matchups).
2. **Pecking Order 3-Line Grid**:
   - Displays the 3-line hierarchy (Line 1 Starters, Line 2–3 Backups).
   - Inactive/idle players in that 2–3 day window are dimmed (`Idle`), visually explaining why Line 2 or 3 stepped into the starting spot.
3. **Multi-Matchup Indicator & Flex Popover**:
   - When a player has 2 real NHL games in a window, their card displays:
     `✦ 2 Matchups: [Day vs Opp] (Better Matchup ★)`.
   - The dropdown or popover allows:
     - Selecting "Best Matchup (Auto)" default for this window.
     - Sending either matchup (Game A or Game B) to an open target window (Window 2 or 3).
     - Whichever matchup is kept remains active in the originating window.
4. **Main Matchup Canvas**:
   - Real-time progressive simulation view: period clock, live scoreboard, shots on goal, and on-ice narrative ticker.
5. **Box Score Division Table**:
   - Below the attribution box score, one table per fantasy period lists every real period assigned to it: player, fantasy team, real game (opponent and date), real period number, nominal start time, an `OT` flag, and the points earned in that real period.
   - Rows read chronologically; the points tie out to the attribution box score. It is shown only once the viewer has watched or skipped to the final, like the rest of the box score.
6. **Matchup Scoreboard Card & Period Spoiler Veil**:
   - Header indicates period status (e.g., `Period 1 in progress`, `Period 2 in progress`, `Final`).
   - Clean horizontal line layout: `<Road Team> <Road Goals> — <Home Goals> <Home Team>`.
   - During Period 1 (first third of game/players underway): scoreboard displays `0 - 0 in progress`.
   - Period 2+ and Final: scores remain veiled (`? - ?`) until a competing team owner watches the highlights or skips.
   - Once a team owner watches or skips, the completed period score is unlocked and visible to everyone in the league.
   - Other managers can watch highlights at any time and are shown a reminder when the owner has not seen the results.
   - Direct action buttons below: `[ Highlights ]`, `[ Box score ]` (anchored to `#boxscore`), and `[ Skip to Score ]`.

---

## 10. Database Schema Extensions

### 10.1 Series & Daily Matchups (`league_series`, `series_daily_games`)
```sql
CREATE TABLE league_series (
    id INTEGER PRIMARY KEY,
    league_id INTEGER REFERENCES leagues(id),
    team_a_id INTEGER REFERENCES teams(id),
    team_b_id INTEGER REFERENCES teams(id),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    total_days INTEGER NOT NULL DEFAULT 7,
    team_a_wins INTEGER DEFAULT 0,
    team_b_wins INTEGER DEFAULT 0,
    status VARCHAR(20) DEFAULT 'SCHEDULED' -- 'SCHEDULED', 'IN_PROGRESS', 'FINAL'
);

CREATE TABLE series_daily_games (
    id INTEGER PRIMARY KEY,
    series_id INTEGER REFERENCES league_series(id),
    window_index INTEGER NOT NULL, -- 1 (Sun-Tue), 2 (Wed-Thu), 3 (Fri-Sat)
    calendar_date_start DATE NOT NULL,
    calendar_date_end DATE NOT NULL,
    game_score_rating INTEGER NOT NULL DEFAULT 50, -- 0-100 leverage scale
    home_team_id INTEGER REFERENCES teams(id),
    away_team_id INTEGER REFERENCES teams(id),
    home_score INTEGER DEFAULT 0,
    away_score INTEGER DEFAULT 0,
    current_period INTEGER DEFAULT 0,
    seconds_remaining INTEGER DEFAULT 1200,
    status VARCHAR(20) DEFAULT 'SCHEDULED' -- 'SCHEDULED', 'LIVE_SIMULATING', 'FINAL'
);
```

### 10.2 Multi-Game Window Assignments (`player_window_assignments`)
```sql
CREATE TABLE player_window_assignments (
    id INTEGER PRIMARY KEY,
    series_id INTEGER REFERENCES league_series(id),
    team_id INTEGER REFERENCES teams(id),
    draftable_asset_id INTEGER REFERENCES draftable_assets(id),
    window_index INTEGER NOT NULL, -- 1, 2, or 3
    source_game_id INTEGER REFERENCES games(id),
    is_manual_override BOOLEAN DEFAULT FALSE,
    UNIQUE(series_id, draftable_asset_id, window_index)
);
```

### 10.3 Pecking Order & Off-Day Banking
```sql
CREATE TABLE lineup_pecking_orders (
    id INTEGER PRIMARY KEY,
    team_id INTEGER REFERENCES teams(id),
    slot_type VARCHAR(10) NOT NULL, -- 'C', 'W', 'D', 'G'
    priority_rank INTEGER NOT NULL,  -- 1..4 for C/G; 1..8 for W/D
    draftable_asset_id INTEGER REFERENCES draftable_assets(id),
    UNIQUE(team_id, slot_type, priority_rank)
);

CREATE TABLE off_day_banked_credits (
    id INTEGER PRIMARY KEY,
    league_id INTEGER REFERENCES leagues(id),
    team_id INTEGER REFERENCES teams(id),
    draftable_asset_id INTEGER REFERENCES draftable_assets(id),
    source_calendar_date DATE NOT NULL,
    credit_points FLOAT NOT NULL,
    assigned_daily_game_id INTEGER REFERENCES series_daily_games(id),
    status VARCHAR(20) DEFAULT 'AVAILABLE', -- 'AVAILABLE', 'ASSIGNED', 'EXPIRED'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 11. Operational Boundaries and Division of Labor

Per repository guidelines (`AGENTS.md`) and platform mandates:

| Component | Target Location | Responsible Agent | Rationale |
|---|---|---|---|
| **Platform Tables & Migrations** | `src/fantasy_sports/platform/db/models/`, `migrations/` | **Codex / Antigravity** | Series models, daily games, pecking order schemas. |
| **Schedule Engine & Calendar APIs** | `src/fantasy_sports/platform/schedules/`, `src/fantasy_sports/api/` | **Codex / Antigravity** | Series generation, Game Score rating algorithm, banking endpoints. |
| **Frontend UI & 2D Simulator** | `web/static/`, `web/static/simulator/` | **Codex / Antigravity** | 7-day tab header, pecking order column, live period simulator canvas. |
| **Draft Room Multimedia & AI Reporter** | `src/fantasy_sports/api/drafts/`, `web/static/draft-board.html` | **Codex / Antigravity** | Spotify queue sync, AI commentator stream, video chat bridge. |
| **Hockey Simulator Adapter & Scoring Tie-Outs** | `src/fantasy_sports/sports/hockey/simulator/` | **Claude Code** | Implements `SportSimulatorAdapter`, xG normalization, noise error modeling, and scoring attribution. |
