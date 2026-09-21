"""
ESPN "standard" fantasy scoring applied to a player's FULL-SEASON stat line
(CFBD's /stats/player/season). Used to show last season's points on the
recruiting board so managers can find better players - it is NOT the
platform's own weekly EPA scoring (see pipeline.py), which is what actually
scores matchups.

ESPN standard values used:
    passing   1 pt per 25 yds (0.04/yd), 4 per TD, -2 per INT
    rushing   1 pt per 10 yds (0.1/yd),  6 per TD
    receiving 1 pt per 10 yds (0.1/yd),  6 per TD, no points per reception
    fumbles lost  -2
    kick/punt return TDs  6
    kicking   1 per XP, 3 per FG

Known approximations (season totals can't express them):
  * ESPN pays FGs by distance (3 for 0-39, 4 for 40-49, 5 for 50+); CFBD's
    season line has only makes and the longest, so every FG is 3.
  * Two-point conversions (2 pts) and missed-kick penalties aren't in the
    season line and aren't counted.
Defense groups (DL/LB/DB) are not scored here - ESPN standard has no
position-group scoring, so they show no last-season points.
"""

from __future__ import annotations

SCORING_SYSTEM = "espn_standard"

# (category, stat_type) -> points per unit
POINTS_PER_UNIT: dict[tuple[str, str], float] = {
    ("passing", "YDS"): 0.04,
    ("passing", "TD"): 4.0,
    ("passing", "INT"): -2.0,
    ("rushing", "YDS"): 0.1,
    ("rushing", "TD"): 6.0,
    ("receiving", "YDS"): 0.1,
    ("receiving", "TD"): 6.0,
    ("fumbles", "LOST"): -2.0,
    ("kickReturns", "TD"): 6.0,
    ("puntReturns", "TD"): 6.0,
    ("kicking", "XPM"): 1.0,
    ("kicking", "FGM"): 3.0,
}


def espn_standard_points(stats: dict[tuple[str, str], float]) -> tuple[float, dict[str, float]]:
    """stats: {(category, stat_type): season total}. Returns (points, the
    per-stat point contributions) so a total can be traced back line by line."""
    breakdown = {
        f"{cat} {stat}": round(stats[(cat, stat)] * per_unit, 2)
        for (cat, stat), per_unit in POINTS_PER_UNIT.items()
        if stats.get((cat, stat))
    }
    return round(sum(breakdown.values()), 1), breakdown
