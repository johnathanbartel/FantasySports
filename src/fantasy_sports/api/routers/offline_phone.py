"""
Offline Draft Mode, phone side (web/static/join.html): join with a code, claim a team with its PIN, then pick / nominate / bid.

No login: a phone holds a device token for one (session, team), sent as X-Device-Token. Managers can only act for their own
team, and only when the rules allow it (see platform/offline_live.py). Every mutation runs under the session lock.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasy_sports.api.routers.offline_draft import refresh
from fantasy_sports.platform import offline_draft as od
from fantasy_sports.platform import offline_live as live
from fantasy_sports.platform.db.models.core import DraftableAsset
from fantasy_sports.platform.db.models.offline_draft import OfflineDraftSession, OfflineTeamClaim
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["offline-phone"])


class ClaimRequest(BaseModel):
    team_id: int
    pin: str | None = None


class AssetRequest(BaseModel):
    asset_id: int
    opening_bid: int | None = None


class BidRequest(BaseModel):
    amount: int


class QueueRequest(BaseModel):
    asset_ids: list[int]


def _session_by_code(db: Session, code: str) -> OfflineDraftSession:
    s = db.query(OfflineDraftSession).filter_by(join_code=code.strip().upper()).first()
    if s is None or s.status == "finalized":
        raise HTTPException(404, "No draft is open with that code")
    return s


def device(x_device_token: str | None = Header(default=None), db: Session = Depends(get_db)) -> OfflineTeamClaim:
    claim = live.find_claim(db, x_device_token)
    if claim is None:
        raise HTTPException(401, "This device is not signed in to a team (it may have been replaced)")
    s = db.get(OfflineDraftSession, claim.session_id)
    if s.status == "finalized":
        raise HTTPException(401, "This draft is finished")
    return claim


def _mutate(db: Session, claim: OfflineTeamClaim, fn):
    if claim.status != "claimed":
        raise HTTPException(403, "Waiting for the commissioner to approve this device")
    sid, team = claim.session_id, claim.team_id
    with od.session_lock(sid):
        try:
            s = od.lock_session(db, sid)
            if live.tick(db, s):
                db.commit()
                s = od.lock_session(db, sid)
            result = fn(s)
            db.commit()
            return result
        except od.OfflineDraftError as e:
            db.rollback()
            msg = str(e)
            raise HTTPException(409 if msg.startswith("Already taken") else 400, msg)


@router.get("/offline-join/{code}")
def join_info(code: str, db: Session = Depends(get_db)):
    return live.join_info(db, _session_by_code(db, code))


@router.post("/offline-join/{code}/claim")
def claim(code: str, req: ClaimRequest, request: Request, db: Session = Depends(get_db)):
    s = _session_by_code(db, code)
    try:
        live.rate_limit(request.client.host if request.client else "?", s.join_code)
    except live.RateLimited as e:
        raise HTTPException(429, str(e))
    with od.session_lock(s.id):
        try:
            s = od.lock_session(db, s.id)
            token, status = live.claim_team(db, s, req.team_id, req.pin)
            db.commit()
        except live.PinRejected as e:
            db.commit()                                   # the failed-attempt counter (and any lock) must stick
            raise HTTPException(403, str(e))
        except od.OfflineDraftError as e:
            db.rollback()
            raise HTTPException(400, str(e))
    return {"token": token, "status": status, "team_id": req.team_id}


@router.get("/offline-phone/state")
def state(since: int | None = None, db: Session = Depends(get_db), claim: OfflineTeamClaim = Depends(device)):
    if claim.status != "claimed":
        return {"me": {"status": claim.status, "team_id": claim.team_id}, "pending": True}
    refresh(db, claim.session_id)
    db.expire_all()
    s = db.get(OfflineDraftSession, claim.session_id)
    if since is not None and since == s.version:
        return {"changed": False, "version": s.version, "live": live.live_info(db, s)}
    return live.phone_view(db, s, db.get(OfflineTeamClaim, claim.id))


@router.get("/offline-phone/pool")
def pool(q: str = "", position: str = "", db: Session = Depends(get_db), claim: OfflineTeamClaim = Depends(device)):
    from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
    s = db.get(OfflineDraftSession, claim.session_id)
    taken = {p.draftable_asset_id for p in od.live_picks(db, s.id)}
    ids = [a for a in s.settings_json["pool"] if a not in taken]
    out = []
    for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_(ids or [0])):
        name, pos = draftable_asset_display_name(a), draftable_asset_position(a)
        if (not position or pos == position) and (not q or q.lower() in name.lower()):
            out.append({"asset_id": a.id, "name": name, "position": pos})
    out.sort(key=lambda x: x["name"])
    return out[:50]


@router.post("/offline-phone/pick")
def pick(req: AssetRequest, db: Session = Depends(get_db), claim: OfflineTeamClaim = Depends(device)):
    def go(s):
        cell = od.on_the_clock(db, s)
        if cell is None:
            raise od.OfflineDraftError("The board is full")
        if od.cell_owner(s, *cell) != claim.team_id:
            raise od.OfflineDraftError("It is not your pick")
        od.make_pick(db, s, req.asset_id, cell[0], cell[1], "phone", None, device=str(claim.id), enforce_turn=True,
                     actor=f"team:{claim.team_id}")
    _mutate(db, claim, go)
    return {"ok": True}


@router.post("/offline-phone/nominate")
def nominate(req: AssetRequest, db: Session = Depends(get_db), claim: OfflineTeamClaim = Depends(device)):
    def go(s):
        if s.entry_mode != "live":
            raise od.OfflineDraftError("Phones can't nominate in free-entry mode")
        live.nominate(db, s, claim.team_id, req.asset_id, req.opening_bid)
    _mutate(db, claim, go)
    return {"ok": True}


@router.post("/offline-phone/bid")
def bid(req: BidRequest, db: Session = Depends(get_db), claim: OfflineTeamClaim = Depends(device)):
    def go(s):
        if s.entry_mode != "live":
            raise od.OfflineDraftError("Phones can't bid in free-entry mode")
        live.bid(db, s, claim.team_id, req.amount)
    _mutate(db, claim, go)
    return {"ok": True}


@router.post("/offline-phone/queue")
def queue(req: QueueRequest, db: Session = Depends(get_db), claim: OfflineTeamClaim = Depends(device)):
    def go(s):
        live.set_queue(db, s, db.get(OfflineTeamClaim, claim.id), req.asset_ids)
    _mutate(db, claim, go)
    return {"ok": True}
