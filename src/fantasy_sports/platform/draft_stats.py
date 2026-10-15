"""
Per-player stat columns for the draft board's sortable table. Everything is read from what is already stored; nothing is
estimated.

  * points      last season's fantasy points: football's stored season points, or for hockey the sum of the stored xG
                credits (shot_credits + event_credits) of the games ingested for that player
  * goals       goals scored (hockey shots flagged as goals)
  * assists     primary + secondary assists (the larger of the play-by-play count and the stored assist credits)
  * shots       shots on goal taken (goals included, empty-net goals excluded)
  * blocks      blocked shots credited
  * projected   always None: there is no projection source yet, so no number is invented

A stat is None (shown as a dash) when the data behind it was never ingested, and 0 only when the player has data
but none of that stat. Tables that a deployment does not have are skipped.
"""

from __future__ import annotations

import datetime

from sqlalchemy import case, func, inspect
from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import Game, Player, RealTeam, Week
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit

STAT_KEYS = ("points", "projected", "goals", "assists", "shots", "blocks")


def _has(db: Session, model) -> bool:
    return inspect(db.get_bind()).has_table(model.__tablename__)


def nhl_season_year(today: datetime.date | None = None) -> int:
    """The NHL season's start year: the season that began in September of this year, or of last year before September
    (the same rule scripts/ingest_nhl_daily_slate.py files games under)."""
    today = today or datetime.date.today()
    return today.year if today.month >= 9 else today.year - 1


def current_season_stats(db: Session, asset_ids: list[int]) -> dict[int, dict]:
    """asset id -> {"points", "games"} for THIS season only; assets with nothing scored this season are left out.
    Hockey: the stored xG credit (shot credits + blocker event credits, as in stats_by_asset) on games filed under the
    current NHL season, counted over the games the player earned credit in. Football: the platform's weekly player
    scores for the newest season's weeks. Nothing is estimated."""
    ids = list(asset_ids)
    if not ids:
        return {}
    season = nhl_season_year()
    points: dict[int, float] = {}
    games: dict[int, set] = {}
    if _has(db, ShotCredit) and _has(db, Shot):
        for a, g, pts in (db.query(ShotCredit.draftable_asset_id, Shot.game_id, ShotCredit.credit_points)
                          .join(Shot, Shot.id == ShotCredit.shot_id).join(Game, Game.id == Shot.game_id)
                          .join(Week, Week.id == Game.week_id)
                          .filter(ShotCredit.draftable_asset_id.in_(ids), Week.season_year == season)):
            points[a] = points.get(a, 0.0) + float(pts or 0)
            games.setdefault(a, set()).add(g)
    if _has(db, EventCredit) and _has(db, GameEvent):
        for a, g, pts in (db.query(EventCredit.draftable_asset_id, GameEvent.game_id, EventCredit.credit_points)
                          .join(GameEvent, GameEvent.id == EventCredit.event_id).join(Game, Game.id == GameEvent.game_id)
                          .join(Week, Week.id == Game.week_id)
                          .filter(EventCredit.draftable_asset_id.in_(ids), EventCredit.role == "blocker",
                                  Week.season_year == season)):
            points[a] = points.get(a, 0.0) + float(pts or 0)
            games.setdefault(a, set()).add(g)
    out = {a: {"points": round(points[a], 2), "games": len(g)} for a, g in games.items()}
    from fantasy_sports.sports.football.models import WeeklyPlayerScore
    if _has(db, WeeklyPlayerScore):
        football_season = db.query(func.max(Week.season_year)).join(WeeklyPlayerScore, WeeklyPlayerScore.week_id == Week.id).scalar()
        if football_season is not None:
            for a, total, n in (db.query(WeeklyPlayerScore.draftable_asset_id, func.sum(WeeklyPlayerScore.points),
                                         func.count(WeeklyPlayerScore.id))
                                .join(Week, Week.id == WeeklyPlayerScore.week_id)
                                .filter(WeeklyPlayerScore.draftable_asset_id.in_(ids), Week.season_year == football_season)
                                .group_by(WeeklyPlayerScore.draftable_asset_id)):
                if n and a not in out:
                    out[a] = {"points": round(float(total or 0), 2), "games": int(n)}
    return out


def stats_by_asset(db: Session, asset_ids: list[int]) -> dict[int, dict]:
    """asset id -> {team, points, projected, goals, assists, shots, blocks}. Players with no stored data get all-None stats."""
    ids = list(asset_ids)
    out: dict[int, dict] = {a: {k: None for k in STAT_KEYS} | {"team": None} for a in ids}
    if not ids:
        return out

    from fantasy_sports.platform.db.models.core import DraftableAsset
    for asset_id, team in (db.query(DraftableAsset.id, RealTeam.name).join(Player, Player.id == DraftableAsset.player_id)
                           .outerjoin(RealTeam, RealTeam.id == Player.real_team_id).filter(DraftableAsset.id.in_(ids))):
        out[asset_id]["team"] = team

    for asset_id, pts in season_points_by_asset(db).items():
        if asset_id in out:
            out[asset_id]["points"] = round(pts, 2)

    if _has(db, Shot):
        rows = (db.query(Shot.shooter_asset_id, func.count(Shot.id), func.sum(case((Shot.is_goal.is_(True), 1), else_=0)))
                .filter(Shot.shooter_asset_id.in_(ids), Shot.is_empty_net.is_(False)).group_by(Shot.shooter_asset_id))
        for a, n, g in rows:
            out[a]["shots"], out[a]["goals"] = int(n), int(g or 0)
    hockey_points: dict[int, float] = {}
    if _has(db, ShotCredit):
        for a, p in (db.query(ShotCredit.draftable_asset_id, func.sum(ShotCredit.credit_points))
                     .join(Shot, Shot.id == ShotCredit.shot_id).join(Game, Game.id == Shot.game_id)
                     .join(Week, Week.id == Game.week_id)
                     .filter(ShotCredit.draftable_asset_id.in_(ids), Week.season_year < nhl_season_year())
                     .group_by(ShotCredit.draftable_asset_id)):
            hockey_points[a] = float(p or 0)
        for a, n in (db.query(ShotCredit.draftable_asset_id, func.count(ShotCredit.id))
                     .filter(ShotCredit.draftable_asset_id.in_(ids), ShotCredit.role.in_(("primary_assist", "secondary_assist")))
                     .group_by(ShotCredit.draftable_asset_id)):
            out[a]["assists"] = int(n)
        for a in hockey_points:
            out[a]["assists"] = out[a]["assists"] or 0
    if _has(db, GameEvent):
        events: dict[int, int] = {}
        for col in (GameEvent.secondary_asset_id, GameEvent.tertiary_asset_id):
            for a, n in (db.query(col, func.count(GameEvent.id))
                         .filter(GameEvent.event_type == "goal", col.in_(ids)).group_by(col)):
                events[a] = events.get(a, 0) + int(n)
        for a, n in events.items():
            out[a]["assists"] = max(out[a]["assists"] or 0, n)
    if _has(db, EventCredit):
        for a, n, p in (db.query(EventCredit.draftable_asset_id, func.count(EventCredit.id), func.sum(EventCredit.credit_points))
                        .join(GameEvent, GameEvent.id == EventCredit.event_id).join(Game, Game.id == GameEvent.game_id)
                        .join(Week, Week.id == Game.week_id)
                        .filter(EventCredit.draftable_asset_id.in_(ids), EventCredit.role == "blocker",
                                Week.season_year < nhl_season_year())
                        .group_by(EventCredit.draftable_asset_id)):
            out[a]["blocks"] = int(n)
            hockey_points[a] = hockey_points.get(a, 0.0) + float(p or 0)
    for a, p in hockey_points.items():
        if out[a]["points"] is None:
            out[a]["points"] = round(p, 2)
    return out
