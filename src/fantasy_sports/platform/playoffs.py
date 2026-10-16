"""Playoff teams and seeding for hockey leagues.

The commissioner sets how many teams make the playoffs (leagues.playoff_teams); until they do it is 33% of the league's teams,
rounded to the nearest whole team. Seeding is division winners plus wild cards: the division winners take the top seeds (in
rank order) and everyone else is seeded by rank, where rank is the standings order the league chose (points or win %). A
league with no divisions just seeds by rank. Nothing here plays the playoffs - this is who is in and in what order.
"""

from __future__ import annotations

DEFAULT_PLAYOFF_SHARE = 0.33


def default_playoff_teams(team_count: int) -> int:
    """33% of the teams, rounded to the nearest whole team (halves round up), and at least one."""
    return max(1, int(team_count * DEFAULT_PLAYOFF_SHARE + 0.5))


def effective_playoff_teams(league, team_count: int) -> int:
    n = league.playoff_teams if league.playoff_teams else default_playoff_teams(team_count)
    return max(1, min(n, team_count))


def seed_teams(ranked: list[int], division_of: dict[int, int | None], playoff_teams: int) -> list[dict]:
    """ranked = team ids best-first. Returns the playoff field in seed order: [{team_id, seed, division_winner}]."""
    winners: list[int] = []
    seen: set[int] = set()
    for tid in ranked:
        div = division_of.get(tid)
        if div is not None and div not in seen:
            seen.add(div)
            winners.append(tid)
    field = winners[:playoff_teams]
    field += [t for t in ranked if t not in field][:max(0, playoff_teams - len(field))]
    return [{"team_id": t, "seed": i, "division_winner": t in winners} for i, t in enumerate(field, start=1)]
