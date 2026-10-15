"""Trading draft picks.

Which picks can be traded depends on the league format:
  * future_pick_redraft - the next draft only (every round, one pick per team per round);
  * keeper / dynasty    - the next N drafts (leagues.pick_trade_seasons), each as many rookie rounds as the commissioner set
                          (a default until a rollover sets them);
  * traditional_redraft - no pick trading (the order is random on draft day).

A pick is identified by (season_number of the draft, round, original team). It belongs to its original team unless a
TradedPick row says otherwise, so nothing is stored until a pick is traded. When a draft is created its pick slots go to the
current owners (api/routers/draft.py). A pick stops being tradeable once its draft has started.

AI teams value a pick by where it would land if the season ended today (reverse of the standings, the champion not yet
known): a pick that would be the Nth overall is worth the points of the Nth best player available, so a trade has to bring an
AI team at least that much back.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fantasy_sports.platform import season_rollover
from fantasy_sports.platform.db.models.core import Draft, League, Team, TradedPick
from fantasy_sports.platform.league_features import roster_limit

TRADEABLE_FORMATS = ("future_pick_redraft", "keeper", "dynasty")
DEFAULT_ROOKIE_PICK_ROUNDS = 3


def enabled(league: League) -> bool:
    return league.league_format in TRADEABLE_FORMATS


def next_draft_season(league: League) -> int:
    """The season number of the next draft: this one's while in the offseason, otherwise the one after the current season."""
    return league.season_number if league.offseason else league.season_number + 1


def tradeable_seasons(league: League) -> list[int]:
    if not enabled(league):
        return []
    first = next_draft_season(league)
    count = 1 if league.league_format == "future_pick_redraft" else max(1, league.pick_trade_seasons)
    return list(range(first, first + count))


def rounds_for(db: Session, league: League, season: int) -> int:
    if league.league_format == "future_pick_redraft":
        return roster_limit(db, league)
    if league.offseason and season == league.season_number and league.offseason_draft_rounds:
        return league.offseason_draft_rounds
    return DEFAULT_ROOKIE_PICK_ROUNDS


def owner_map(db: Session, league_id: int, season: int) -> dict[tuple[int, int], int]:
    """(round, original team id) -> owner team id, for the picks of that season that have changed hands."""
    return {(r.round, r.original_team_id): r.owner_team_id
            for r in db.query(TradedPick).filter_by(league_id=league_id, season_number=season)}


def draft_started(db: Session, league_id: int, season: int) -> bool:
    return db.query(Draft).filter(Draft.league_id == league_id, Draft.season_number == season,
                                  Draft.status.in_(("in_progress", "complete"))).first() is not None


def pick_available(db: Session, league: League, season: int, rnd: int, original_team_id: int) -> str | None:
    """None if the pick can be traded, else why not."""
    if not enabled(league):
        return "Draft picks can't be traded in this league format"
    if season not in tradeable_seasons(league):
        return "That draft is outside the seasons whose picks can be traded"
    if rnd < 1 or rnd > rounds_for(db, league, season):
        return "That round doesn't exist in that draft"
    if db.query(Team).filter_by(id=original_team_id, league_id=league.id).first() is None:
        return "That pick's original team isn't in this league"
    if draft_started(db, league.id, season):
        return "That draft has already started"
    return None


def owned_picks(db: Session, league: League, team_id: int) -> list[dict]:
    """Every tradeable pick the team owns right now: its own that it still holds, plus the ones it acquired."""
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league.id).order_by(Team.id)]
    out = []
    for season in tradeable_seasons(league):
        if draft_started(db, league.id, season):
            continue
        owners = owner_map(db, league.id, season)
        for rnd in range(1, rounds_for(db, league, season) + 1):
            for original in team_ids:
                if owners.get((rnd, original), original) == team_id:
                    out.append({"season": season, "round": rnd, "original_team_id": original})
    return out


def projected_order(db: Session, league: League) -> list[int]:
    """Team ids worst-first as the standings stand today. In the offseason it is the archived season's order."""
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league.id).order_by(Team.id)]
    try:
        if league.offseason:
            order = season_rollover.draft_order(db, league)
            if order:
                return order
        best_first = [r["team_id"] for r in season_rollover.final_standings(db, league)]
    except Exception:
        return team_ids
    return [t for t in reversed(best_first) if t in team_ids] or team_ids


def projected_overall(league: League, order: list[int], rnd: int, original_team_id: int) -> int:
    n = len(order)
    idx = order.index(original_team_id) if original_team_id in order else 0
    snake = season_rollover.draft_is_snake(league)
    return (rnd - 1) * n + (idx if (rnd % 2 == 1 or not snake) else n - 1 - idx) + 1


def ranked_points(db: Session, league: League) -> list[float]:
    """Last season's points of every draftable player in the league's pool, best first - the yardstick for a pick's value."""
    from fantasy_sports.platform.defense_modes import league_pool
    from fantasy_sports.platform.draft_stats import stats_by_asset
    ids = [a.id for a in league_pool(db, league.id)]
    stats = stats_by_asset(db, ids)
    return sorted((float(s.get("points") or 0.0) for s in stats.values()), reverse=True)


def pick_value(points_desc: list[float], overall: int) -> float:
    """The points of the player who would go Nth overall in a points-ranked draft (0 past the end of the pool)."""
    return points_desc[overall - 1] if 0 < overall <= len(points_desc) else 0.0


def transfer(db: Session, league: League, picks: list[tuple[int, int, int]], to_team_id: int) -> None:
    """Gives each pick (season, round, original team id) to a team. Flushes."""
    for season, rnd, original in picks:
        row = db.query(TradedPick).filter_by(league_id=league.id, season_number=season, round=rnd,
                                             original_team_id=original).one_or_none()
        if to_team_id == original:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(TradedPick(league_id=league.id, season_number=season, round=rnd, original_team_id=original,
                              owner_team_id=to_team_id))
        else:
            row.owner_team_id = to_team_id
    db.flush()


def current_owner(db: Session, league_id: int, season: int, rnd: int, original_team_id: int) -> int:
    row = db.query(TradedPick).filter_by(league_id=league_id, season_number=season, round=rnd,
                                         original_team_id=original_team_id).one_or_none()
    return row.owner_team_id if row is not None else original_team_id
