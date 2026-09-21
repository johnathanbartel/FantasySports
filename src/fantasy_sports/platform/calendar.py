"""
Which seasons the app is working with, read from the data rather than
hard-coded so the same code serves a seeded 2024 fixture and a current 2026
league:

  * current_season_year - the newest season that has Week rows (lineups are set
    for these weeks, and the stats table shows their weekly scores).
  * last_points_season  - the newest season with full-season fantasy points
    ingested (player_season_points): what the draft board shows and the AI values
    players by. For a 2026 league that is 2025, the last completed season.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import Week
from fantasy_sports.sports.football.models import PlayerSeasonPoints


def current_season_year(db: Session, universe_ids: list[int] | None = None) -> int | None:
    q = db.query(func.max(Week.season_year))
    if universe_ids:
        q = q.filter(Week.universe_id.in_(universe_ids))
    return q.scalar()


def last_points_season(db: Session) -> int | None:
    return db.query(func.max(PlayerSeasonPoints.season)).scalar()
