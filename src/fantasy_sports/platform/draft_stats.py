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

from sqlalchemy import case, func, inspect
from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import Player, RealTeam
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit

STAT_KEYS = ("points", "projected", "goals", "assists", "shots", "blocks")


def _has(db: Session, model) -> bool:
    return inspect(db.get_bind()).has_table(model.__tablename__)


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
                     .filter(ShotCredit.draftable_asset_id.in_(ids)).group_by(ShotCredit.draftable_asset_id)):
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
                        .filter(EventCredit.draftable_asset_id.in_(ids), EventCredit.role == "blocker")
                        .group_by(EventCredit.draftable_asset_id)):
            out[a]["blocks"] = int(n)
            hockey_points[a] = hockey_points.get(a, 0.0) + float(p or 0)
    for a, p in hockey_points.items():
        if out[a]["points"] is None:
            out[a]["points"] = round(p, 2)
    return out
