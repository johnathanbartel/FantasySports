"""The Trade Hub: every team's trading plan, and who is mathematically eliminated.

A manager picks Win Now or Rebuilding; everyone starts Win Now. An AI team has no choice to make: it is Win Now until it is
mathematically eliminated from the playoffs, and Rebuilding from then on.

Elimination here is read-only and conservative. A team is out only when nothing can save it, never because it merely looks
unlikely. It deliberately does not write teams.eliminated_season: that flag switches on the CFG-06 trade restrictions
(platform/elimination.py), which are a separate decision from showing a plan.

Only hockey leagues have a standings table and a remaining schedule to prove elimination from (series_standings.py), so in
any other league nobody is eliminated and every AI team stays Win Now.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fantasy_sports.platform import playoffs, series_standings
from fantasy_sports.platform.db.models.core import League, Team

WIN_NOW = "win_now"
REBUILDING = "rebuilding"
PLANS = (WIN_NOW, REBUILDING)


def eliminated_from(best: dict[int, float], worst: dict[int, float], division_of: dict[int, int | None], spots: int) -> set[int]:
    """Teams that cannot make a field of `spots` teams, whatever happens in the games left.

    best / worst = the most / least a team can finish with on the league's ranking number (points or win %).
    A team is out when `spots` other teams are already guaranteed to finish strictly above even its best finish (a tie is never
    counted as out - the tiebreakers could still go its way), and it cannot win its division (a division winner is seeded in
    ahead of the rank order, see playoffs.seed_teams). Divisions are only ever a way back in, so ignoring them can only keep a
    team alive, never knock one out.
    """
    out: set[int] = set()
    for t, top in best.items():
        above = [o for o, floor in worst.items() if o != t and floor > top]
        if len(above) < spots:
            continue
        div = division_of.get(t)
        if div is not None and not any(division_of.get(o) == div for o in above):
            continue            # nobody in its division is guaranteed to beat it: it can still win the division
        out.add(t)
    return out


def eliminated_team_ids(db: Session, league: League) -> set[int]:
    """The franchises that are mathematically out of the playoffs now. Empty in the offseason, before a schedule exists, and in
    any league without a series standings table."""
    if league.offseason or league.series_length_days is None:
        return set()
    team_ids, series, games = series_standings.load_league(db, league.id)
    if not games:
        return set()
    rules = series_standings.rules_for(league)
    rows = {r.team_id: r for r in series_standings.compute_standings(team_ids, series, games, rules=rules)}
    left = {t: 0 for t in team_ids}
    for g in games:
        if not g.final:
            for t in (g.home_team_id, g.away_team_id):
                if t in left:
                    left[t] += 1
    best, worst = {}, {}
    for t, r in rows.items():
        if rules.mode == "points":
            best[t] = r.points + left[t] * max(rules.points_win, rules.points_tie)
            worst[t] = r.points
        else:
            played = r.games_played + left[t]
            banked = r.wins + 0.5 * r.ties
            best[t] = (banked + left[t]) / played if played else 0.0
            worst[t] = banked / played if played else 0.0
    division_of = {t.id: t.division_id for t in db.query(Team).filter_by(league_id=league.id)}
    return eliminated_from(best, worst, division_of, playoffs.effective_playoff_teams(league, len(team_ids)))


def plan_of(team: Team, eliminated: set[int]) -> str:
    """The plan the league sees for a team: an AI team's follows its elimination, a manager's is their own choice."""
    if team.is_ai_owned:
        return REBUILDING if team.id in eliminated else WIN_NOW
    return team.trade_plan if team.trade_plan in PLANS else WIN_NOW
