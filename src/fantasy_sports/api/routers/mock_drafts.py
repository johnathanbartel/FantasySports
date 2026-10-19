"""Mock drafts: create one, list the public ones, join, and watch the lobby until the draft starts (rules in
platform/mock_drafts.py). The draft itself runs in the ordinary draft room (/static/draft-board.html)."""

import datetime
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import current_user, optional_user
from fantasy_sports.api.routers import draft as draft_router
from fantasy_sports.api.routers.leagues import AI_TEAM_NAMES
from fantasy_sports.api.schemas import DraftStartRequest
from fantasy_sports.platform import league_join, mock_drafts
from fantasy_sports.platform.db.models.core import Draft, League, LeagueUniverse, Team, Universe, User
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.defense_modes import league_pool, roster_size
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.league_features import draft_state
from fantasy_sports.platform.league_lifecycle import delete_league as purge_league
from fantasy_sports.platform.nfl_reference import seed_nfl_reference_data

router = APIRouter(prefix="/mock-drafts", tags=["mock-drafts"])
log = logging.getLogger(__name__)


class MockDraftCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    universe_code: str
    size: int = Field(default=12, ge=mock_drafts.MIN_TEAMS, le=mock_drafts.MAX_TEAMS)
    human_slots: int = Field(default=1, ge=1, le=mock_drafts.MAX_TEAMS)    # includes the creator
    start_at: datetime.datetime
    visibility: Literal["public", "private"] = "public"
    fill_ai_if_unfilled: bool = True        # human slots still empty at the start time become AI teams (else they're removed)
    pick_seconds: int = Field(default=mock_drafts.DEFAULT_PICK_SECONDS, ge=10, le=600)
    rounds: int | None = Field(default=None, ge=1, le=60)   # college only; other sports draft one round per roster spot


class MockTeamOut(BaseModel):
    id: int
    name: str
    kind: Literal["human", "open", "ai"]
    is_you: bool


class MockDraftOut(BaseModel):
    id: int
    name: str
    universe_code: str
    size: int
    rounds: int
    visibility: str
    start_at: datetime.datetime | None
    fill_ai_if_unfilled: bool
    pick_seconds: int | None
    status: Literal["lobby", "in_progress", "complete"]
    creator_name: str
    is_creator: bool
    humans_joined: int
    human_slots: int            # people already in plus unclaimed human slots
    open_slots: int
    my_team_id: int | None
    can_join: bool
    join_blocker: str | None
    teams: list[MockTeamOut]


class JoinRequest(BaseModel):
    team_name: str | None = Field(default=None, max_length=80)


_STATUS = {"not_started": "lobby", "in_progress": "in_progress", "complete": "complete"}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _get_mock(db: Session, league_id: int) -> League:
    league = db.get(League, league_id)
    if league is None or not league.is_mock_draft:
        raise HTTPException(404, "Mock draft not found")
    return league


def start(db: Session, league: League) -> bool:
    """Starts the draft now: unclaimed human slots are resolved by the league's policy, the order is shuffled and the pick
    clock set. False if it had already started (or can't start), True if this call started it. Commits."""
    league = db.query(League).filter(League.id == league.id).with_for_update().one()
    if db.query(Draft.id).filter(Draft.league_id == league.id).first() is not None:
        db.rollback()
        return False
    creator = db.get(User, league.commissioner_user_id)
    try:
        draft_router.start_draft(
            DraftStartRequest(league_id=league.id, pick_seconds=league.mock_pick_seconds, randomize_order=True), db, creator)
    except HTTPException as e:
        db.rollback()
        log.warning("Mock draft %s could not start: %s", league.id, e.detail)
        return False
    return True


def advance(db: Session, league: League, now: datetime.datetime | None = None) -> bool:
    """Starts the mock draft if its start time has passed. Nothing runs on a timer: this is called whenever the mock draft is
    read, so the lobby page (which polls) is what sets it going. True if it started."""
    now = now or _utcnow()
    if league.draft_scheduled_at is None or now < league.draft_scheduled_at:
        return False
    return start(db, league)


def _out(db: Session, league: League, user: User | None) -> MockDraftOut:
    teams = db.query(Team).filter(Team.league_id == league.id).order_by(Team.id).all()
    code = db.query(Universe.code).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id).filter(
        LeagueUniverse.league_id == league.id).scalar()
    creator = db.get(User, league.commissioner_user_id)
    status = _STATUS[draft_state(db, league.id)]
    mine = next((t for t in teams if user is not None and t.owner_user_id == user.id), None)
    options, reason = league_join.joinable_teams(db, league, user.id if user else None)
    kinds = [mock_drafts.team_kind(t) for t in teams]
    return MockDraftOut(
        id=league.id, name=league.name, universe_code=code or "", size=league.size, rounds=league.draft_rounds,
        visibility=league.visibility, start_at=league.draft_scheduled_at,
        fill_ai_if_unfilled=league.unfilled_slot_policy == "fill_ai", pick_seconds=league.mock_pick_seconds, status=status,
        creator_name=creator.display_name if creator else "",
        is_creator=user is not None and user.id == league.commissioner_user_id,
        humans_joined=kinds.count("human"), human_slots=kinds.count("human") + kinds.count("open"),
        open_slots=kinds.count("open"), my_team_id=mine.id if mine else None, can_join=bool(options), join_blocker=reason,
        teams=[MockTeamOut(id=t.id, name=t.name, kind=k, is_you=mine is not None and t.id == mine.id)
               for t, k in zip(teams, kinds)])


@router.post("", response_model=MockDraftOut)
def create_mock_draft(payload: MockDraftCreate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    now = _utcnow()
    try:
        start_at = mock_drafts.normalize_start(payload.start_at, now)
        mock_drafts.validate_shape(payload.size, payload.human_slots)
    except mock_drafts.MockDraftError as e:
        raise HTTPException(400, str(e))
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Give the mock draft a name")
    try:
        if payload.universe_code == "nfl":
            seed_nfl_reference_data(db)
        elif payload.universe_code == "nhl":
            seed_hockey_reference_data(db)
    except RuntimeError as e:
        raise HTTPException(400, f"Reference data conflict: {e}")
    universe = db.query(Universe).filter(Universe.code == payload.universe_code).first()
    if universe is None:
        raise HTTPException(400, f"Unknown sport '{payload.universe_code}'")

    spots = roster_size(db, universe.id, "group")
    if universe.level == "college":
        rounds = payload.rounds or spots or 20
    else:
        rounds = spots or payload.rounds or 20       # every roster spot is drafted outside college
    league = mock_drafts.create(
        db, creator=user, name=name, universe=universe, size=payload.size, human_slots=payload.human_slots, rounds=rounds,
        start_at=start_at, visibility=payload.visibility, fill_ai_if_unfilled=payload.fill_ai_if_unfilled,
        pick_seconds=payload.pick_seconds, ai_names=AI_TEAM_NAMES)
    pool = len(league_pool(db, league.id))
    if pool < payload.size * rounds:
        db.rollback()
        raise HTTPException(400, f"The {universe.display_name} player pool has {pool} players but {payload.size} teams x "
                                 f"{rounds} rounds need {payload.size * rounds}. Use fewer teams.")
    db.commit()
    db.refresh(league)
    return _out(db, league, user)


@router.get("/public", response_model=list[MockDraftOut])
def list_public(db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    """Public mock drafts that haven't started and still have a human slot open."""
    out = []
    for league in db.query(League).filter(League.is_mock_draft.is_(True), League.visibility == "public").order_by(
            League.draft_scheduled_at, League.id).all():
        if advance(db, league):
            continue
        row = _out(db, league, user)
        if row.status == "lobby" and row.open_slots > 0:
            out.append(row)
    return out


@router.get("/mine", response_model=list[MockDraftOut])
def list_mine(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Mock drafts the caller created or is in, newest first."""
    owned = {t.league_id for t in db.query(Team).filter(Team.owner_user_id == user.id)}
    rows = db.query(League).filter(
        League.is_mock_draft.is_(True), (League.commissioner_user_id == user.id) | (League.id.in_(owned or {0}))
    ).order_by(League.id.desc()).limit(25).all()
    for league in rows:
        advance(db, league)
    return [_out(db, league, user) for league in rows]


@router.get("/{league_id}", response_model=MockDraftOut)
def get_mock_draft(league_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    """The lobby: who is in, how many human slots are left, and the status. Reading it past the start time starts the draft."""
    league = _get_mock(db, league_id)
    advance(db, league)
    db.refresh(league)
    return _out(db, league, user)


@router.post("/{league_id}/join", response_model=MockDraftOut)
def join_mock_draft(league_id: int, payload: JoinRequest | None = None, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    league = _get_mock(db, league_id)
    if advance(db, league):
        raise HTTPException(409, "This mock draft has just started.")
    options, reason = league_join.joinable_teams(db, league, user.id)
    if not options:
        raise HTTPException(409, reason or "There is no open human slot.")
    try:
        league_join.join(db, league, user.id, options[0][0].id, payload.team_name if payload else None)
    except ValueError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    db.commit()
    return _out(db, league, user)


@router.post("/{league_id}/leave", response_model=MockDraftOut)
def leave_mock_draft(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    league = _get_mock(db, league_id)
    if draft_state(db, league.id) != "not_started":
        raise HTTPException(409, "The draft has started; the slot can't be given back.")
    try:
        mock_drafts.leave(db, league, user.id)
    except mock_drafts.MockDraftError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return _out(db, league, user)


@router.post("/{league_id}/start", response_model=MockDraftOut)
def start_mock_draft_now(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Creator only: starts the draft before its start time. Unclaimed human slots are resolved as at the start time."""
    league = _get_mock(db, league_id)
    if league.commissioner_user_id != user.id:
        raise HTTPException(403, "Only the creator can start the mock draft")
    if not start(db, league):
        raise HTTPException(409, "The mock draft has already started or couldn't start")
    db.refresh(league)
    return _out(db, league, user)


@router.delete("/{league_id}")
def cancel_mock_draft(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Creator only: deletes the mock draft, started or not."""
    league = _get_mock(db, league_id)
    if league.commissioner_user_id != user.id:
        raise HTTPException(403, "Only the creator can cancel the mock draft")
    purge_league(db, league.id)
    db.commit()
    return {"deleted": league_id}
