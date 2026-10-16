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
    Conference,
    RealTeam,
    Player,
    DefenseGroup,
    DraftableAsset,
    RosterSlotDefinition,
    Draft,
    DraftPick,
    AuctionDraft,
    AuctionTeamBudget,
    AuctionRound,
    AuctionBid,
    RosterEntry,
    Week,
    LineupEntry,
    Game,
    WeeklyMatchup,
    LeagueSeries,
    SeriesDailyGame,
    TeamPeckingOrder,
    WeeklyTeamScore,
    SimulatorGame,
    SimulatorEvent,
    SimulatorReveal,
    BankedCredit,
    WeeklyRedraftSettings,
    WeeklyContest,
    WeeklyContestPairing,
    WeeklyPlayerSalary,
    WeeklyContestEntry,
    WeeklyContestSlot,
)
from fantasy_sports.sports.football.models import (  # noqa: F401
    Play,
    PlayCredit,
    WeeklyPlayerScore,
    WeeklyRealTeamEpaTotal,
)
from fantasy_sports.sports.hockey.models import (  # noqa: F401
    NhlStandingsSnapshot,
    PlayerGameToi,
    Shot,
    ShotCredit,
)
