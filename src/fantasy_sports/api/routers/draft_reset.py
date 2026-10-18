"""
Draft reset endpoints (platform/draft_reset.py): see where each of the league's drafts stands and how a reset would go, ask
for a reset (the commissioner), and vote on or withdraw one that went to the league. Members read; only the commissioner
asks; only a person-managed team votes.
"""

import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, current_user
from fantasy_sports.platform import commissioner, draft_reset, notifications, setting_changes
from fantasy_sports.platform.db.models.core import (
    AuctionDraft, Draft, DraftResetRequest, League, RecruitingDraft, Team, User,
)
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["draft-reset"])


class DraftResetStateOut(BaseModel):
    kind: str
    draft_id: int
    status: str
    done: int            # picks made / rounds held / periods processed
    total: int
    mode: str            # now | vote | off
    can_reset: bool
    reason: str | None


class ResetVoteOut(BaseModel):
    team_id: int
    team_name: str
    vote: str | None


class DraftResetRequestOut(BaseModel):
    id: int
    league_id: int
    draft_kind: str
    draft_id: int
    keep: int
    status: str
    failure_reason: str | None
    created_at: datetime.datetime
    resolved_at: datetime.datetime | None
    votes: list[ResetVoteOut]
    my_vote: str | None = None
    can_vote: bool = False


class DraftResetsOut(BaseModel):
    policy: str
    drafts: list[DraftResetStateOut]
    open_request: DraftResetRequestOut | None
    history: list[DraftResetRequestOut]


class DraftResetBody(BaseModel):
    kind: Literal["snake", "auction", "recruiting"]
    draft_id: int
    keep: int = Field(default=0, ge=0, le=10_000)     # picks / rounds / periods that stay; 0 = from the start


class DraftResetVoteBody(BaseModel):
    vote: Literal["approve", "reject"]


def _request_out(db: Session, req: DraftResetRequest, user: User) -> DraftResetRequestOut:
    cast = {v.team_id: v.vote for v in req.votes}
    teams = setting_changes.voters(db, req.league_id)
    mine = next((t for t in teams if t.owner_user_id == user.id), None)
    return DraftResetRequestOut(
        id=req.id, league_id=req.league_id, draft_kind=req.draft_kind, draft_id=req.draft_id, keep=req.keep, status=req.status,
        failure_reason=req.failure_reason, created_at=req.created_at, resolved_at=req.resolved_at,
        votes=[ResetVoteOut(team_id=t.id, team_name=t.name, vote=cast.get(t.id)) for t in teams],
        my_vote=cast.get(mine.id) if mine else None, can_vote=bool(mine) and req.status == "voting")


def _league_for_member(db: Session, league_id: int, user: User) -> League:
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if not commissioner.is_commissioner(db, league, user.id) and not db.query(Team).filter_by(
            league_id=league_id, owner_user_id=user.id).first():
        raise HTTPException(403, "You are not a member of this league")
    return league


def _drafts(db: Session, league_id: int) -> list[tuple[str, object]]:
    """The league's latest draft of each type."""
    found: list[tuple[str, object]] = []
    for kind, model in (("snake", Draft), ("auction", AuctionDraft), ("recruiting", RecruitingDraft)):
        draft = db.query(model).filter_by(league_id=league_id).order_by(model.id.desc()).first()
        if draft is not None:
            found.append((kind, draft))
    return found


@router.get("/leagues/{league_id}/draft-resets", response_model=DraftResetsOut)
def get_draft_resets(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Where each draft stands and whether (and how) it can be reset, the open vote if any, and past requests."""
    league = _league_for_member(db, league_id, user)
    rows = db.query(DraftResetRequest).filter_by(league_id=league_id).order_by(DraftResetRequest.id.desc()).all()
    open_req = next((r for r in rows if r.status == "voting"), None)
    return DraftResetsOut(
        policy=league.draft_reset_policy,
        drafts=[DraftResetStateOut(**draft_reset.describe(db, league, k, d)) for k, d in _drafts(db, league_id)],
        open_request=_request_out(db, open_req, user) if open_req else None,
        history=[_request_out(db, r, user) for r in rows if r.status != "voting"][:20])


@router.post("/leagues/{league_id}/draft-resets", response_model=DraftResetRequestOut)
def request_draft_reset(league_id: int, payload: DraftResetBody, background: BackgroundTasks, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """Commissioner: resets a draft. While the draft runs, or when the league lets the commissioner decide, it happens now and
    the response says 'applied'; otherwise it opens a vote that needs every person-managed team ('voting')."""
    league = assert_commissioner(db, user, league_id)
    try:
        draft = draft_reset.get_draft(db, league_id, payload.kind, payload.draft_id)
        # A timed snake draft's pick may be due: make it first so a reset starts from a settled state.
        if payload.kind == "snake":
            from fantasy_sports.api.routers.draft import _lock_draft, process_overdue
            draft = _lock_draft(db, draft.id)
            process_overdue(db, draft, datetime.datetime.utcnow())
        req = draft_reset.request_reset(db, league, payload.kind, draft, payload.keep, user.id)
    except draft_reset.ResetError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except PermissionError as e:
        db.rollback()
        raise HTTPException(403, str(e))
    db.commit()
    background.add_task(notifications.deliver_pending)
    return _request_out(db, req, user)


def _load_request(db: Session, request_id: int, user: User) -> DraftResetRequest:
    req = db.get(DraftResetRequest, request_id)
    if req is None:
        raise HTTPException(404, "Reset request not found")
    _league_for_member(db, req.league_id, user)
    return req


@router.post("/draft-resets/{request_id}/vote", response_model=DraftResetRequestOut)
def vote_on_draft_reset(request_id: int, payload: DraftResetVoteBody, background: BackgroundTasks,
                        db: Session = Depends(get_db), user: User = Depends(current_user)):
    """A manager approves or rejects. The reset happens when every manager has approved; one rejection ends it."""
    req = _load_request(db, request_id, user)
    try:
        draft_reset.vote(db, req, user.id, payload.vote)
    except draft_reset.ResetError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except PermissionError as e:
        db.rollback()
        raise HTTPException(403, str(e))
    db.commit()
    background.add_task(notifications.deliver_pending)
    db.refresh(req)
    return _request_out(db, req, user)


@router.post("/draft-resets/{request_id}/cancel", response_model=DraftResetRequestOut)
def cancel_draft_reset(request_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner: withdraws an open reset vote."""
    req = _load_request(db, request_id, user)
    try:
        draft_reset.cancel(db, req, user.id)
    except draft_reset.ResetError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except PermissionError as e:
        db.rollback()
        raise HTTPException(403, str(e))
    db.commit()
    return _request_out(db, req, user)
