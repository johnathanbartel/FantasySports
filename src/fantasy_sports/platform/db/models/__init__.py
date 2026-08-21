"""
Importing this module registers every model on Base.metadata - required
for Alembic autogenerate to see the full schema, and for
Base.metadata.create_all() in scripts/init_db.py.
"""

from fantasy_sports.platform.db.models.core import (  # noqa: F401
    User,
    Universe,
    League,
    LeagueUniverse,
    Team,
    RealTeam,
    Player,
    DefenseGroup,
    DraftableAsset,
    RosterSlotDefinition,
    Draft,
    DraftPick,
    RosterEntry,
    Week,
    LineupEntry,
    Game,
    WeeklyMatchup,
    WeeklyTeamScore,
)
from fantasy_sports.sports.football.models import (  # noqa: F401
    Play,
    PlayCredit,
    WeeklyPlayerScore,
    WeeklyRealTeamEpaTotal,
)
