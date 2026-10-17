"""Which franchises are mathematically eliminated from the playoffs (CFG-06 integration boundary).

The proof itself is platform/elimination_engine.py (pure, sport-neutral). This module is the glue around it:
  * snapshot_for() turns a league's standings data into the engine's Snapshot. One adapter per sport; hockey is the only sport with
    a regular-season standings table and a remaining schedule today. To support another sport, write an adapter that returns a
    Snapshot (its played games with scores, its unplayed games, the standings rules and playoff rules) and add it to ADAPTERS.
  * compute() runs the engine and returns the report without writing anything.
  * recalculate() writes the result to teams.eliminated_season, which switches on the CFG-06 trade restrictions
    (platform/trade_eligibility.py). It runs right after a game is finalised. Elimination is permanent: under the same rules a
    team that is proven out stays out, so a normal recalculation only ever adds teams. The one exception is clear_stale=True, used
    when the proof's premises change (the commissioner edits the tiebreakers or playoff rules, or voids a finished game), which
    drops any team that is no longer proven out.
  * nothing a client sends ever reaches mark_eliminated(). A low playoff probability is not elimination, so a team is active
    until the proof says otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from fantasy_sports.platform import elimination_engine as engine
from fantasy_sports.platform import playoffs, series_standings
from fantasy_sports.platform.db.models.core import League, Team

log = logging.getLogger(__name__)


def is_eliminated(league: League, team: Team) -> bool:
    """True when the franchise was eliminated in the season now being played (offseason: nobody is eliminated)."""
    return not league.offseason and team.eliminated_season == league.season_number


def mark_eliminated(db: Session, league: League, team: Team) -> None:
    """Records that the team is mathematically eliminated from this season's playoffs. Flushes."""
    team.eliminated_season = league.season_number
    db.flush()


def clear_elimination(db: Session, team: Team) -> None:
    team.eliminated_season = None
    db.flush()


# --- one adapter per sport ---------------------------------------------------------------------------------------------

def _hockey_snapshot(db: Session, league: League) -> engine.Snapshot | None:
    team_ids, _series, games = series_standings.load_league(db, league.id)
    if not games:
        return None
    known = set(team_ids)
    games = [g for g in games if g.home_team_id in known and g.away_team_id in known]
    results = [engine.Result(g.home_team_id, g.away_team_id, g.home_score, g.away_score) for g in games if g.final]
    remaining = [engine.Fixture(g.home_team_id, g.away_team_id) for g in games if not g.final]
    rules = series_standings.league_rules(db, league)
    return engine.Snapshot(team_ids, dict(rules.division_of or {}), results, remaining, rules,
                           playoffs.rules_for(league, len(team_ids)), ties_possible=True)


# A league belongs to the first adapter whose test passes. The tests are how the platform already tells sports apart.
ADAPTERS: list[tuple[Callable[[League], bool], Callable[[Session, League], "engine.Snapshot | None"]]] = [
    (lambda league: league.series_length_days is not None, _hockey_snapshot),
]


def snapshot_for(db: Session, league: League) -> engine.Snapshot | None:
    """The league's standings situation, or None when its sport has no adapter or no schedule exists yet."""
    for belongs, build in ADAPTERS:
        if belongs(league):
            return build(db, league)
    return None


def compute(db: Session, league: League, node_budget: int = engine.DEFAULT_NODE_BUDGET) -> engine.Report | None:
    """Who is proven out right now. None in the offseason, before a schedule exists, and for a sport without an adapter."""
    if league.offseason:
        return None
    snap = snapshot_for(db, league)
    return engine.analyze(snap, node_budget) if snap is not None else None


def eliminated_team_ids(db: Session, league: League) -> set[int]:
    report = compute(db, league)
    return set(report.eliminated) if report else set()


def recalculate(db: Session, league: League, *, clear_stale: bool = False) -> list[int]:
    """Writes the proof's result to the teams. Returns the ids newly marked eliminated. Flushes, does not commit."""
    report = compute(db, league)
    if report is None:
        return []
    newly: list[int] = []
    for team in db.query(Team).filter_by(league_id=league.id):
        if team.id in report.eliminated:
            if team.eliminated_season != league.season_number:
                mark_eliminated(db, league, team)
                newly.append(team.id)
        elif clear_stale and team.eliminated_season == league.season_number:
            clear_elimination(db, team)
    return newly


def recalculate_league(db: Session, league_id: int, *, clear_stale: bool = False) -> list[int]:
    """recalculate() for the hooks that run inside someone else's transaction (a game finishing, a setting changing): a failure
    here is logged and never stops the game from finalising."""
    league = db.get(League, league_id)
    if league is None:
        return []
    try:
        return recalculate(db, league, clear_stale=clear_stale)
    except Exception:           # the proof is a bonus on top of finalising a game; the next game re-runs it
        log.exception("elimination recalculation failed for league %s", league_id)
        return []
