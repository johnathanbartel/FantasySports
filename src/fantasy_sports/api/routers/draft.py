import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, assert_owns_team, current_user, optional_user
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.schemas import (
    DraftBoardAsset,
    DraftClockOut,
    DraftOut,
    DraftOvernightOut,
    DraftPickOut,
    DraftProcessOut,
    DraftStartRequest,
    MakePickRequest,
)
from fantasy_sports.platform import draft_clock
from fantasy_sports.platform.draft_stats import stats_by_asset
from fantasy_sports.platform.db.models.core import (
    Draft,
    DraftableAsset,
    DraftPick,
    League,
    LeagueUniverse,
    RosterEntry,
    Team,
    User,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.defense_modes import in_pool, league_modes, league_pool
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset

router = APIRouter(prefix="/drafts", tags=["draft"])


def utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _pick_out(p: DraftPick) -> DraftPickOut:
    return DraftPickOut(
        id=p.id,
        round=p.round,
        pick_number=p.pick_number,
        team_id=p.team_id,
        draftable_asset_id=p.draftable_asset_id,
        display_name=draftable_asset_display_name(p.draftable_asset) if p.draftable_asset else None,
        picked_at=p.picked_at,
        is_autopick=p.is_autopick,
    )


@router.post("/start", response_model=DraftOut)
def start_draft(payload: DraftStartRequest, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    league = db.get(League, payload.league_id)
    if league is None:
        raise HTTPException(404, "League not found")

    teams = db.query(Team).filter(Team.league_id == league.id).order_by(Team.id).all()
    if not teams:
        raise HTTPException(400, "League has no teams yet")
    # TODO: real draft-order randomization/settings. v1 uses team.id order.
    if db.query(Draft).filter(Draft.league_id == league.id, Draft.status == "in_progress").first():
        raise HTTPException(409, "This league already has a draft in progress")
    if not league_pool(db, league.id):
        raise HTTPException(
            400,
            "This league's player pool is empty - ingest the universe's rosters first "
            "(for NFL: scripts/ingest_nfl_rosters_and_stats.py)",
        )

    timed = payload.pick_seconds is not None or payload.overnight_start or payload.overnight_end
    now = utcnow()
    draft = Draft(league_id=league.id, status="in_progress", started_at=now)
    if timed:
        # A pick clock changes how the draft runs for everyone, so only the commissioner may set one. The original
        # untimed start stays open exactly as before.
        if user is None:
            raise HTTPException(401, "Sign in as the commissioner to start a timed draft")
        assert_commissioner(db, user, league.id)
        try:
            draft_clock.configure(draft, payload.pick_seconds, draft_clock.parse_hhmm(payload.overnight_start),
                                  draft_clock.parse_hhmm(payload.overnight_end), payload.utc_offset_minutes)
        except draft_clock.ClockError as e:
            raise HTTPException(400, str(e))
    db.add(draft)
    db.flush()
    draft_clock.begin_pick(draft, now)

    pick_number = 1
    for round_number in range(1, league.draft_rounds + 1):
        order = teams if round_number % 2 == 1 else list(reversed(teams))
        for team in order:
            db.add(
                DraftPick(
                    draft_id=draft.id,
                    round=round_number,
                    pick_number=pick_number,
                    team_id=team.id,
                )
            )
            pick_number += 1

    db.commit()
    db.refresh(draft)
    return draft


@router.get("/league/{league_id}", response_model=DraftOut)
def get_league_draft(league_id: int, db: Session = Depends(get_db)):
    """The league's newest draft, so a page can find it from the league id alone."""
    draft = db.query(Draft).filter(Draft.league_id == league_id).order_by(Draft.id.desc()).first()
    if draft is None:
        raise HTTPException(404, "This league has no draft yet")
    return draft


@router.get("/{draft_id}", response_model=DraftOut)
def get_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(404, "Draft not found")
    return draft


@router.get("/{draft_id}/picks", response_model=list[DraftPickOut])
def get_pick_history(draft_id: int, db: Session = Depends(get_db)):
    picks = (
        db.query(DraftPick)
        .filter(DraftPick.draft_id == draft_id)
        .order_by(DraftPick.pick_number)
        .all()
    )
    return [_pick_out(p) for p in picks]


@router.get("/{draft_id}/board", response_model=list[DraftBoardAsset])
def get_draft_board(draft_id: int, db: Session = Depends(get_db)):
    """Draftable assets in the draft's league universe(s) not yet picked in
    this draft."""
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(404, "Draft not found")

    already_picked = {
        p.draftable_asset_id
        for p in db.query(DraftPick).filter(
            DraftPick.draft_id == draft_id, DraftPick.draftable_asset_id.isnot(None)
        )
    }
    points = season_points_by_asset(db)  # best last-season scorers first; unscored players A-Z after
    available = [
        (a, draftable_asset_display_name(a)) for a in league_pool(db, draft.league_id) if a.id not in already_picked
    ]
    available.sort(key=lambda pair: (-points.get(pair[0].id, -1.0), pair[1]))
    stats = stats_by_asset(db, [a.id for a, _ in available if a.asset_type == "player"])
    return [
        DraftBoardAsset(
            draftable_asset_id=a.id,
            asset_type=a.asset_type,
            display_name=name,
            position=draftable_asset_position(a),
            **stats.get(a.id, {}),
        )
        for a, name in available
    ]


def _close_if_finished(db: Session, draft_id: int) -> None:
    if _next_open_pick(db, draft_id) is None:
        draft = db.get(Draft, draft_id)
        draft.status = "complete"
        draft.completed_at = datetime.datetime.utcnow()
        draft_clock.clear(draft)


def _next_open_pick(db: Session, draft_id: int) -> DraftPick | None:
    return (
        db.query(DraftPick)
        .filter(DraftPick.draft_id == draft_id, DraftPick.draftable_asset_id.is_(None))
        .order_by(DraftPick.pick_number)
        .first()
    )


def _lock_draft(db: Session, draft_id: int) -> Draft:
    """The draft row, locked for the rest of the transaction (a no-op on SQLite) so that two requests cannot both
    process the same overdue pick."""
    draft = db.query(Draft).filter(Draft.id == draft_id).with_for_update().one_or_none()
    if draft is None:
        raise HTTPException(404, "Draft not found")
    return draft


def _fill_with_best_available(db: Session, draft: Draft, pick: DraftPick, picked_at: datetime.datetime) -> None:
    """Deterministic autopick: the first asset on the board (best last-season points, then name)."""
    board = get_draft_board(draft.id, db)
    if not board:
        raise HTTPException(400, "No draftable assets left")
    pick.draftable_asset_id = board[0].draftable_asset_id
    pick.picked_at = picked_at
    pick.is_autopick = True
    db.add(RosterEntry(team_id=pick.team_id, draftable_asset_id=pick.draftable_asset_id, acquired_via="draft"))
    db.flush()
    _close_if_finished(db, draft.id)


def process_overdue(db: Session, draft: Draft, now: datetime.datetime) -> list[DraftPick]:
    """Make every pick whose clock has run out, in order. Each autopick is stamped with its own deadline and starts
    the next pick's clock from that deadline, so the outcome is identical whether this runs the moment time runs out
    or days later (restart-safe), and running it twice changes nothing. Flushes, does not commit."""
    made: list[DraftPick] = []
    while draft_clock.is_overdue(draft, now):
        pick = _next_open_pick(db, draft.id)
        if pick is None:
            break
        deadline = draft.deadline_at
        _fill_with_best_available(db, draft, pick, deadline)
        made.append(pick)
        if draft.status == "in_progress":
            draft_clock.begin_pick(draft, deadline)
    return made


def _clock_out(db: Session, draft: Draft, now: datetime.datetime) -> DraftClockOut:
    window = draft_clock.window_of(draft)
    in_window = bool(window and window.contains(now))
    pick = _next_open_pick(db, draft.id) if draft.status == "in_progress" else None
    if draft.pick_seconds is None:
        status = "untimed" if draft.status != "complete" else "complete"
    elif draft.status == "complete":
        status = "complete"
    elif draft.paused:
        status = "paused"
    elif in_window:
        status = "overnight"
    else:
        status = "running"
    resumes = None
    if in_window and not draft.paused:
        resumes = now + datetime.timedelta(seconds=window.seconds_until_end(now))
    return DraftClockOut(
        draft_id=draft.id, status=draft.status, clock_status=status, mode=draft_clock.mode(draft),
        pick_seconds=draft.pick_seconds, paused=bool(draft.paused), server_time=now, deadline_at=draft.deadline_at,
        remaining_seconds=draft_clock.remaining_seconds(draft, now),
        overnight=DraftOvernightOut(start=draft_clock.hhmm(draft.overnight_start_minute),
                                    end=draft_clock.hhmm(draft.overnight_end_minute),
                                    utc_offset_minutes=draft.utc_offset_minutes or 0) if window else None,
        overnight_active=in_window, resumes_at=resumes, pick_number=pick.pick_number if pick else None,
        on_clock_team_id=pick.team_id if pick else None)


@router.get("/{draft_id}/clock", response_model=DraftClockOut)
def get_clock(draft_id: int, db: Session = Depends(get_db)):
    """The pick clock. Applies any overdue autopicks first (idempotent), so a page that only polls this still sees the
    draft move when time runs out."""
    draft = _lock_draft(db, draft_id)
    now = utcnow()
    process_overdue(db, draft, now)
    db.commit()
    return _clock_out(db, draft, now)


@router.post("/{draft_id}/process-overdue", response_model=DraftProcessOut)
def process_overdue_picks(draft_id: int, db: Session = Depends(get_db)):
    """Make the picks whose time has run out. Needs no sign-in: it only does what the clock has already decided, in a
    fixed order, and repeating it is a no-op."""
    draft = _lock_draft(db, draft_id)
    now = utcnow()
    made = process_overdue(db, draft, now)
    db.commit()
    return DraftProcessOut(processed=len(made), picks=[_pick_out(p) for p in made], clock=_clock_out(db, draft, now))


@router.post("/{draft_id}/pause", response_model=DraftClockOut)
def pause_draft(draft_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner only. Freezes the remaining time of the current pick; idempotent."""
    draft = _lock_draft(db, draft_id)
    assert_commissioner(db, user, draft.league_id)
    now = utcnow()
    process_overdue(db, draft, now)   # a pick already out of time is made first; a pause cannot revive it
    try:
        draft_clock.pause(draft, now)
    except draft_clock.ClockError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    db.commit()
    return _clock_out(db, draft, now)


@router.post("/{draft_id}/resume", response_model=DraftClockOut)
def resume_draft(draft_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner only. Restarts the clock with the time that was frozen; idempotent."""
    draft = _lock_draft(db, draft_id)
    assert_commissioner(db, user, draft.league_id)
    now = utcnow()
    try:
        draft_clock.resume(draft, now)
    except draft_clock.ClockError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    db.commit()
    return _clock_out(db, draft, now)


@router.post("/{draft_id}/pick", response_model=DraftPickOut)
def make_pick(
    draft_id: int, payload: MakePickRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Fills the next open pick slot with the given asset.

    The caller must own the team on the clock, and the asset must not already
    be rostered by another team in the league.
    """
    draft = _lock_draft(db, draft_id)
    now = utcnow()
    made = process_overdue(db, draft, now)
    if made:
        db.commit()
        raise HTTPException(409, "Your pick time ran out, so the pick was made automatically")
    if draft.paused:
        raise HTTPException(409, "The draft is paused")
    pick = _next_open_pick(db, draft_id)
    if pick is None:
        raise HTTPException(400, "Draft has no open picks left")
    team_on_clock = assert_owns_team(db, user, pick.team_id)

    asset = db.get(DraftableAsset, payload.draftable_asset_id)
    if asset is None:
        raise HTTPException(404, "Draftable asset not found")
    if not in_pool(asset, league_modes(db, team_on_clock.league_id)):
        raise HTTPException(400, "That asset isn't in this league's defense mode")

    already_rostered = (
        db.query(RosterEntry.id)
        .join(Team, Team.id == RosterEntry.team_id)
        .filter(
            RosterEntry.draftable_asset_id == asset.id,
            Team.league_id == team_on_clock.league_id,
        )
        .first()
    )
    if already_rostered is not None:
        raise HTTPException(409, "Draftable asset is already rostered in this league")

    pick.draftable_asset_id = asset.id
    pick.picked_at = now
    db.add(RosterEntry(team_id=pick.team_id, draftable_asset_id=asset.id, acquired_via="draft"))
    db.flush()
    _close_if_finished(db, draft_id)
    if draft.status == "in_progress":
        draft_clock.begin_pick(draft, now)
    db.commit()
    db.refresh(pick)
    return _pick_out(pick)


@router.post("/{draft_id}/autopick", response_model=DraftPickOut)
def autopick(draft_id: int, db: Session = Depends(get_db)):
    """Fills the next open pick with the first available board asset.

    TODO: real AI personality-driven pick logic (Team.personality_profile
    isn't consulted at all yet) - this is a placeholder so AI-owned teams
    don't stall a draft, not the actual trait system.

    On a timed draft this is limited to what the clock allows: an AI-owned team may be picked for at any time (the
    draft page uses that to run AI teams), a human team only once its time has run out, and nothing while paused.
    """
    draft = _lock_draft(db, draft_id)
    now = utcnow()
    if draft_clock.is_timed(draft):
        made = process_overdue(db, draft, now)
        if made:
            db.commit()
            return _pick_out(made[-1])
        if draft.paused:
            raise HTTPException(409, "The draft is paused")
    pick = _next_open_pick(db, draft_id)
    if pick is None:
        raise HTTPException(400, "Draft has no open picks left")
    if draft_clock.is_timed(draft) and not db.get(Team, pick.team_id).is_ai_owned:
        raise HTTPException(409, "That team still has time on the clock")

    _fill_with_best_available(db, draft, pick, now)
    if draft.status == "in_progress":
        draft_clock.begin_pick(draft, now)
    db.commit()
    db.refresh(pick)
    return _pick_out(pick)
