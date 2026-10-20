"""Imported scoring (FS-037): the ONE exception to "the platform's only points formula is the realistic model".

A league brought over from another platform keeps its existing box-score formula, copied once at import and frozen.
The formula is a flat list of {stat, points}; double- and triple-doubles are derived from the counting stats.  Nothing
here updates or deletes a row: create_formula is the only writer, it runs while the league is created, and it refuses a
league that already has items.  Changing a formula later is an operator-only data fix.  No API route reads scoring_system
from a request, and create-league refuses 'imported' (api/routers/leagues.py).

Leagues on this formula are not on the realistic scale, so scale-dependent features skip them (is_imported).

Stat-line input shape (no box-score ingestion exists yet): a dict of canonical stat code -> number, e.g.
    {"PTS": 30, "3PM": 4, "REB": 10, "AST": 5, "STL": 2, "BLK": 1, "TO": 3}
Missing stats count as 0.  DD/TD need not be supplied; they are derived (a supplied "DD"/"TD" is ignored).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import ImportedScoringItem, League

SCORING_REALISTIC = "realistic"
SCORING_IMPORTED = "imported"

# ESPN fantasy basketball stat id -> canonical code.  Only ids confirmed from a real league's scoringItems are listed
# (league 395243481, mSettings, 2026-10-19); anything else is refused rather than guessed.  Extend from observed data.
ESPN_BASKETBALL_STATS = {
    0: "PTS",
    1: "BLK",
    2: "STL",
    3: "AST",
    6: "REB",
    11: "TO",
    17: "3PM",
    37: "DD",
    38: "TD",
}

DOUBLE_DOUBLE_STATS = ("PTS", "REB", "AST", "STL", "BLK")

# UNCONFIRMED against ESPN's own documentation (no external calls were allowed).  True means a triple-double scores both
# the TD and the DD bonus: a triple-double is always also a double-double by the NBA definition, and ESPN lists DD and TD
# as separate scoring categories.  Flip to False if ESPN is shown to award only the TD.
TRIPLE_DOUBLE_ALSO_COUNTS_DOUBLE = True


class ImportedScoringError(ValueError):
    pass


def is_imported(league: League) -> bool:
    return league.scoring_system == SCORING_IMPORTED


def create_formula(db: Session, league: League, items: list[dict], source: str = "espn") -> list[ImportedScoringItem]:
    """Freezes `items` ([{"stat_id": 17, "points": 0.5}, ...], the ESPN scoringItems) as the league's formula and flags
    the league 'imported'.  Raises for an unknown stat id, a repeated stat, an empty list, a non-ESPN source or a league
    that already has a formula (or is already imported)."""
    if source != "espn":
        raise ImportedScoringError(f"Unsupported source platform '{source}'")
    if not items:
        raise ImportedScoringError("An imported formula needs at least one stat")
    if db.query(ImportedScoringItem).filter_by(league_id=league.id).first() is not None or is_imported(league):
        raise ImportedScoringError("This league already has an imported scoring formula; it is frozen")
    rows, seen = [], set()
    for item in items:
        stat_id = item["stat_id"]
        code = ESPN_BASKETBALL_STATS.get(stat_id)
        if code is None:
            raise ImportedScoringError(f"Unknown ESPN basketball stat id {stat_id}; add it to ESPN_BASKETBALL_STATS")
        if code in seen:
            raise ImportedScoringError(f"Stat {code} appears twice in the formula")
        seen.add(code)
        rows.append(ImportedScoringItem(league_id=league.id, source_platform=source, source_stat_id=stat_id,
                                        stat_code=code, points=item["points"]))
    league.scoring_system = SCORING_IMPORTED
    db.add_all(rows)
    db.flush()
    return rows


def formula(db: Session, league_id: int) -> dict[str, float]:
    """stat code -> points per unit, empty for a league without an imported formula."""
    return {r.stat_code: float(r.points) for r in
            db.query(ImportedScoringItem).filter_by(league_id=league_id).order_by(ImportedScoringItem.id)}


def derived_stats(stat_line: dict) -> dict[str, float]:
    """The line plus its DD and TD: a double-double is 10+ in two of PTS/REB/AST/STL/BLK, a triple-double in three."""
    tens = sum(1 for k in DOUBLE_DOUBLE_STATS if stat_line.get(k, 0) >= 10)
    out = {k: float(v) for k, v in stat_line.items() if k not in ("DD", "TD")}
    out["TD"] = 1.0 if tens >= 3 else 0.0
    out["DD"] = 1.0 if tens >= 2 and (TRIPLE_DOUBLE_ALSO_COUNTS_DOUBLE or tens < 3) else 0.0
    return out


def score_stat_line(formula_: dict[str, float], stat_line: dict) -> float:
    """One player-game's fantasy points: the frozen weights applied to his (derived) stats."""
    stats = derived_stats(stat_line)
    return float(sum(points * stats.get(code, 0.0) for code, points in formula_.items()))


def week_stat_lines(db: Session, team_id: int, week_id: int) -> list[dict]:
    """The stat lines of the team's starters for the week.  NBA box scores are not ingested yet, so this returns none and
    a team scores 0.  Replace this when box-score ingestion exists (docs/last_minute_league_gap_audit.md)."""
    return []


def team_week_score(db: Session, team_id: int, week_id: int, league_id: int) -> float:
    f = formula(db, league_id)
    return sum(score_stat_line(f, line) for line in week_stat_lines(db, team_id, week_id))
