"""
Season shape and season money, as decided by the commissioner (2026-09-20).

  * The season is 10 weeks. A team's weekly budget (program size: $300 / $200 /
    $100, see auction/constants.py) accrues each of those weeks.
  * Then 3 playoff weeks. Free agency is LOCKED during the playoffs, so no money
    is spent (or accrues) there.
  * Season Budget (Total) = weekly budget x 10 + the team's Recruiting Budget
    (Total) (weekly budget x the number of recruiting weeks).
  * Roster Cost = what the team paid for the players on its roster.
  * The season Weekly Budget = (Season Budget - Roster Cost) / the weeks in the
    season.

Nothing else in the platform runs a season yet (no free agency, no weekly
matchups over a full season) - this module just pins the numbers so the UI and
the future free-agency work read them from one place.
"""

SEASON_WEEKS = 10
PLAYOFF_WEEKS = 3
FREE_AGENCY_OPEN_IN_PLAYOFFS = False


# Weeks 1-9 are the regular season (a premade round-robin that teams can renegotiate in the preseason
# schedule stage); week 10 is conference championship week.
REGULAR_SEASON_WEEKS = 9
CHAMPIONSHIP_WEEK = 10


def season_budget_total(weekly_budget: int, recruiting_budget_total: int) -> int:
    """weekly budget x the season's weeks, plus the whole recruiting budget."""
    return weekly_budget * SEASON_WEEKS + recruiting_budget_total


def season_weekly_budget(season_budget: int, roster_cost: int) -> int:
    """What a team can spend each week of the season once its roster is paid for."""
    return round((season_budget - roster_cost) / SEASON_WEEKS)
