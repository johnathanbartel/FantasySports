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

## 4. Lineup Architecture: 6 Positions & The 4-Line Pecking Order

To eliminate daily lineup churn, hockey implements a **4-Line Pecking Order System**.

### 4.1 The 6-Position Starting Unit
Every daily game features exactly 6 active starters per team:
- **1 Center (C)**
- **2 Wingers (W)** (LW and RW eligible)
- **2 Defensemen (D)**
- **1 Goalie (G)**

### 4.2 The 4-Line Depth Chart (24 Roster Spots)
Each team carries **24 total player slots**, formatted as 4 full forward/defense lines and goalie tandems:

| Line / Depth | Center (C) | Winger 1 (W) | Winger 2 (W) | Defense 1 (D) | Defense 2 (D) | Goalie (G) | Priority Status |
|---|---|---|---|---|---|---|---|
| **Line 1** | C1 | W1 | W2 | D1 | D2 | G1 | **Starting Lineup** |
| **Line 2** | C2 | W3 | W4 | D3 | D4 | G2 | Priority Backup 1 |
| **Line 3** | C3 | W5 | W6 | D5 | D6 | G3 | Priority Backup 2 |
| **Line 4** | C4 | W7 | W8 | D7 | D8 | G4 | Priority Backup 3 |

### 4.3 AI Monte Carlo Roster & Coverage Math
To guarantee every roster spot is filled over the course of an 82-game season without daily manual edits, we simulated schedule distributions across 1,023 available NHL players:
- **Center (1 starter, depth 4)**: $95.5\%+$ full coverage on heavy game days.
- **Wingers (2 starters, pooled depth 8)**: Ranking 8 wingers where the top 2 active dress for Line 1 yields $86.7\% - 97.0\%$ coverage on $\ge 8$-game slates.
- **Defensemen (2 starters, pooled depth 8)**: Ranking 8 defensemen where the top 2 active dress for Line 1 yields $86.3\% - 97.0\%$ coverage on $\ge 8$-game slates.
- **Goalie (1 starter, depth 4)**: Covers 1A starters plus tandem backups; yields $87.8\% - 94.5\%$ starting netminder coverage.
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
   - The interface highlights which matchup is favorable (`Better Matchup ★`) based on opponent defensive rating and home/away status.
   - The scoring engine automatically awards the game with the **highest fantasy score** (`MAX(points)` based on xG attribution).
   - **Zero User Maintenance**: Casual or inactive managers are guaranteed their star player's best performance without having to log in or configure settings.
3. **Optional Surplus Game Flex (Reassignment)**:
   For managers who want active tactical control:
   - A manager can see which matchup is the better matchup and send **either matchup** to count for an open target window (**Window 2 or Window 3**).
   - Whichever matchup is not moved remains to count in the originating window.
   - *Example*: McDavid plays on Sunday vs SJS and Tuesday vs BOS (Window 1), but Edmonton is idle on Thursday (Window 2). The manager can send Tuesday's matchup vs BOS to Window 2 while keeping the favorable Sunday matchup vs SJS in Window 1!
   - *Guardrail*: A surplus game can only be assigned to a window where the player does not already have an active game scheduled.
   - *Direction*: a surplus game moves **forward only** (Window 1 to 2 or 3, Window 2 to 3), so a game never rewrites a window that has already finished. One moved game per target window, and at least one game always stays where it was played.
   - *"Has a game"* means the player's real team plays in that window, whether or not he earned credit in it: a scheduled game occupies its window. Such a game still never beats a game he earned credit in, so staying out of a game cannot be used to dodge a bad night (goalie GSAx).
   - *Ties*: equal scores go to the earlier game, then the lower game id, so the result never depends on input order.
4. **Off-Day Point Banking**:
   On rare league-wide freeze days (e.g. Christmas Eve/Day, 3-day holiday breaks), any points from isolated games enter the off-day bank (`off_day_banked_credits`) and can be applied as a bench spark (capped at +1.50 pts).

---

## 6. Game Simulator: Real-Time Progression, Normalization & Randomness

### 6.1 Real-Time Progressive Simulation
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
2. **Pecking Order 4-Line Grid**:
   - Displays the 4-line hierarchy (Line 1 Starters, Line 2–4 Backups).
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
