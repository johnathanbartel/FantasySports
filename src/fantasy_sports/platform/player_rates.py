"""Points per game from what is stored, never estimated.

  * Hockey: the stored xG credit (shot credits + event credits) of the games a player earned any credit in, divided by
    the number of those games.
  * Football: the platform's weekly player scores - points divided by the weeks scored.

A player with no stored games gets no rate (None), shown as a dash.
"""

from __future__ import annotations

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from fantasy_sports.sports.football.models import WeeklyPlayerScore
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit


def _has(db: Session, model) -> bool:
    return inspect(db.get_bind()).has_table(model.__tablename__)


def rates_by_asset(db: Session, asset_ids: list[int]) -> dict[int, dict]:
    """asset id -> {"games": n, "points_per_game": x}; assets with no stored games are left out."""
    ids = list(asset_ids)
    if not ids:
        return {}
    points: dict[int, float] = {}
    games: dict[int, set] = {}
    if _has(db, ShotCredit) and _has(db, Shot):
        for a, g, pts in (db.query(ShotCredit.draftable_asset_id, Shot.game_id, ShotCredit.credit_points)
                          .join(Shot, Shot.id == ShotCredit.shot_id).filter(ShotCredit.draftable_asset_id.in_(ids))):
            points[a] = points.get(a, 0.0) + float(pts or 0)
            games.setdefault(a, set()).add(g)
    if _has(db, EventCredit) and _has(db, GameEvent):
        for a, g, pts in (db.query(EventCredit.draftable_asset_id, GameEvent.game_id, EventCredit.credit_points)
                          .join(GameEvent, GameEvent.id == EventCredit.event_id).filter(EventCredit.draftable_asset_id.in_(ids))):
            points[a] = points.get(a, 0.0) + float(pts or 0)
            games.setdefault(a, set()).add(g)
    out = {a: {"games": len(g), "points_per_game": round(points[a] / len(g), 3)} for a, g in games.items() if g}
    if _has(db, WeeklyPlayerScore):
        missing = [a for a in ids if a not in out]
        if missing:
            for a, total, n in (db.query(WeeklyPlayerScore.draftable_asset_id, func.sum(WeeklyPlayerScore.points),
                                         func.count(WeeklyPlayerScore.id))
                                .filter(WeeklyPlayerScore.draftable_asset_id.in_(missing))
                                .group_by(WeeklyPlayerScore.draftable_asset_id)):
                if n:
                    out[a] = {"games": int(n), "points_per_game": round(float(total or 0) / n, 3)}
    return out
