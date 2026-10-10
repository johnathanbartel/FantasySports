# NHL v1 Universal Simulator, Roster Pecking Order, and Scheduling Specification

## 1. Status and Architectural Scope

This document specifies the hockey (NHL) implementation within the multi-sport platform, adhering to the **Cross-Sport Game Simulator — Universal Platform Specification (v1.0.0)**.

Hockey operates under the platform's multi-sport split:
- **Core Platform Engine** (`src/fantasy_sports/platform`): League scheduling, calendar generation, roster slot definitions, drafts, user sessions, spoiler veil protection, and asynchronous finality tracking.
- **Sport-Specific Adapter** (`src/fantasy_sports/sports/hockey`): Shot ingestion (MoneyPuck/NHL API), xG credit attribution, goalie GSAx calculations, play-by-play animation scripting, and negative-point event inversion.
- **Frontend 2D Arcade Engine** (`web/static/simulator`): Canvas/SVG-based 2D ice rink renderer, dynamic monotonic scoreboard, and dual-path reveal UX.

---

## 2. Roster Architecture: The 4-Line "Pecking Order" System

Daily sports (hockey, basketball, baseball) impose high cognitive load and user fatigue when requiring daily manual lineup edits. To eliminate daily churn while preserving strategic depth, hockey implements a **4-Line Pecking Order System**.

### 2.1 The 6-Starter On-Ice Unit
Every simulated fantasy matchup features exactly 6 active starters per team, mirroring a standard NHL on-ice lineup:
- **1 Center (C)**
- **2 Wingers (W)** (LW/RW eligible)
- **2 Defensemen (D)**
- **1 Goalie (G)**

### 2.2 The 4-Line Depth Chart (24 Total Roster Spots)
Each fantasy roster contains **24 player slots**, organized as 4 complete forward/defense lines and goalie tandems:

| Line / Tier | Center (C) | Winger 1 (W) | Winger 2 (W) | Defense 1 (D) | Defense 2 (D) | Goalie (G) | Priority Status |
|---|---|---|---|---|---|---|---|
| **Line 1** | C1 | W1 | W2 | D1 | D2 | G1 | **Primary Starters** |
| **Line 2** | C2 | W3 | W4 | D3 | D4 | G2 | Priority Backup 1 |
| **Line 3** | C3 | W5 | W6 | D5 | D6 | G3 | Priority Backup 2 |
| **Line 4** | C4 | W7 | W8 | D7 | D8 | G4 | Priority Backup 3 |

### 2.3 Draft Sizing and Talent Pool Depth
- **Draft Length**: 24 rounds (snake or timed auction draft).
- **12-Team League Draft Pool**: $12 \times 24 = 288$ total drafted players.
- **Talent Pool Health**: Across 32 NHL franchises (~736 active roster skaters/goalies; 1,023 total draftable assets in the platform database), drafting 288 players captures:
  - 48 Centers (top 16% of available NHL Centers).
  - 96 Wingers (top 31% of available NHL Wingers; solid top-9 forwards).
  - 96 Defensemen (top 30% of available NHL Defensemen; solid top-4 pairings).
  - 48 Goalies (all 32 NHL starters plus 16 high-volume 1B/backup tandems).

### 2.4 Pecking Order Resolution Rules
On any scheduled Matchup Day, the platform automatically resolves the starting 6-man lineup at puck drop:
1. **Center**: The system selects the highest-priority Center whose real NHL team plays that day (`C1 -> C2 -> C3 -> C4`).
2. **Wingers (Pooled)**: The manager ranks their 8 rostered wingers (W1 through W8). The top 2 active wingers with games that day are dressed as Starters.
3. **Defensemen (Pooled)**: The manager ranks their 8 rostered defensemen (D1 through D8). The top 2 active defensemen with games that day are dressed as Starters.
4. **Goalie**: The system checks G1 through G4. A goalie is considered active only if their real team is scheduled AND the goalie is confirmed as the starting netminder.
5. **Zero Maintenance**: Managers can check in whenever they want to adjust priority ranks; no daily roster locking or bench-swapping is required.

---

## 3. Predictable Matchup Calendar & The League Off-Day Model

Because the NHL schedule is unevenly distributed across days of the week, running head-to-head matchups on light days produces incomplete rosters and user frustration.

### 3.1 The $\ge 7$ Games Schedule Threshold
- **Matchup Day**: Any calendar date with **$\ge 7$ NHL games** (~14 to 28 NHL teams active) is an official **League Matchup Day**.
- **Off Day**: Any calendar date with **$< 7$ NHL games** (typically 1 to 5 games, or holiday freezes) is an official **League Off Day**.

### 3.2 Seasonal Matchup Cadence
- Across the 185-day NHL regular season, exactly **~77 calendar days** feature $\ge 7$ games (overwhelmingly Tuesdays, Thursdays, and Saturdays).
- In a 26-week fantasy regular season, this yields an average of **3.0 Matchup Days per week** ($77 / 26 \approx 3.0$).
- Every fantasy team plays a predictable **~75 to 80-game regular season**, exactly matching the real NHL's 82-game team rhythm.
- **Calendar Pre-Lock**: At league creation, the platform computes and commits the season calendar. Every date is explicitly marked as `MATCHUP_DAY` or `OFF_DAY`, giving managers complete visibility weeks in advance.

---

## 4. Off-Day Point Banking & Smart Reassignment System

On an official `OFF_DAY`, head-to-head simulations do not run. However, real NHL games still occur, and rostered players earn fantasy points.

### 4.1 The Banking Mechanic
- Any fantasy point earned by a rostered player on an `OFF_DAY` is captured in an off-day point reservoir (`off_day_banked_credits`).
- Active managers can assign these banked points to any upcoming Matchup Day within a rolling **7-day window**.

### 4.2 The Smart Reassignment Engine
To avoid decision paralysis, the platform presents a single-tap recommendation:
1. **Point Detection**: When an off-day player records fantasy output (e.g., Connor McDavid posts 1.35 pts on a Wednesday Off Day), an alert is generated:
   > *"Connor McDavid earned **1.35 pts** on today's Off Day. Bank these points to an upcoming matchup!"*
2. **Algorithmic Recommendation**: The engine evaluates the manager's next 3 scheduled Matchup Days, factoring in:
   - Projected matchup point margin (prioritizes tight games).
   - Opponent team strength / power rating.
   - Win probability swing ($+\Delta P(\text{Win})$).
   > *"Recommended: Bank to **Thursday vs. Boston** (+1.35 pts swings win probability from 48% to 62%)."*
3. **One-Tap Confirmation**: The manager can tap `Accept Recommendation` or select an alternative game from the 7-day calendar.

### 4.3 Passive Manager Safeguard & Anti-Stacking Guardrails
- **Passive Auto-Bank**: If an unassigned off-day credit remains unclaimed after 24 hours, the system automatically banks it to the manager's immediate next Matchup Day. No points are lost.
- **Matchup Banking Cap**: To preserve competitive parity, a team may apply at most **1 banked player credit (or a maximum of +1.50 points)** to any single Matchup Day. Point dumping across multiple off-days is prevented.

---

## 5. Universal Simulator Integration (3-Period Engine)

The simulation engine converts asynchronous NHL events into an arcade-style, 3-act cinematic replay.

### 5.1 Multi-Sport Matrix Compliance

| Parameter | Specification Value | Rationale |
|---|---|---|
| **Sim Segments** | **3 Periods** | Mirrors standard hockey periods; 3-act narrative rhythm. |
| **Starters per Team** | **6 Starters** | 1 C, 2 W, 2 D, 1 G. |
| **Total Matchup Units** | **36 player-periods** | $2 \text{ teams} \times 6 \text{ starters} \times 3 \text{ periods} = 36$. |
| **Units per Sim Segment** | **12 units (33.3%)** | Evenly distributed across Period 1, 2, and 3. |
| **Visual Pacing** | **45–60 sec / period** | Total climactic highlight replay runs 1:15 to 1:30. |
| **Universal Queue Priority** | `(Puck Drop, Real Period, Asset ID)` | Chronologically sliced starter actions. |

### 5.2 Strict Monotonicity & The Forward Turnover Rule
- **The Invariant**: Scores never roll backward ($Score_{t+1} \ge Score_t$). Historical watched segments are permanently immutable.
- **Negative Play Inversion**:
  - In our xG scoring pipeline (`src/fantasy_sports/sports/hockey/pipeline.py`), goalies accumulate Goals Saved Above Expected ($\text{GSAx} = \text{xG} - \text{goal}$). On every goal conceded, the goalie receives a negative score ($xG - 1.0 < 0$).
  - Any negative credit output from a starter in a given segment is inverted ($|-x|$) and credited to the opposing team's scoreboard.
  - **Narrative Thematic Mapping**:
    - *Real Negative Play*: Goalie Goal Conceded / Penalty Taken / High-Danger Turnover.
    - *Inverted Opponent Sim Event*: Opponent Power-Play Goal / Odd-Man Rush Snipe.
  - **Net Margin Equality**:
    $$\text{Net Margin} = Score_A - (Score_B + X) = (Score_A - X) - Score_B$$
    The competitive margin matches real fantasy math, but the arcade scoreboard only climbs upward.

### 5.3 Banked Points in the Simulation Narrative
When a team applies an off-day banked credit:
- The banked points enter the simulation in **Period 2 or Period 3** as an explosive momentum event.
- Visual representation:
  - Triggered as a **"Line 4 Energy Shift"** or **"Special Teams Power-Play Snipe"**.
  - Commentary banner: *"Bench spark! Connor McDavid off-day reserve surge powers a top-shelf one-timer (+1.35 pts)!"*

### 5.4 Home Ice Advantage & Overtime
- **Home Ice Advantage**:
  - Regular Season: **+0.5 goals / pts** awarded to the home team ("Last-change line matchups, hostile horn energy").
  - Playoffs: **0.0 pts** (Neutral Rink Championship Rules).
- **Overtime Handling**:
  - Regular season games that end regulation in a fantasy tie resolve via standard tiebreakers (higher real skater xG).
  - In playoff matchups, if scores are tied after 3 Periods, the arcade engine runs a sudden-death 3-on-3 OT sim sequence.

---

## 6. Universal Spoiler-Proof Reveal Ceremony

To maintain tension for asynchronous viewers:
1. **Masked by Default**: Matchup scores and player box scores are shielded behind a `[ ??? ]` veil once `SIM_READY`.
2. **Path A (Watch Climactic Highlights)**: Renders the 3-period 2D play-by-play animation with live scoreboard counting, culminating in final horn and goal celebration effects.
3. **Path B (Skip to Box Score)**: Instantly lifts the veil to show the final score, period splits, and player attribution breakdown.
4. **Independent Revealing**: Manager A watching the simulation does not unmask the outcome for Manager B.

---

## 7. Database Schema Extensions

Platform tables remain sport-agnostic, extended with hockey-compatible models:

### 7.1 Roster Slot Definitions (`roster_slot_definitions`)
Seeded via `src/fantasy_sports/platform/hockey_reference.py`:
- `slot_code="C"`, `eligible_positions=["C"]`, `slot_count=1`, `is_starter=True`
- `slot_code="W"`, `eligible_positions=["W", "LW", "RW"]`, `slot_count=2`, `is_starter=True`
- `slot_code="D"`, `eligible_positions=["D"]`, `slot_count=2`, `is_starter=True`
- `slot_code="G"`, `eligible_positions=["G"]`, `slot_count=1`, `is_starter=True`
- `slot_code="BENCH"`, `eligible_positions=["C", "W", "LW", "RW", "D", "G"]`, `slot_count=18`, `is_starter=False`
*(Total roster slots: $1 + 2 + 2 + 1 + 18 = 24$ slots).*

### 7.2 Calendar and Matchup Days (`league_matchup_days`)
```sql
CREATE TABLE league_matchup_days (
    id INTEGER PRIMARY KEY,
    league_id INTEGER REFERENCES leagues(id),
    calendar_date DATE NOT NULL,
    day_type VARCHAR(20) NOT NULL, -- 'MATCHUP_DAY' or 'OFF_DAY'
    nhl_game_count INTEGER NOT NULL,
    status VARCHAR(20) DEFAULT 'SCHEDULED', -- 'SCHEDULED', 'IN_PROGRESS', 'FINAL'
    UNIQUE(league_id, calendar_date)
);
```

### 7.3 Off-Day Banked Credits (`off_day_banked_credits`)
```sql
CREATE TABLE off_day_banked_credits (
    id INTEGER PRIMARY KEY,
    league_id INTEGER REFERENCES leagues(id),
    team_id INTEGER REFERENCES teams(id),
    draftable_asset_id INTEGER REFERENCES draftable_assets(id),
    source_calendar_date DATE NOT NULL,
    credit_points FLOAT NOT NULL,
    assigned_matchup_id INTEGER REFERENCES weekly_matchups(id),
    status VARCHAR(20) DEFAULT 'AVAILABLE', -- 'AVAILABLE', 'ASSIGNED', 'EXPIRED'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 7.4 Lineup Pecking Orders (`lineup_pecking_orders`)
```sql
CREATE TABLE lineup_pecking_orders (
    id INTEGER PRIMARY KEY,
    team_id INTEGER REFERENCES teams(id),
    slot_type VARCHAR(10) NOT NULL, -- 'C', 'W', 'D', 'G'
    priority_rank INTEGER NOT NULL,  -- 1, 2, 3, 4 (or 1..8 for W/D)
    draftable_asset_id INTEGER REFERENCES draftable_assets(id),
    UNIQUE(team_id, slot_type, priority_rank)
);
```

---

## 8. Operational Boundaries and Division of Labor

Per repository guidelines (`AGENTS.md`) and the Universal Platform Specification:

| Component | Target Location | Responsible Agent | Rationale |
|---|---|---|---|
| **Platform Tables & Migrations** | `src/fantasy_sports/platform/db/models/`, `migrations/` | **Codex / Antigravity** | Sport-agnostic schema extensions and Alembic revisions. |
| **Pecking Order Resolution & APIs** | `src/fantasy_sports/platform/`, `src/fantasy_sports/api/routers/` | **Codex / Antigravity** | Lineup resolution, schedule routes, banking endpoints. |
| **Frontend 2D Arcade Engine** | `web/static/simulator/` | **Codex / Antigravity** | Ice rink canvas renderer, scoreboard animation, reveal UX. |
| **Hockey Simulator Adapter & Scoring Tie-Outs** | `src/fantasy_sports/sports/hockey/simulator/` | **Claude Code** | Implements `SportSimulatorAdapter`, xG/GSAx inversion, and scoring tie-outs (protected by scoring mandate). |
