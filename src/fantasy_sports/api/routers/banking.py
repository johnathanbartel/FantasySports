"""
NHL off-day banking endpoints (rules: platform/banking.py). Credits are ingested by the scoring side, never over
HTTP, so a commissioner cannot mint points; the API only lists, recommends, assigns and runs the 24-hour fallback.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_can_manage_team, assert_commissioner, current_user
from fantasy_sports.api.schemas import (
    BankAssignRequest, BankedCreditOut, BankedCreditsOut, BankFallbackOut, BankGameOptionOut, BankRecommendationOut,
)
from fantasy_sports.platform import banking, series_schedule
from fantasy_sports.platform.db.models.core import BankedCredit, Team, User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["banking"])

_STATUS = {"ALREADY_ASSIGNED": 409, "EXPIRED": 409, "GAME_HAS_CREDIT": 409, "NOT_FOUND": 404, "NOT_YOUR_GAME": 400}


def _fail(e: banking.BankingError) -> HTTPException:
    return HTTPException(_STATUS.get(e.code, 400), {"code": e.code, "message": str(e)})


def _out(c: BankedCredit) -> BankedCreditOut:
    return BankedCreditOut(
        id=c.id, league_id=c.league_id, team_id=c.team_id, draftable_asset_id=c.draftable_asset_id,
        player_name=c.draftable_asset.player.full_name if c.draftable_asset.player else "?",
        source_calendar_date=c.source_calendar_date, credit_points=c.credit_points, applied_points=c.applied_points,
        capped=banking.is_capped(c), status=c.status, status_reason=c.status_reason, assignment_mode=c.assignment_mode,
        assigned_daily_game_id=c.assigned_daily_game_id, assigned_at=c.assigned_at, created_at=c.created_at,
        fallback_at=banking.fallback_at(c), window_end=banking.window_end(c))


def _credit_for(db: Session, user: User, credit_id: int) -> BankedCredit:
    credit = db.get(BankedCredit, credit_id)
    if credit is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Credit not found"})
    assert_can_manage_team(db, user, credit.team_id)
    return credit


@router.get("/teams/{team_id}/banked-credits", response_model=BankedCreditsOut)
def list_banked_credits(team_id: int, status: str | None = None, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """The team's banked credits, newest first. Owner (or the commissioner acting for an AI team)."""
    team = assert_can_manage_team(db, user, team_id)
    if not series_schedule.league_is_nhl(db, team.league_id):
        raise HTTPException(400, {"code": "NOT_NHL", "message": "Banking is only available for NHL leagues"})
    if status is not None and status not in ("AVAILABLE", "ASSIGNED", "EXPIRED"):
        raise HTTPException(400, {"code": "BAD_STATUS", "message": "status must be AVAILABLE, ASSIGNED or EXPIRED"})
    q = db.query(BankedCredit).filter_by(team_id=team_id)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(BankedCredit.created_at.desc(), BankedCredit.id.desc()).all()
    return BankedCreditsOut(team_id=team_id, cap=banking.BANK_CAP, fallback_hours=banking.FALLBACK_HOURS,
                            credits=[_out(c) for c in rows])


@router.get("/banked-credits/{credit_id}/recommendation", response_model=BankRecommendationOut)
def recommend_game(credit_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    credit = _credit_for(db, user, credit_id)
    if credit.status != "AVAILABLE":
        raise HTTPException(409, {"code": "NOT_AVAILABLE", "message": f"This credit is {credit.status.lower()}"})
    rec = banking.recommend(db, credit, banking.utcnow())
    return BankRecommendationOut(
        credit=_out(credit), recommended_game_id=rec.recommended_game_id, confidence=rec.confidence, basis=rec.basis,
        reasons=rec.reasons, inputs_available=rec.inputs_available,
        options=[BankGameOptionOut(**o.__dict__) for o in rec.options])


@router.post("/banked-credits/{credit_id}/assign", response_model=BankedCreditOut)
def assign_credit(credit_id: int, payload: BankAssignRequest, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    """Assign the credit to one eligible game (the recommendation, or another). Final: no reassignment."""
    credit = _credit_for(db, user, credit_id)
    try:
        banking.assign(db, credit, payload.daily_game_id, banking.utcnow(), "MANUAL")
        db.commit()
    except banking.BankingError as e:
        db.rollback()
        raise _fail(e)
    except IntegrityError:
        db.rollback()   # a concurrent assignment won the unique key
        raise HTTPException(409, {"code": "GAME_HAS_CREDIT", "message": "That game already has a banked credit"})
    db.refresh(credit)
    return _out(credit)


@router.post("/leagues/{league_id}/banked-credits/process-fallback", response_model=BankFallbackOut)
def process_fallback(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner only. Auto-assigns (or expires) credits older than 24 hours. Safe to call repeatedly."""
    assert_commissioner(db, user, league_id)
    if not series_schedule.league_is_nhl(db, league_id):
        raise HTTPException(400, {"code": "NOT_NHL", "message": "Banking is only available for NHL leagues"})
    result = banking.process_fallback(db, banking.utcnow(), league_id)
    db.commit()
    return BankFallbackOut(processed_at=result.processed_at,
                           assigned=[{"credit_id": c, "daily_game_id": g} for c, g in result.assigned],
                           expired=result.expired, waiting=result.waiting)
