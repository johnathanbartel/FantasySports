"""
Shared program-size tier definitions. Team.program_size ("large"/"moderate"/
"small" - see db/models/core.py) is the one tier a team belongs to; the
auction engine's starting budgets (platform/auction/constants.py) and the
conference system (platform/conferences/) both key off the same three
values, so the tier list itself lives here instead of being duplicated.

Season goals are informational only for now - a fixed per-tier label shown
on a team, not fed into scoring. The commissioner's stated intent is that in
a multi-season league, reaching (or missing) a goal could raise or lower a
team's budget or tier the following season - that's explicit future work,
not implemented here (there's no season-rollover concept in the schema at
all yet).
"""

PROGRAM_SIZES = ("large", "moderate", "small")

SEASON_GOAL_BY_PROGRAM_SIZE = {
    "large": "Win the championship",
    "moderate": "Make the playoffs",
    "small": "Win the conference",
}
