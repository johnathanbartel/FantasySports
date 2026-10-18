"""
Offline Draft Mode endpoints (commissioner console, web/static/offline-draft.html, and the read-only Big Screen feed).
All rules live in platform/offline_draft.py; every mutation runs under the session's lock and commits as one transaction.
Phones (join code + PIN) and the Excel round-trip are later phases.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, current_user
from fantasy_sports.platform import offline_draft as od
from fantasy_sports.platform.db.models.core import League, User
from fantasy_sports.platform.db.models.offline_draft import OfflineAuditLog, OfflineDraftSession
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["offline-draft"])


class CreateRequest(BaseModel):
    format: str
    entry_mode: str = "free"
    rounds: int | None = None
    team_order: list[int] | None = None
    budget: int | None = None
    min_bid: int = 1
    increment: int = 1


class PickRequest(BaseModel):
    asset_id: int
    round: int
    slot: int
    team_id: int | None = None
    keeper: bool = False


class SaleRequest(BaseModel):
    asset_id: int
    team_id: int
    price: int
    override: bool = False
    keeper: bool = False


class EditRequest(BaseModel):
    asset_id: int | None = None
    team_id: int | None = None
    price: int | None = None
    override: bool = False


class VoidRequest(BaseModel):
    reason: str | None = None


class StatusRequest(BaseModel):
    status: str


class ModeRequest(BaseModel):
    entry_mode: str


class CellOwnerRequest(BaseModel):
    round: int
    slot: int
    team_id: int


class FinalizeRequest(BaseModel):
    allow_incomplete: bool = False


def _get(db: Session, session_id: int, user: User) -> OfflineDraftSession:
    s = db.get(OfflineDraftSession, session_id)
    if s is None:
        raise HTTPException(404, "Offline draft not found")
    assert_commissioner(db, user, s.league_id)
    return s


def _mutate(db: Session, session_id: int, user: User, fn):
    """Runs fn(session) as the only writer: process lock + row lock, one commit. Refusals roll back."""
    _get(db, session_id, user)
    with od.session_lock(session_id):
        try:
            s = od.lock_session(db, session_id)
            result = fn(s)
            db.commit()
            return result, s
        except od.OfflineDraftError as e:
            db.rollback()
            raise HTTPException(409 if str(e).startswith("Already taken") else 400, str(e))


@router.post("/leagues/{league_id}/offline-draft")
def create(league_id: int, req: CreateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = assert_commissioner(db, user, league_id)
    try:
        s = od.create_session(db, league, user.id, req.format, req.entry_mode, req.rounds, req.team_order, req.budget,
                              req.min_bid, req.increment)
        db.commit()
    except od.OfflineDraftError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {**od.snapshot(db, s), "display_token": s.display_token}


@router.get("/leagues/{league_id}/offline-draft")
def current(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    assert_commissioner(db, user, league_id)
    s = db.query(OfflineDraftSession).filter_by(league_id=league_id).order_by(OfflineDraftSession.id.desc()).first()
    if s is None:
        raise HTTPException(404, "No offline draft for this league")
    return {**od.snapshot(db, s), "display_token": s.display_token}


@router.get("/offline-drafts/display/{token}")
def display(token: str, since: int | None = None, db: Session = Depends(get_db)):
    """Big Screen feed: read-only, keyed by the display token alone. The screen polls with since=<last version>: an
    unchanged draft answers with a tiny {changed: false}, so a 1-second poll costs almost nothing."""
    s = db.query(OfflineDraftSession).filter_by(display_token=token).first()
    if s is None:
        raise HTTPException(404, "Unknown display link")
    if since is not None and since == s.version:
        return {"changed": False, "version": s.version}
    return {**od.snapshot(db, s), "league_name": db.get(League, s.league_id).name}


@router.get("/offline-drafts/{session_id}")
def state(session_id: int, pool: bool = False, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = _get(db, session_id, user)
    return {**od.snapshot(db, s, include_pool=pool), "display_token": s.display_token}


@router.get("/offline-drafts/{session_id}/audit")
def audit_log(session_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _get(db, session_id, user)
    rows = db.query(OfflineAuditLog).filter_by(session_id=session_id).order_by(OfflineAuditLog.id.desc()).limit(200)
    return [{"id": r.id, "actor": r.actor, "action": r.action, "before": r.before_json, "after": r.after_json,
             "at": r.created_at.isoformat()} for r in rows]


@router.post("/offline-drafts/{session_id}/picks")
def pick(session_id: int, req: PickRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.make_pick(db, s, req.asset_id, req.round, req.slot, "commissioner",
                                                                 user.id, team_id=req.team_id, keeper=req.keeper))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/sales")
def sale(session_id: int, req: SaleRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.log_sale(db, s, req.asset_id, req.team_id, req.price, user.id,
                                                                override=req.override, keeper=req.keeper))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/picks/{pick_id}/void")
def void(session_id: int, pick_id: int, req: VoidRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.void_pick(db, s, pick_id, user.id, req.reason))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/picks/{pick_id}/edit")
def edit(session_id: int, pick_id: int, req: EditRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.edit_pick(db, s, pick_id, user.id, req.asset_id, req.team_id, req.price,
                                                                 req.override))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/undo")
def undo(session_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.undo_last(db, s, user.id))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/status")
def status(session_id: int, req: StatusRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.set_status(db, s, req.status, user.id))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/entry-mode")
def entry_mode(session_id: int, req: ModeRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.set_entry_mode(db, s, req.entry_mode, user.id))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/cell-owner")
def cell_owner(session_id: int, req: CellOwnerRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.set_cell_owner(db, s, req.round, req.slot, req.team_id, f"user:{user.id}"))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/skip-announcement")
def skip_announcement(session_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.skip_announcement(db, s, user.id))
    return od.snapshot(db, s)


@router.post("/offline-drafts/{session_id}/rotate-display-token")
def rotate(session_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    token, _ = _mutate(db, session_id, user, lambda s: od.rotate_display_token(db, s, user.id))
    return {"display_token": token}


@router.post("/offline-drafts/{session_id}/finalize")
def finalize(session_id: int, req: FinalizeRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    result, s = _mutate(db, session_id, user, lambda s: od.finalize(db, s, user.id, req.allow_incomplete))
    return {**od.snapshot(db, s), "result": result}


@router.post("/offline-drafts/{session_id}/reopen")
def reopen(session_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, s = _mutate(db, session_id, user, lambda s: od.reopen(db, s, user.id))
    return od.snapshot(db, s)
