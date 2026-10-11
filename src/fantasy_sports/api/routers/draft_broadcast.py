"""
Draft broadcast room: a versioned announcement for every completed pick, with deterministic local commentary
(platform/ai/commentary.py). Announcements are derived on read from the stored picks, so there is nothing to
duplicate, nothing to lose on a restart, and a client that reconnects simply asks for everything after the last pick
it saw. No external service is called.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.schemas import (
    AnnouncementCommentaryOut, AnnouncementPlayerOut, AnnouncementsOut, AnnouncementTeamOut, PickAnnouncementOut,
)
from fantasy_sports.platform.ai import commentary
from fantasy_sports.platform.db.models.core import Draft, DraftPick, Team
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.defense_modes import league_pool
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset

router = APIRouter(prefix="/drafts", tags=["draft-broadcast"])


def build_announcements(db: Session, draft: Draft) -> list[PickAnnouncementOut]:
    """Every completed pick's announcement, in pick order."""
    picks = db.query(DraftPick).filter_by(draft_id=draft.id).order_by(DraftPick.pick_number).all()
    teams = {t.id: t for t in db.query(Team).filter_by(league_id=draft.league_id)}
    points = season_points_by_asset(db)
    pool_ids = {a.id for a in league_pool(db, draft.league_id)}
    ranked = sorted((a for a in pool_ids if a in points), key=lambda a: (-points[a], a))
    rank_of = {a: i for i, a in enumerate(ranked, start=1)}
    provider = commentary.get_provider()
    by_team: dict[tuple[int, str], int] = {}
    by_league: dict[str, int] = {}
    out = []
    for p in picks:
        if p.draftable_asset_id is None:
            continue
        asset = p.draftable_asset
        pos = draftable_asset_position(asset)
        key = pos or ""
        ctx = commentary.PickContext(
            pick_number=p.pick_number, round=p.round, teams=len(teams), team_name=teams[p.team_id].name,
            player_name=draftable_asset_display_name(asset), position=pos,
            team_position_count=by_team.get((p.team_id, key), 0), league_position_count=by_league.get(key, 0),
            pool_rank=rank_of.get(asset.id), ranked_pool_size=len(ranked) or None, is_autopick=bool(p.is_autopick))
        c = provider.describe(ctx)
        by_team[(p.team_id, key)] = by_team.get((p.team_id, key), 0) + 1
        by_league[key] = by_league.get(key, 0) + 1
        lead = f"Pick {p.pick_number}, round {p.round}: {ctx.team_name} select {ctx.player_name}" + (f", {pos}." if pos else ".")
        out.append(PickAnnouncementOut(
            version=commentary.EVENT_VERSION, event_id=f"draft:{draft.id}:pick:{p.pick_number}", draft_id=draft.id,
            pick_number=p.pick_number, round=p.round, team=AnnouncementTeamOut(id=p.team_id, name=ctx.team_name),
            player=AnnouncementPlayerOut(asset_id=asset.id, name=ctx.player_name, position=pos),
            is_autopick=bool(p.is_autopick), picked_at=p.picked_at,
            commentary=AnnouncementCommentaryOut(provider=provider.provider_id, text=c.text, tags=list(c.tags),
                                                 value_label=c.value_label, unavailable=list(c.unavailable), inputs=c.inputs),
            spoken_text=f"{lead} {c.text}"))
    return out


@router.get("/{draft_id}/announcements", response_model=AnnouncementsOut)
def read_announcements(draft_id: int, after_pick: int = 0, limit: int = 300, db: Session = Depends(get_db)):
    """Announcements for picks after `after_pick` (0 = from the start). Idempotent; used for first load, polling and
    reconnect backfill alike."""
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(404, "Draft not found")
    if after_pick < 0:
        raise HTTPException(400, "after_pick cannot be negative")
    allv = build_announcements(db, draft)
    page = [a for a in allv if a.pick_number > after_pick][: max(1, min(limit, 500))]
    return AnnouncementsOut(version=commentary.EVENT_VERSION, draft_id=draft_id, provider=commentary.get_provider().provider_id,
                            after_pick=after_pick, next_pick=page[-1].pick_number if page else after_pick,
                            picks_made=len(allv), announcements=page)
