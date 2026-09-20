"""
Program-size budget tiers for auction drafts. A team's program_size
(Team.program_size in db/models/core.py) sets its starting auction budget -
that's all this module wires up. See platform/team_tiers.py for the shared
tier list and platform/conferences/ for the conference-assignment side of
program_size.
"""

from fantasy_sports.platform.team_tiers import PROGRAM_SIZES  # noqa: F401

STARTING_BUDGET_BY_PROGRAM_SIZE = {
    "large": 300,
    "moderate": 200,
    "small": 100,
}

MINIMUM_BID = 1

DEFAULT_ROUND_SECONDS = 30
