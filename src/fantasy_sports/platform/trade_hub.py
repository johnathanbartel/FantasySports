"""The Trade Hub: every team's trading plan, and who is mathematically eliminated.

A manager picks Win Now or Rebuilding; everyone starts Win Now. An AI team has no choice to make: it is Win Now until it is
mathematically eliminated from the playoffs, and Rebuilding from then on.

Elimination here is read-only: it asks the elimination proof (platform/elimination.py) and writes nothing. The proof is exact
and conservative - a team is out only when nothing can save it, never because it merely looks unlikely. The same proof writes
teams.eliminated_season as games finish, which is what switches on the CFG-06 trade restrictions.

Only hockey leagues have a standings table and a remaining schedule to prove elimination from today, so in any other league
nobody is eliminated and every AI team stays Win Now until another sport gets an adapter in platform/elimination.py.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fantasy_sports.platform import elimination
from fantasy_sports.platform.db.models.core import League, Team

WIN_NOW = "win_now"
REBUILDING = "rebuilding"
PLANS = (WIN_NOW, REBUILDING)


def eliminated_team_ids(db: Session, league: League) -> set[int]:
    """The franchises that are mathematically out of the playoffs now (platform/elimination.py: an exact proof over every way the
    remaining games can finish). Empty in the offseason, before a schedule exists, and in a sport without a standings table."""
    return elimination.eliminated_team_ids(db, league)


def plan_of(team: Team, eliminated: set[int]) -> str:
    """The plan the league sees for a team: an AI team's follows its elimination, a manager's is their own choice."""
    if team.is_ai_owned:
        return REBUILDING if team.id in eliminated else WIN_NOW
    return team.trade_plan if team.trade_plan in PLANS else WIN_NOW
