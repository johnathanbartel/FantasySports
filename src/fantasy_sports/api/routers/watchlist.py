"""
Watchlist: players a team is keeping an eye on. The Watch button on a player card adds to it; the Transactions tab lists it.

  GET    /teams/{team_id}/watchlist
  POST   /teams/{team_id}/watchlist            {draftable_asset_id}
  DELETE /teams/{team_id}/watchlist/{draftable_asset_id}

Private to the team's manager (or a commissioner acting for an AI team): nobody else sees who a team watches. Each row says
where the player is now (free agent, on waivers, on another team, on this team) so the list doubles as a shopping list.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from fantasy_sports.api.auth import assert_can_manage_team, current_user
from fantasy_sports.api.helpers import draftable_asset_age, draftable_asset_display_name, draftable_asset_position
from fantasy_sports.platform import free_agency, waivers
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, LeagueUniverse, RealTeam, RosterEntry, Team, User, WatchlistEntry,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.draft_stats import current_season_stats, stats_by_asset
from fantasy_sports.platform.player_rates import rates_by_asset

router = APIRouter(tags=["watchlist"])


class WatchRequest(BaseModel):
    draftable_asset_id: int


class WatchedPlayerOut(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    age: int | None = None
    injury_status: str | None = None
    last_season_points: float | None = None
    points_per_game: float | None = None
    this_season_points: float | None = None
    this_season_games: int | None = None
    status: str                       # "free_agent" / "waivers" / "mine" / "rostered" (another team) / "unavailable"
    team_id: int | None = None        # the team that rosters him
    team_name: str | None = None
    waiver_until: datetime.datetime | None = None
    watched_at: datetime.datetime


@router.get("/teams/{team_id}/watchlist", response_model=list[WatchedPlayerOut])
def get_watchlist(team_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_can_manage_team(db, user, team_id)
    entries = (db.query(WatchlistEntry).filter_by(team_id=team.id)
               .order_by(WatchlistEntry.created_at.desc(), WatchlistEntry.id.desc()).all())
    if not entries:
        return []
    ids = [e.draftable_asset_id for e in entries]
    assets = {a.id: a for a in db.query(DraftableAsset)
              .options(joinedload(DraftableAsset.player), joinedload(DraftableAsset.defense_group))
              .filter(DraftableAsset.id.in_(ids))}
    teams = {t.id: t for t in db.query(Team).filter_by(league_id=team.league_id)}
    held = {r.draftable_asset_id: r.team_id for r in db.query(RosterEntry).filter(
        RosterEntry.team_id.in_(list(teams)), RosterEntry.draftable_asset_id.in_(ids))}
    pool = set(free_agency.pool_ids(db, team.league_id))
    on_waivers = waivers.waiver_until(db, team.league_id, datetime.datetime.utcnow())
    schools = {t.id: t.name for t in db.query(RealTeam)}
    stats, rates, current = stats_by_asset(db, ids), rates_by_asset(db, ids), current_season_stats(db, ids)
    out = []
    for e in entries:
        a = assets.get(e.draftable_asset_id)
        if a is None:
            continue
        holder = held.get(a.id)
        if holder == team.id:
            status = "mine"
        elif holder is not None:
            status = "rostered"
        elif a.id in pool:
            status = "waivers" if a.id in on_waivers else "free_agent"
        else:
            status = "unavailable"
        real_team_id = a.player.real_team_id if a.player else (a.defense_group.real_team_id if a.defense_group else None)
        out.append(WatchedPlayerOut(
            draftable_asset_id=a.id, display_name=draftable_asset_display_name(a), position=draftable_asset_position(a),
            real_team=schools.get(real_team_id), age=draftable_asset_age(a),
            injury_status=a.player.injury_status if a.player else None,
            last_season_points=stats.get(a.id, {}).get("points"), points_per_game=rates.get(a.id, {}).get("points_per_game"),
            this_season_points=current.get(a.id, {}).get("points"), this_season_games=current.get(a.id, {}).get("games"),
            status=status, team_id=holder, team_name=teams[holder].name if holder in teams else None,
            waiver_until=on_waivers.get(a.id) if status == "waivers" else None, watched_at=e.created_at))
    return out


@router.post("/teams/{team_id}/watchlist", response_model=WatchRequest)
def watch_player(team_id: int, payload: WatchRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_can_manage_team(db, user, team_id)
    asset = db.get(DraftableAsset, payload.draftable_asset_id)
    if asset is None or not db.query(LeagueUniverse).filter_by(league_id=team.league_id, universe_id=asset.universe_id).first():
        raise HTTPException(404, "Player not found in this league")
    if db.query(RosterEntry).filter_by(team_id=team.id, draftable_asset_id=asset.id).first():
        raise HTTPException(400, "That player is already on your roster")
    if not db.query(WatchlistEntry).filter_by(team_id=team.id, draftable_asset_id=asset.id).first():
        db.add(WatchlistEntry(team_id=team.id, draftable_asset_id=asset.id, created_at=datetime.datetime.utcnow()))
        db.commit()
    return payload


@router.delete("/teams/{team_id}/watchlist/{draftable_asset_id}")
def unwatch_player(team_id: int, draftable_asset_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_can_manage_team(db, user, team_id)
    db.query(WatchlistEntry).filter_by(team_id=team.id, draftable_asset_id=draftable_asset_id).delete()
    db.commit()
    return {"ok": True}
