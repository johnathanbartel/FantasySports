import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from fantasy_sports.api.auth import (
    assert_commissioner, assert_owns_team, current_user, optional_user, require_commissioner_mode,
)
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.platform import season
from fantasy_sports.platform.ai import recruiting_ai
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    League,
    RecruitingAward,
    RecruitingBid,
    RecruitingBidResult,
    RecruitingCharge,
    RealTeam,
    RecruitingDraft,
    RecruitingResultAck,
    Team,
    User,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.recruiting import engine, simulate
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset

router = APIRouter(prefix="/recruiting", tags=["recruiting"])


class DraftStatus(BaseModel):
    league_id: int
    status: str  # "scheduled" (not started yet) / "open" / "closed"
    draft_id: int | None
    scheduled_start: datetime.datetime | None
    started_at: datetime.datetime | None
    ended_at: datetime.datetime | None
    period_seconds: int
    total_periods: int
    current_period: int | None     # the period bids are being collected for (the next one, during a buffer)
    period_ends_at: datetime.datetime | None  # when the current period's bids get processed (None during a buffer)
    buffer_seconds: int
    # "scheduled" / "bidding" / "buffer" (between periods: results shown, no bids) / "closed"
    phase: str
    next_period_starts_at: datetime.datetime | None  # set during a buffer


class StartRequest(BaseModel):
    league_id: int


class BidRequest(BaseModel):
    team_id: int
    draftable_asset_id: int
    amount: int  # 0 retracts the bid


class BidResult(BaseModel):
    draftable_asset_id: int
    amount: int
    current_period: int
    period_ends_at: datetime.datetime | None


class PoolAsset(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    # Last season's fantasy points, ESPN standard scoring (see
    # sports/football/college/season_scoring.py). None for defense groups and
    # for players with no stat line.
    last_season_points: float | None


class AwardOut(BaseModel):
    draftable_asset_id: int
    team_id: int
    period_number: int
    # What the team bid to get him. Shown only to the team's owner - everyone
    # else sees who has whom, never what it cost.
    price: int | None
    reason: str
    was_tie_broken_randomly: bool


class MyBidOut(BaseModel):
    team_id: int
    draftable_asset_id: int
    amount: int


class MyChargeOut(BaseModel):
    team_id: int
    draftable_asset_id: int
    period_number: int
    amount: int
    is_winner: bool


class TeamFinanceOut(BaseModel):
    team_id: int
    weekly_budget: int
    weeks_funded: int   # weeks of budget accrued so far (the current week counts)
    available: int      # weekly_budget x weeks_funded - the money accumulates
    spent: int  # charged for bids on players who were awarded, won or lost
    lost_on_bids: int  # the part of spent that went to players the team didn't get
    remaining: int      # available - spent
    # Money tied up in the team's own standing bids, and what it can still
    # bid (remaining - committed). Private: only the team's owner sees them.
    committed: int | None
    spendable: int | None
    roster_count: int
    # The budget panel. remaining = "Recruiting Budget (This Week)" (weekly x weeks so far - charged);
    # spendable = "Budget Remaining (This Week)" (remaining - all standing bids).
    recruiting_budget_total: int       # weekly budget x the weeks of recruiting
    season_budget_total: int           # weekly budget x the season's weeks + recruiting_budget_total
    roster_cost: int | None            # what the team paid for players on its roster (owner only)
    season_weekly_budget: int | None   # (season_budget_total - roster_cost) / season weeks (owner only)


class StateOut(BaseModel):
    status: str
    draft_id: int
    current_period: int
    total_periods: int
    period_seconds: int
    buffer_seconds: int
    phase: str
    next_period_starts_at: datetime.datetime | None
    period_ends_at: datetime.datetime | None
    started_at: datetime.datetime
    ended_at: datetime.datetime | None
    season_weeks: int
    playoff_weeks: int
    awards: list[AwardOut]
    my_bids: list[MyBidOut]
    my_charges: list[MyChargeOut]
    teams: list[TeamFinanceOut]


def _phase(draft: RecruitingDraft) -> str:
    if draft.status != "open":
        return "closed"
    return "buffer" if engine.in_buffer(draft) else "bidding"


def _timing(draft: RecruitingDraft) -> dict:
    """period_ends_at / next_period_starts_at for the phase the draft is in."""
    phase = _phase(draft)
    return {
        "phase": phase,
        "period_ends_at": engine.period_end(draft, draft.periods_processed + 1) if phase == "bidding" else None,
        "next_period_starts_at": engine.period_start(draft, draft.periods_processed + 1) if phase == "buffer" else None,
    }


def _status(db: Session, league_id: int) -> DraftStatus:
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    draft = engine.sync(db, league_id)
    db.commit()
    if draft is None:
        return DraftStatus(
            league_id=league_id, status="scheduled", draft_id=None, scheduled_start=league.draft_scheduled_at,
            started_at=None, ended_at=None, period_seconds=engine.period_length(db, league_id),
            total_periods=league.draft_rounds, current_period=None, period_ends_at=None,
            buffer_seconds=engine.buffer_length(db, league_id), phase="scheduled", next_period_starts_at=None,
        )
    return DraftStatus(
        league_id=league_id, status=draft.status, draft_id=draft.id, scheduled_start=league.draft_scheduled_at,
        started_at=draft.started_at, ended_at=draft.ended_at, period_seconds=draft.period_seconds,
        total_periods=draft.total_periods, current_period=engine.current_period(draft),
        buffer_seconds=draft.buffer_seconds, **_timing(draft),
    )


@router.get("/league/{league_id}", response_model=DraftStatus)
def league_status(league_id: int, db: Session = Depends(get_db)):
    """Where the league's recruiting draft stands. Opens it lazily once the
    scheduled start time (set at league creation) has passed, and processes
    any period that has ended since the last call."""
    return _status(db, league_id)


@router.post("/start", response_model=DraftStatus)
def start_now(payload: StartRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner override: open period 1 now instead of waiting for the
    scheduled start time (handy for testing)."""
    assert_commissioner(db, user, payload.league_id)
    try:
        engine.open_draft(db, payload.league_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _status(db, payload.league_id)


def _draft(db: Session, draft_id: int) -> RecruitingDraft:
    draft = db.get(RecruitingDraft, draft_id)
    if draft is None:
        raise HTTPException(404, "Recruiting draft not found")
    return draft


@router.post("/league/{league_id}/simulate", response_model=DraftStatus)
def simulate_draft(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """COMMISSIONER MODE (premium): plays out the league's whole recruiting draft
    instantly - real AI bids, real week-by-week processing, real charges - with
    only the waiting skipped. All-AI leagues only. Starts the draft if it hasn't
    started, or finishes it from wherever it is."""
    assert_commissioner(db, user, league_id)
    require_commissioner_mode(db, user)
    try:
        simulate.simulate_draft(db, league_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _status(db, league_id)


@router.post("/{draft_id}/process-period", response_model=DraftStatus)
def process_period(draft_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner action: end the current period now and process its bids
    (skips the time remaining in it)."""
    draft = _draft(db, draft_id)
    assert_commissioner(db, user, draft.league_id)
    try:
        engine.process_period_now(db, draft_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _status(db, draft.league_id)


@router.post("/{draft_id}/end", response_model=DraftStatus)
def end_draft(draft_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner action: end the whole draft now. The current period is
    processed as the final one, so every player with bids goes to his highest
    bidder."""
    draft = _draft(db, draft_id)
    assert_commissioner(db, user, draft.league_id)
    try:
        engine.end_draft(db, draft_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _status(db, draft.league_id)


@router.get("/{draft_id}/pool", response_model=list[PoolAsset])
def pool(draft_id: int, db: Session = Depends(get_db)):
    """Every player and defense group that can be bid on (one shared pool)."""
    draft = _draft(db, draft_id)
    ids = engine.pool_asset_ids(db, draft.league_id)
    assets = db.query(DraftableAsset).options(
        joinedload(DraftableAsset.player), joinedload(DraftableAsset.defense_group)
    ).filter(DraftableAsset.id.in_(ids)).all()
    points = season_points_by_asset(db)
    school = {t.id: t.name for t in db.query(RealTeam)}
    return [
        PoolAsset(
            draftable_asset_id=a.id, display_name=draftable_asset_display_name(a),
            position=draftable_asset_position(a),
            real_team=school.get(a.player.real_team_id if a.player else a.defense_group.real_team_id),
            last_season_points=points.get(a.id),
        )
        for a in assets
    ]


@router.get("/{draft_id}/state", response_model=StateOut)
def state(draft_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    """Live state: who has been recruited (public - which team has whom and in
    which period, but only your own team's bid amounts), the caller's own standing bids (other teams' bids
    and every hidden price stay private), and each team's budget position."""
    draft = _draft(db, draft_id)
    engine.sync(db, draft.league_id)
    db.commit()
    awards = db.query(RecruitingAward).filter_by(draft_id=draft_id).order_by(RecruitingAward.id).all()
    my_bids: list[MyBidOut] = []
    my_charges: list[MyChargeOut] = []
    own: list[int] = []
    if user is not None:
        own = [t.id for t in db.query(Team).filter(Team.league_id == draft.league_id, Team.owner_user_id == user.id)]
        taken = {a.draftable_asset_id for a in awards}
        my_bids = [
            MyBidOut(team_id=b.team_id, draftable_asset_id=b.draftable_asset_id, amount=b.amount)
            for b in db.query(RecruitingBid).filter(
                RecruitingBid.draft_id == draft_id, RecruitingBid.team_id.in_(own))
            if b.draftable_asset_id not in taken
        ]
        my_charges = [
            MyChargeOut(team_id=c.team_id, draftable_asset_id=c.draftable_asset_id,
                        period_number=c.period_number, amount=c.amount, is_winner=c.is_winner)
            for c in db.query(RecruitingCharge).filter(
                RecruitingCharge.draft_id == draft_id, RecruitingCharge.team_id.in_(own))
        ]
    return StateOut(
        status=draft.status, draft_id=draft.id, current_period=engine.current_period(draft),
        total_periods=draft.total_periods, period_seconds=draft.period_seconds,
        buffer_seconds=draft.buffer_seconds, **_timing(draft),
        started_at=draft.started_at, ended_at=draft.ended_at,
        season_weeks=season.SEASON_WEEKS, playoff_weeks=season.PLAYOFF_WEEKS,
        awards=[
            AwardOut(draftable_asset_id=a.draftable_asset_id, team_id=a.team_id, period_number=a.period_number,
                     price=a.price if a.team_id in own else None, reason=a.reason,
                     was_tie_broken_randomly=a.was_tie_broken_randomly)
            for a in awards
        ],
        my_bids=my_bids,
        my_charges=my_charges,
        teams=[
            TeamFinanceOut(
                **{**f, "committed": f["committed"] if tid in own else None,
                   "roster_cost": f["roster_cost"] if tid in own else None,
                   "season_weekly_budget": f["season_weekly_budget"] if tid in own else None},
                team_id=tid, spendable=f["remaining"] - f["committed"] if tid in own else None)
            for tid, f in engine.team_finances(db, draft).items()
        ],
    )


@router.put("/{draft_id}/bids", response_model=BidResult)
def put_bid(
    draft_id: int, payload: BidRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Sets (or, with amount 0, retracts) your team's standing bid on any
    player, at any time while the draft is open. The caller must own the
    team, and the bid can't take the team past the money it has accrued (a
    400 says how much it can put on this player). The bid is processed when the current period ends: if the bids on
    the player then add up to his hidden price, the highest bidder gets him -
    and every team that bid on him is charged its own bid, win or lose;
    otherwise it carries into the next period, uncharged."""
    draft = _draft(db, draft_id)
    assert_owns_team(db, user, payload.team_id)
    try:
        engine.place_bid(db, draft_id, payload.team_id, payload.draftable_asset_id, payload.amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return BidResult(
        draftable_asset_id=payload.draftable_asset_id, amount=payload.amount,
        current_period=engine.current_period(draft),
        period_ends_at=_timing(draft)["period_ends_at"],
    )


class ResultBidOut(BaseModel):
    draftable_asset_id: int
    bid_amount: int
    outcome: str            # "won" / "lost" (another team won him - the bid is still charged) / "pending" (price not met)
    completion_pct: float   # total bids on him / his hidden price, in percent - shown ONLY on this results screen
    charged: int


class PeriodResultsOut(BaseModel):
    period_number: int
    is_final: bool
    bids: list[ResultBidOut]
    charged_total: int
    lost_total: int         # charged for bids on players another team won
    players_won: int


class AckRequest(BaseModel):
    team_id: int
    period_number: int


@router.get("/{draft_id}/results/pending", response_model=list[PeriodResultsOut])
def pending_results(
    draft_id: int, team_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """The end-of-period results this team hasn't closed yet, oldest first: how
    each of ITS bids came out, with each player's completion percentage. This is
    the only place a completion percentage is ever exposed, and only for the
    caller's own bids."""
    draft = _draft(db, draft_id)
    assert_owns_team(db, user, team_id)
    engine.sync(db, draft.league_id)
    db.commit()
    acked = {a.period_number for a in db.query(RecruitingResultAck).filter_by(draft_id=draft_id, team_id=team_id)}
    rows = db.query(RecruitingBidResult).filter_by(draft_id=draft_id, team_id=team_id).order_by(
        RecruitingBidResult.period_number, RecruitingBidResult.id).all()
    out = []
    for n in range(1, draft.periods_processed + 1):
        if n in acked:
            continue
        mine = [r for r in rows if r.period_number == n]
        out.append(PeriodResultsOut(
            period_number=n, is_final=n == draft.total_periods,
            bids=[ResultBidOut(draftable_asset_id=r.draftable_asset_id, bid_amount=r.bid_amount, outcome=r.outcome,
                               completion_pct=r.completion_pct, charged=r.charged) for r in mine],
            charged_total=sum(r.charged for r in mine),
            lost_total=sum(r.charged for r in mine if r.outcome == "lost"),
            players_won=sum(1 for r in mine if r.outcome == "won"),
        ))
    return out


@router.post("/{draft_id}/results/ack")
def ack_results(draft_id: int, payload: AckRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The team closed the results screen for a period."""
    draft = _draft(db, draft_id)
    assert_owns_team(db, user, payload.team_id)
    if not 1 <= payload.period_number <= draft.periods_processed:
        raise HTTPException(400, "That period hasn't been processed yet")
    exists = db.query(RecruitingResultAck).filter_by(
        draft_id=draft_id, team_id=payload.team_id, period_number=payload.period_number).first()
    if exists is None:
        db.add(RecruitingResultAck(draft_id=draft_id, team_id=payload.team_id, period_number=payload.period_number))
        db.commit()
    return {"acknowledged": payload.period_number}


@router.post("/{draft_id}/ai/tick")
def ai_tick(draft_id: int, intensity: float = 0.35, db: Session = Depends(get_db)):
    """Lets every AI-owned team act (raise or open bids). Takes no team
    argument and never acts for a human-owned team, so there's nothing to
    spoof - a caller can only make the AI move sooner."""
    _draft(db, draft_id)
    intensity = max(0.0, min(1.0, intensity))
    acted = recruiting_ai.tick(db, draft_id, intensity=intensity)
    db.commit()
    return {"bids_placed": acted}
