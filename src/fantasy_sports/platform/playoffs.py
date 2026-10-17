"""Playoff teams and seeding.

The commissioner sets how many teams make the playoffs (leagues.playoff_teams); until they do it is 33% of the league's teams,
rounded to the nearest whole team. How the field is filled and seeded is a PlayoffRules, set per league:
  * division_winners_qualify - every division winner is in the field (up to its size); the other spots are wild cards, taken
    by rank. Off: the field is simply the best teams by rank.
  * division_winners_seeded_first - the division winners take the top seeds (in rank order) and the wild cards follow. Off:
    everyone is seeded by rank.
  * byes - the top seeds skip the first round.
Rank is the standings order the league chose (points or win %, then its tiebreakers). A league with no divisions just seeds by
rank. Nothing here plays the playoffs - this is who is in, in what order and who has a bye.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PLAYOFF_SHARE = 0.33


@dataclass(frozen=True)
class PlayoffRules:
    teams: int                              # size of the field
    byes: int = 0                           # top seeds that skip the first round
    division_winners_qualify: bool = True
    division_winners_seeded_first: bool = True


def default_playoff_teams(team_count: int) -> int:
    """33% of the teams, rounded to the nearest whole team (halves round up), and at least one."""
    return max(1, int(team_count * DEFAULT_PLAYOFF_SHARE + 0.5))


def effective_playoff_teams(league, team_count: int) -> int:
    n = league.playoff_teams if league.playoff_teams else default_playoff_teams(team_count)
    return max(1, min(n, team_count))


def rules_for(league, team_count: int) -> PlayoffRules:
    teams = effective_playoff_teams(league, team_count)
    return PlayoffRules(teams, max(0, min(getattr(league, "playoff_byes", 0) or 0, teams)),
                        bool(getattr(league, "division_winners_qualify", True)),
                        bool(getattr(league, "division_winners_seeded_first", True)))


def seed_teams(ranked: list[int], division_of: dict[int, int | None], playoff_teams: int | PlayoffRules) -> list[dict]:
    """ranked = team ids best-first. Returns the playoff field in seed order: [{team_id, seed, division_winner, bye}]."""
    rules = playoff_teams if isinstance(playoff_teams, PlayoffRules) else PlayoffRules(playoff_teams)
    winners: list[int] = []
    seen: set[int] = set()
    for tid in ranked:
        div = division_of.get(tid)
        if div is not None and div not in seen:
            seen.add(div)
            winners.append(tid)
    if rules.division_winners_qualify:
        field = winners[:rules.teams]
        field += [t for t in ranked if t not in field][:max(0, rules.teams - len(field))]
        if not rules.division_winners_seeded_first:
            members = set(field)
            field = [t for t in ranked if t in members]
    else:
        field = ranked[:rules.teams]
    return [{"team_id": t, "seed": i, "division_winner": t in winners, "bye": i <= rules.byes}
            for i, t in enumerate(field, start=1)]
