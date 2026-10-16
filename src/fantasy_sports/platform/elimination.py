"""Which franchises are mathematically eliminated from championship contention (CFG-06 integration boundary).

No elimination calculation exists in the platform yet, so this module only exposes the authoritative flag the trade rules
read (teams.eliminated_season). Whatever computes elimination later - from standings, the remaining schedule and the
playoff field - calls mark_eliminated(); nothing a client sends ever reaches it. A low playoff probability is not
elimination, so a team is active until that calculation says otherwise.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import League, Team


def is_eliminated(league: League, team: Team) -> bool:
    """True when the franchise was eliminated in the season now being played (offseason: nobody is eliminated)."""
    return not league.offseason and team.eliminated_season == league.season_number


def mark_eliminated(db: Session, league: League, team: Team) -> None:
    """Records that the team is mathematically eliminated from this season's championship. Flushes."""
    team.eliminated_season = league.season_number
    db.flush()


def clear_elimination(db: Session, team: Team) -> None:
    team.eliminated_season = None
    db.flush()
