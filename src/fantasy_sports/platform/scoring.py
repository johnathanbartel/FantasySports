"""
League scoring, DERIVED LIVE from lineups and per-player weekly scores.

A team's score for a week is the sum of its STARTERS' weekly scores for that
week (bench players don't count); a matchup is won by the higher score. Nothing
here is stored - matchup results, standings and totals are recomputed from the
lineup entries and weekly_player_scores every time they are read. That is what
lets a commissioner go back and change a past week's lineup and have every
result, standing and total update: there is nothing stale to fix.

A player's weekly score is his summed EPA-based credit for his real team's game
that week (sports/football/college/ingest_scores.py); a real team with no game
that week (a bye) scores 0.

A team's score is the fitted scale (platform/scoring_scale.py, docs/scoring_scale_fit_report.md) applied
ONCE to the sum of its starters' raw credit: the map is strictly increasing, so the team with more credit always
wins, and over a season a league's average score matches the NCAA's points per game whatever its size. Individual
players are shown in raw EPA credit; only the team total is in football points.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session, joinedload

from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.platform import calendar, season
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    Game,
    League,
    LeagueUniverse,
    LineupEntry,
    RealTeam,
    Team,
    Week,
    WeeklyMatchup,
)
from fantasy_sports.platform.scoring_scale import ScaleContext, context_for_league, to_fantasy_points as scale_points
from fantasy_sports.sports.football.models import WeeklyPlayerScore


def league_context(db: Session, league_id: int) -> ScaleContext | None:
    """The realistic scale for the league; None for an imported-scoring league, which has no scale (FS-037)."""
    if db.get(League, league_id).scoring_system == "imported":
        return None
    return context_for_league(db, league_id)


def to_fantasy_points(raw_team_credit: float, context: ScaleContext) -> float:
    return scale_points(raw_team_credit, context)


def universe_ids(db: Session, league_id: int) -> list[int]:
    return [lu.universe_id for lu in db.query(LeagueUniverse).filter_by(league_id=league_id)]


def season_weeks(db: Session, league_id: int) -> list[Week]:
    """The league's season: the newest season's weeks, in order."""
    ids = universe_ids(db, league_id)
    current = calendar.current_season_year(db, ids)
    return db.query(Week).filter(Week.universe_id.in_(ids), Week.season_year == current).order_by(Week.week_number).all()


def scored_week_ids(db: Session, week_ids: list[int]) -> set[int]:
    """Weeks that have real games ingested (so their scores exist)."""
    return {g.week_id for g in db.query(Game).filter(Game.week_id.in_(week_ids or [0]), Game.status == "final")}


def is_locked(week: Week, today: datetime.date | None = None) -> bool:
    """A week's lineups lock when it starts. (A commissioner-mode commissioner can still change them.)"""
    today = today or datetime.datetime.utcnow().date()
    return week.start_date is not None and week.start_date <= today


@dataclass
class StarterLine:
    slot_code: str
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    points: float         # raw EPA credit for the week
    played: bool          # his real team had a game this week


def _teams_with_games(db: Session, week_id: int) -> set[int]:
    out: set[int] = set()
    for g in db.query(Game).filter_by(week_id=week_id):
        out.add(g.home_team_id)
        out.add(g.away_team_id)
    return out


def lineup_breakdown(db: Session, team_id: int, week_id: int) -> list[StarterLine]:
    """The team's starters for the week with each one's score, in slot order."""
    entries = (
        db.query(LineupEntry).options(
            joinedload(LineupEntry.roster_slot),
            joinedload(LineupEntry.draftable_asset).options(joinedload(DraftableAsset.player), joinedload(DraftableAsset.defense_group)),
        ).filter_by(team_id=team_id, week_id=week_id).order_by(LineupEntry.id).all()
    )
    starters = [e for e in entries if e.roster_slot.is_starter]
    scores = {
        s.draftable_asset_id: s.points
        for s in db.query(WeeklyPlayerScore).filter(
            WeeklyPlayerScore.week_id == week_id,
            WeeklyPlayerScore.draftable_asset_id.in_([e.draftable_asset_id for e in starters] or [0]))
    }
    played = _teams_with_games(db, week_id)
    schools = {t.id: t.name for t in db.query(RealTeam)}
    out = []
    for e in starters:
        a = e.draftable_asset
        real_id = a.player.real_team_id if a.player else a.defense_group.real_team_id
        raw = scores.get(a.id, 0.0)
        out.append(StarterLine(
            slot_code=e.roster_slot.slot_code, draftable_asset_id=a.id, display_name=draftable_asset_display_name(a),
            position=draftable_asset_position(a), real_team=schools.get(real_id), points=raw, played=real_id in played))
    return out


def team_raw_credit(db: Session, team_id: int, week_id: int) -> float:
    return sum(l.points for l in lineup_breakdown(db, team_id, week_id))


def team_week_score(db: Session, team_id: int, week_id: int, context: ScaleContext | None = None) -> float:
    """The team's fantasy points for the week: the fitted scale applied to its starters' summed credit.
    A team with no starters set scores 0."""
    team = db.get(Team, team_id)
    if db.get(League, team.league_id).scoring_system == "imported":
        from fantasy_sports.platform import imported_scoring       # a frozen box-score formula, no realistic scale
        return imported_scoring.team_week_score(db, team_id, week_id, team.league_id)
    lines = lineup_breakdown(db, team_id, week_id)
    if not lines:
        return 0.0
    if context is None:
        context = league_context(db, db.get(Team, team_id).league_id)
    return to_fantasy_points(sum(l.points for l in lines), context)


@dataclass
class MatchupResult:
    matchup_id: int | None          # None for a conference-championship game (derived, not stored)
    week_id: int
    team_a_id: int
    team_b_id: int
    scored: bool
    team_a_score: float | None
    team_b_score: float | None
    winner_team_id: int | None      # None for a tie or an unscored week
    championship: bool = False


def _result(db: Session, matchup_id, week_id: int, a_id: int, b_id: int, scored: bool, context: ScaleContext,
            championship: bool = False) -> MatchupResult:
    if not scored:
        return MatchupResult(matchup_id, week_id, a_id, b_id, False, None, None, None, championship)
    a, b = team_week_score(db, a_id, week_id, context), team_week_score(db, b_id, week_id, context)
    winner = a_id if a > b else b_id if b > a else None
    return MatchupResult(matchup_id, week_id, a_id, b_id, True, a, b, winner, championship)


def matchup_result(db: Session, m: WeeklyMatchup, scored: bool, context: ScaleContext | None = None) -> MatchupResult:
    return _result(db, m.id, m.week_id, m.team_a_id, m.team_b_id, scored, context or league_context(db, m.league_id))


def _regular_results(db: Session, league_id: int, weeks: list[Week], scored: set[int], context: ScaleContext) -> dict[int, list[MatchupResult]]:
    by_week: dict[int, list[MatchupResult]] = {w.id: [] for w in weeks}
    for m in db.query(WeeklyMatchup).filter(
            WeeklyMatchup.league_id == league_id, WeeklyMatchup.week_id.in_(list(by_week) or [0])).order_by(WeeklyMatchup.id):
        by_week[m.week_id].append(matchup_result(db, m, m.week_id in scored, context))
    return by_week


def _standings_from(db: Session, league_id: int, results: list[MatchupResult]) -> list[dict]:
    rows = {t.id: {"team_id": t.id, "team_name": t.name, "wins": 0, "losses": 0, "ties": 0,
                   "points_for": 0.0, "points_against": 0.0}
            for t in db.query(Team).filter_by(league_id=league_id)}
    for r in results:
        if not r.scored:
            continue
        for me, mine, theirs in ((r.team_a_id, r.team_a_score, r.team_b_score), (r.team_b_id, r.team_b_score, r.team_a_score)):
            row = rows[me]
            row["points_for"] += mine
            row["points_against"] += theirs
            if r.winner_team_id is None:
                row["ties"] += 1
            elif r.winner_team_id == me:
                row["wins"] += 1
            else:
                row["losses"] += 1
    return sorted(rows.values(), key=lambda r: (-r["wins"], -r["ties"], -r["points_for"], r["team_name"]))


def _split_weeks(weeks: list[Week]) -> tuple[list[Week], Week | None]:
    regular = [w for w in weeks if w.week_number <= season.REGULAR_SEASON_WEEKS]
    championship = next((w for w in weeks if w.week_number == season.CHAMPIONSHIP_WEEK), None)
    return regular, championship


def championships(db: Session, league_id: int) -> dict:
    """Week 10: {"ready", "week_id", "games": [(conference_id, team_a, team_b)], "byes": [team ids]}.
    Ready once every regular-season week has been scored; the pairings come from the regular-season standings."""
    from fantasy_sports.platform import schedule       # (schedule imports this module)
    weeks = season_weeks(db, league_id)
    regular, champ_week = _split_weeks(weeks)
    scored = scored_week_ids(db, [w.id for w in weeks])
    ready = bool(regular) and champ_week is not None and all(w.id in scored for w in regular) \
        and db.query(WeeklyMatchup).filter_by(league_id=league_id).count() > 0
    if not ready:
        return {"ready": False, "week_id": champ_week.id if champ_week else None, "games": [], "byes": []}
    context = league_context(db, league_id)
    results = [r for rs in _regular_results(db, league_id, regular, scored, context).values() for r in rs]
    games, byes = schedule.championship_bracket(db, league_id, _standings_from(db, league_id, results))
    return {"ready": True, "week_id": champ_week.id, "games": [(g.conference_id, g.team_a_id, g.team_b_id) for g in games], "byes": byes}


def league_results(db: Session, league_id: int) -> dict[int, list[MatchupResult]]:
    """week_id -> that week's matchup results (scores only for weeks whose games are in). Week 10's
    conference-championship games are derived from the standings and appear once they are set."""
    weeks = season_weeks(db, league_id)
    scored = scored_week_ids(db, [w.id for w in weeks])
    context = league_context(db, league_id)
    by_week = _regular_results(db, league_id, weeks, scored, context)
    champ = championships(db, league_id)
    if champ["ready"]:
        by_week[champ["week_id"]] = [
            _result(db, None, champ["week_id"], a, b, champ["week_id"] in scored, context, championship=True)
            for _, a, b in champ["games"]]
    return by_week


def standings(db: Session, league_id: int) -> list[dict]:
    """Every team's regular-season record (weeks 1-9) and points from the scored weeks, best first."""
    weeks = season_weeks(db, league_id)
    regular, _ = _split_weeks(weeks)
    scored = scored_week_ids(db, [w.id for w in weeks])
    results = [r for rs in _regular_results(db, league_id, regular, scored, league_context(db, league_id)).values() for r in rs]
    return _standings_from(db, league_id, results)
