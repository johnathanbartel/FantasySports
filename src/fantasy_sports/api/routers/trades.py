"""
Trades router: endpoints to propose, vote on, respond to, and view league trades.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_can_manage_team, assert_commissioner, current_user, optional_user
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.platform import dues, manager_replacement, pick_trading, trade_eligibility, trade_review, trades
from fantasy_sports.platform import trade_hub as trade_hub_logic
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, RealTeam, RosterEntry, SeasonDues, Team, Trade, TradeBlockEntry, TradeVote, User,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.draft_stats import stats_by_asset
from fantasy_sports.platform.player_rates import rates_by_asset

router = APIRouter(tags=["trades"])


class PickRefIn(BaseModel):
    season: int                 # season number of the draft the pick is in
    round: int
    original_team_id: int       # the team the pick started with


class TradeProposeRequest(BaseModel):
    proposer_team_id: int
    counterparty_team_id: int
    send_asset_ids: list[int] = []
    receive_asset_ids: list[int] = []
    send_picks: list[PickRefIn] = []
    receive_picks: list[PickRefIn] = []


class TradePickOut(BaseModel):
    season: int
    round: int
    original_team_id: int
    original_team_name: str
    projected_overall: int | None = None     # where it would land if the season ended today (a pick's point value stays server-side)


class TradeRespondRequest(BaseModel):
    team_id: int
    accept: bool


class TradeVoteRequest(BaseModel):
    team_id: int
    vote: str  # "approve" | "reject"


class TradeAssetOut(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    points: float | None = None
    points_per_game: float | None = None


class TradeVoteOut(BaseModel):
    team_id: int
    team_name: str
    vote: str
    created_at: datetime.datetime


class EligibilityOut(BaseModel):
    status: str
    approval_requirements: list[str] = []
    payment_requirements: list[dict] = []
    blocking_codes: list[str] = []
    explanations: list[str] = []
    missing_prerequisites: list[str] = []


class CommissionerDecisionRequest(BaseModel):
    decision: str  # "approve" | "reject"


class DuesRecordRequest(BaseModel):
    season_number: int
    amount_cents: int
    status: str = "paid"
    payment_reference: str = Field(min_length=1, max_length=120)


class DuesStatusRequest(BaseModel):
    status: str


class DuesOut(BaseModel):
    id: int
    team_id: int
    season_number: int
    required_cents: int
    amount_cents: int
    status: str
    payment_reference: str


class TradeOut(BaseModel):
    id: int
    league_id: int
    proposer_team_id: int
    proposer_team_name: str
    proposer_is_ai: bool
    counterparty_team_id: int
    counterparty_team_name: str
    counterparty_is_ai: bool
    status: str
    is_ai_trade: bool
    created_at: datetime.datetime
    resolved_at: datetime.datetime | None
    send_assets: list[TradeAssetOut]
    receive_assets: list[TradeAssetOut]
    send_picks: list[TradePickOut] = []
    receive_picks: list[TradePickOut] = []
    votes: list[TradeVoteOut]
    approvals: int
    rejections: int
    majority_needed: int
    total_teams: int
    user_vote: str | None = None
    eligibility: EligibilityOut | None = None
    failure_reason: str | None = None
    commissioner_decision: str | None = None
    # FS-036 review window (platform/trade_review.py)
    review_ends_at: datetime.datetime | None = None
    league_veto_count: int = 0
    league_veto_needed: int | None = None       # None: no team may veto (or the league has no league veto)
    commissioner_vetoed: bool = False


class TradeVetoRequest(BaseModel):
    team_id: int


class CommissionerVetoRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


def _asset_outs(db: Session, ids: list[int]) -> dict[int, TradeAssetOut]:
    assets = {a.id: a for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_(ids or [0]))}
    stats = stats_by_asset(db, ids)
    rates = rates_by_asset(db, ids)
    real_teams = {t.id: t.name for t in db.query(RealTeam)}
    out: dict[int, TradeAssetOut] = {}
    for aid in ids:
        a = assets.get(aid)
        if not a:
            out[aid] = TradeAssetOut(draftable_asset_id=aid, display_name="Unknown Player", position=None, real_team=None)
            continue
        out[aid] = TradeAssetOut(
            draftable_asset_id=aid,
            display_name=draftable_asset_display_name(a),
            position=draftable_asset_position(a),
            real_team=real_teams.get(a.player.real_team_id if a.player else a.defense_group.real_team_id),
            points=stats.get(aid, {}).get("points"),
            points_per_game=rates.get(aid, {}).get("points_per_game"),
        )
    return out


def _format_trade(db: Session, trade: Trade, active_user: User | None = None) -> TradeOut:
    p_team = trade.proposer_team
    c_team = trade.counterparty_team
    p_items = [it.draftable_asset_id for it in trade.items if it.sender_team_id == p_team.id and it.draftable_asset_id is not None]
    c_items = [it.draftable_asset_id for it in trade.items if it.sender_team_id == c_team.id and it.draftable_asset_id is not None]

    asset_by_id = _asset_outs(db, p_items + c_items)
    send_assets = [asset_by_id[aid] for aid in p_items]
    receive_assets = [asset_by_id[aid] for aid in c_items]

    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=trade.league_id)}
    league = db.get(League, trade.league_id)
    pick_items = [it for it in trade.items if it.draftable_asset_id is None]
    order = pick_trading.projected_order(db, league) if pick_items else []

    def picks_out(team_id: int) -> list[TradePickOut]:
        out = []
        for it in pick_items:
            if it.sender_team_id != team_id:
                continue
            overall = pick_trading.projected_overall(league, order, it.pick_round, it.pick_original_team_id)
            out.append(TradePickOut(season=it.pick_season, round=it.pick_round, original_team_id=it.pick_original_team_id,
                                    original_team_name=names.get(it.pick_original_team_id, "?"), projected_overall=overall))
        return out

    total_teams = db.query(Team).filter_by(league_id=trade.league_id).count()
    majority_needed = trade_eligibility.votes_needed(db, league, p_team, c_team)

    team_map = {t.id: t.name for t in db.query(Team).filter_by(league_id=trade.league_id)}
    votes_out = [
        TradeVoteOut(team_id=v.team_id, team_name=team_map.get(v.team_id, f"Team {v.team_id}"), vote=v.vote, created_at=v.created_at)
        for v in trade.votes
    ]
    approvals = sum(1 for v in trade.votes if v.vote == "approve")
    rejections = sum(1 for v in trade.votes if v.vote == "reject")

    user_vote = None
    if active_user:
        user_team_ids = {t.id for t in db.query(Team).filter_by(league_id=trade.league_id, owner_user_id=active_user.id)}
        for v in trade.votes:
            if v.team_id in user_team_ids:
                user_vote = v.vote
                break

    is_ai = p_team.is_ai_owned or c_team.is_ai_owned
    approval = trade_eligibility.commissioner_approval(db, trade.id)

    return TradeOut(
        id=trade.id,
        league_id=trade.league_id,
        proposer_team_id=p_team.id,
        proposer_team_name=p_team.name,
        proposer_is_ai=p_team.is_ai_owned,
        counterparty_team_id=c_team.id,
        counterparty_team_name=c_team.name,
        counterparty_is_ai=c_team.is_ai_owned,
        status=trade.status,
        is_ai_trade=is_ai,
        created_at=trade.created_at,
        resolved_at=trade.resolved_at,
        send_assets=send_assets,
        receive_assets=receive_assets,
        send_picks=picks_out(p_team.id),
        receive_picks=picks_out(c_team.id),
        votes=votes_out,
        approvals=approvals,
        rejections=rejections,
        majority_needed=majority_needed,
        total_teams=total_teams,
        user_vote=user_vote,
        eligibility=EligibilityOut(**trade_eligibility.evaluate_trade(db, trade).as_dict()),
        failure_reason=trade.failure_reason,
        commissioner_decision=approval.decision if approval else None,
        review_ends_at=trade.review_ends_at,
        league_veto_count=trade_review.league_veto_count(db, trade),
        league_veto_needed=trade_review.veto_majority(db, trade) if league.trade_review_mode in ("league", "both") else None,
        commissioner_vetoed=trade_review.commissioner_veto(db, trade.id) is not None,
    )


@router.post("/leagues/{league_id}/trades", response_model=TradeOut)
def create_trade(
    league_id: int,
    payload: TradeProposeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Proposes a trade.

    If either team is an AI team, enters 'voting' status requiring league majority approval.
    Otherwise, enters 'proposed' status directly sent to the counterparty.
    """
    proposer_team = assert_can_manage_team(db, user, payload.proposer_team_id)
    if proposer_team.league_id != league_id:
        raise HTTPException(400, "Proposer team is not in this league")
    counterparty_team = db.get(Team, payload.counterparty_team_id)
    if not counterparty_team or counterparty_team.league_id != league_id:
        raise HTTPException(404, "Counterparty team not found in this league")

    try:
        trade = trades.propose_trade(
            db,
            proposer_team,
            counterparty_team,
            payload.send_asset_ids,
            payload.receive_asset_ids,
            user_id=user.id,
            send_picks=[(p.season, p.round, p.original_team_id) for p in payload.send_picks],
            receive_picks=[(p.season, p.round, p.original_team_id) for p in payload.receive_picks],
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))

    return _format_trade(db, trade, user)


@router.get("/leagues/{league_id}/teams/{team_id}/tradeable-picks", response_model=list[TradePickOut])
def tradeable_picks(league_id: int, team_id: int, db: Session = Depends(get_db)):
    """The draft picks a team owns that can be traded now, each with where it would land today and what that is worth."""
    league = db.get(League, league_id)
    team = db.get(Team, team_id)
    if league is None or team is None or team.league_id != league_id:
        raise HTTPException(404, "Team not found in this league")
    mine = pick_trading.owned_picks(db, league, team_id)
    if not mine:
        return []
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=league_id)}
    order = pick_trading.projected_order(db, league)
    out = []
    for p in mine:
        overall = pick_trading.projected_overall(league, order, p["round"], p["original_team_id"])
        out.append(TradePickOut(season=p["season"], round=p["round"], original_team_id=p["original_team_id"],
                                original_team_name=names.get(p["original_team_id"], "?"), projected_overall=overall))
    return out


@router.get("/leagues/{league_id}/trades", response_model=list[TradeOut])
def list_league_trades(
    league_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_user),
):
    """Lists all active and completed trades in the league. Trades whose review window has ended execute first."""
    _run_review_queue(db, league_id)
    all_trades = (
        db.query(Trade)
        .filter(Trade.league_id == league_id)
        .order_by(Trade.created_at.desc())
        .all()
    )
    return [_format_trade(db, t, user) for t in all_trades]


@router.get("/trades/{trade_id}", response_model=TradeOut)
def get_trade_detail(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_user),
):
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    if trade.status == "in_review":
        _run_review_queue(db, trade.league_id)
        db.refresh(trade)
    return _format_trade(db, trade, user)


@router.post("/trades/{trade_id}/respond", response_model=TradeOut)
def respond_to_trade(
    trade_id: int,
    payload: TradeRespondRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Counterparty team accepts or declines a direct trade offer ('proposed' status)."""
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    team = assert_can_manage_team(db, user, payload.team_id)

    try:
        trade = trades.respond_trade(db, trade, team, payload.accept)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))

    return _format_trade(db, trade, user)


@router.post("/trades/{trade_id}/vote", response_model=TradeOut)
def vote_on_trade(
    trade_id: int,
    payload: TradeVoteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Casts a league approval or rejection vote on an AI-involved trade ('voting' status)."""
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    team = assert_can_manage_team(db, user, payload.team_id)

    try:
        trade = trades.vote_trade(db, trade, team, payload.vote, user_id=user.id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))

    return _format_trade(db, trade, user)


@router.post("/trades/{trade_id}/cancel", response_model=TradeOut)
def cancel_trade_proposal(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Proposing team cancels an active trade; while it is in its review window the counterparty can too."""
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    team = assert_can_manage_team(db, user, trade.proposer_team_id) if trade.status != "in_review" else next(
        (t for t in (trade.proposer_team, trade.counterparty_team) if t.owner_user_id == user.id and not t.is_ai_owned), None)
    if team is None:
        raise HTTPException(403, "You don't manage a team in this trade")

    try:
        trade = trades.cancel_trade(db, trade, team)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))

    return _format_trade(db, trade, user)


def _run_review_queue(db: Session, league_id: int) -> list[dict]:
    """Nothing schedules the review queue, so it runs whenever a league's trades are read (and on the route below)."""
    done = trade_review.process_review_queue(db, league_id=league_id)
    if done:
        db.commit()
    return done


@router.post("/leagues/{league_id}/trades/process-review")
def process_trade_review(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Executes every trade in the league whose review window has ended. Safe to call any time and from any member: it only
    does what the clock and the eligibility recheck already allow."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if league.commissioner_user_id != user.id and not db.query(Team).filter_by(league_id=league_id, owner_user_id=user.id).first():
        raise HTTPException(403, "You are not in this league")
    return {"processed": _run_review_queue(db, league_id)}


@router.post("/trades/{trade_id}/veto", response_model=TradeOut)
def veto_trade(trade_id: int, payload: TradeVetoRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """A person-managed team that isn't in the trade vetoes it during its review window; a majority of them kills it."""
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    team = assert_can_manage_team(db, user, payload.team_id)
    try:
        trade = trade_review.veto_by_league(db, trade, team, user.id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return _format_trade(db, trade, user)


@router.post("/trades/{trade_id}/commissioner-veto", response_model=TradeOut)
def commissioner_veto_trade(
    trade_id: int, payload: CommissionerVetoRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """The commissioner vetoes a trade in its review window (not if they manage a team in it)."""
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    try:
        trade = trade_review.veto_by_commissioner(db, trade, user.id, payload.reason)
        db.commit()
    except PermissionError as e:
        db.rollback()
        raise HTTPException(403, str(e))
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return _format_trade(db, trade, user)


def _picks_in(items: list[PickRefIn]) -> list[tuple[int, int, int]]:
    return [(p.season, p.round, p.original_team_id) for p in items]


@router.post("/leagues/{league_id}/trades/preview", response_model=EligibilityOut)
def preview_trade_eligibility(
    league_id: int,
    payload: TradeProposeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """What would happen if this trade were submitted now: shown to the manager before they submit it."""
    proposer_team = assert_can_manage_team(db, user, payload.proposer_team_id)
    counterparty_team = db.get(Team, payload.counterparty_team_id)
    league = db.get(League, league_id)
    if league is None or proposer_team.league_id != league_id or not counterparty_team or counterparty_team.league_id != league_id:
        raise HTTPException(404, "Teams not found in this league")
    res = trade_eligibility.evaluate_proposal(
        db, league, proposer_team, counterparty_team, payload.send_asset_ids, payload.receive_asset_ids,
        _picks_in(payload.send_picks), _picks_in(payload.receive_picks))
    return EligibilityOut(**res.as_dict())


@router.get("/leagues/{league_id}/trades/{trade_id}/eligibility", response_model=EligibilityOut)
def trade_eligibility_status(
    league_id: int, trade_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    res = trade_eligibility.validate_trade_eligibility(db, trade_id, league_id, user.id)
    if "NOT_AUTHORIZED" in res.blocking_codes:
        raise HTTPException(403, res.message())
    if "TRADE_NOT_FOUND" in res.blocking_codes:
        raise HTTPException(404, res.message())
    return EligibilityOut(**res.as_dict())


@router.post("/trades/{trade_id}/commissioner-decision", response_model=TradeOut)
def commissioner_decision(
    trade_id: int,
    payload: CommissionerDecisionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """The commissioner signs off on (or rejects) a free-league trade involving an eliminated team."""
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    try:
        trade = trades.commissioner_decide(db, trade, user.id, payload.decision)
        db.commit()
    except PermissionError as e:
        db.rollback()
        raise HTTPException(403, str(e))
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return _format_trade(db, trade, user)


class TradeHubTeamOut(BaseModel):
    team_id: int
    team_name: str
    is_ai_owned: bool
    plan: str                   # "win_now" | "rebuilding"
    eliminated: bool            # mathematically out of the playoffs (what turns an AI team to Rebuilding)
    on_block: int               # players on its trade block


class TradeHubOut(BaseModel):
    league_id: int
    trade_deadline: datetime.datetime | None
    deadline_passed: bool
    teams: list[TradeHubTeamOut]


class TradePlanRequest(BaseModel):
    plan: str


class TradeBlockAddRequest(BaseModel):
    draftable_asset_id: int


class TradeBlockTeamOut(BaseModel):
    team_id: int
    team_name: str
    plan: str
    players: list[TradeAssetOut]


def _block_entries(db: Session, league_id: int) -> dict[int, list[TradeBlockEntry]]:
    """Block entries per team, leaving out a player who is no longer on the team's roster."""
    on_roster = {(r.team_id, r.draftable_asset_id) for r in
                 db.query(RosterEntry).join(Team, Team.id == RosterEntry.team_id).filter(Team.league_id == league_id)}
    out: dict[int, list[TradeBlockEntry]] = {}
    for e in db.query(TradeBlockEntry).filter_by(league_id=league_id).order_by(TradeBlockEntry.id):
        if (e.team_id, e.draftable_asset_id) in on_roster:
            out.setdefault(e.team_id, []).append(e)
    return out


@router.get("/leagues/{league_id}/trade-hub", response_model=TradeHubOut)
def trade_hub(league_id: int, db: Session = Depends(get_db)):
    """The trade deadline and every team's trading plan. A manager's plan is their own pick; an AI team is Win Now until it is
    mathematically eliminated, then Rebuilding."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    eliminated = trade_hub_logic.eliminated_team_ids(db, league)
    block = _block_entries(db, league_id)
    teams = db.query(Team).filter_by(league_id=league_id).order_by(Team.id).all()
    deadline = league.trade_deadline
    return TradeHubOut(
        league_id=league_id, trade_deadline=deadline,
        deadline_passed=deadline is not None and datetime.datetime.utcnow() > deadline,
        teams=[TradeHubTeamOut(team_id=t.id, team_name=t.name, is_ai_owned=t.is_ai_owned,
                               plan=trade_hub_logic.plan_of(t, eliminated), eliminated=t.id in eliminated,
                               on_block=len(block.get(t.id, []))) for t in teams])


@router.put("/teams/{team_id}/trade-plan", response_model=TradeHubTeamOut)
def set_trade_plan(team_id: int, payload: TradePlanRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """A manager chooses Win Now or Rebuilding for their team. An AI team's plan cannot be set: it follows elimination."""
    team = assert_can_manage_team(db, user, team_id)
    if team.is_ai_owned:
        raise HTTPException(400, "An AI team's plan follows its standing and can't be set")
    if payload.plan not in trade_hub_logic.PLANS:
        raise HTTPException(400, "Plan must be 'win_now' or 'rebuilding'")
    team.trade_plan = payload.plan
    db.commit()
    league = db.get(League, team.league_id)
    eliminated = trade_hub_logic.eliminated_team_ids(db, league)
    return TradeHubTeamOut(team_id=team.id, team_name=team.name, is_ai_owned=False, plan=team.trade_plan,
                           eliminated=team.id in eliminated, on_block=len(_block_entries(db, team.league_id).get(team.id, [])))


@router.get("/leagues/{league_id}/trade-block", response_model=list[TradeBlockTeamOut])
def trade_block(league_id: int, db: Session = Depends(get_db)):
    """Every team that has players on its trade block, with those players."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    block = _block_entries(db, league_id)
    if not block:
        return []
    eliminated = trade_hub_logic.eliminated_team_ids(db, league)
    assets = _asset_outs(db, [e.draftable_asset_id for es in block.values() for e in es])
    teams = {t.id: t for t in db.query(Team).filter(Team.id.in_(list(block)))}
    return [TradeBlockTeamOut(team_id=tid, team_name=teams[tid].name, plan=trade_hub_logic.plan_of(teams[tid], eliminated),
                              players=[assets[e.draftable_asset_id] for e in es])
            for tid, es in sorted(block.items(), key=lambda kv: teams[kv[0]].name)]


@router.post("/teams/{team_id}/trade-block", response_model=TradeAssetOut)
def add_to_trade_block(team_id: int, payload: TradeBlockAddRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_can_manage_team(db, user, team_id)
    if team.is_ai_owned:
        raise HTTPException(400, "AI teams don't use the trade block")
    if not db.query(RosterEntry).filter_by(team_id=team.id, draftable_asset_id=payload.draftable_asset_id).first():
        raise HTTPException(400, "That player is not on your roster")
    if not db.query(TradeBlockEntry).filter_by(team_id=team.id, draftable_asset_id=payload.draftable_asset_id).first():
        db.add(TradeBlockEntry(league_id=team.league_id, team_id=team.id, draftable_asset_id=payload.draftable_asset_id,
                               created_at=datetime.datetime.utcnow()))
        db.commit()
    return _asset_outs(db, [payload.draftable_asset_id])[payload.draftable_asset_id]


@router.delete("/teams/{team_id}/trade-block/{draftable_asset_id}")
def remove_from_trade_block(team_id: int, draftable_asset_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    team = assert_can_manage_team(db, user, team_id)
    db.query(TradeBlockEntry).filter_by(team_id=team.id, draftable_asset_id=draftable_asset_id).delete()
    db.commit()
    return {"ok": True}


def _dues_out(r: SeasonDues) -> DuesOut:
    return DuesOut(id=r.id, team_id=r.team_id, season_number=r.season_number, required_cents=r.required_cents,
                   amount_cents=r.amount_cents, status=r.status, payment_reference=r.payment_reference)


@router.get("/leagues/{league_id}/dues", response_model=list[DuesOut])
def list_dues(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The commissioner sees every payment record; a manager sees their own franchises'."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    q = db.query(SeasonDues).filter_by(league_id=league_id)
    if league.commissioner_user_id != user.id:
        mine = [t.id for t in db.query(Team).filter_by(league_id=league_id, owner_user_id=user.id)]
        if not mine:
            raise HTTPException(403, "You are not in this league")
        q = q.filter(SeasonDues.team_id.in_(mine))
    return [_dues_out(r) for r in q.order_by(SeasonDues.id)]


@router.post("/leagues/{league_id}/teams/{team_id}/dues", response_model=DuesOut)
def record_dues(
    league_id: int, team_id: int, payload: DuesRecordRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: records a franchise's dues payment for a season (idempotent on the payment reference)."""
    league = assert_commissioner(db, user, league_id)
    team = db.get(Team, team_id)
    if team is None or team.league_id != league_id:
        raise HTTPException(404, "Team not found in this league")
    try:
        row = dues.record_payment(db, league, team, payload.season_number, payload.amount_cents, payload.status,
                                  payload.payment_reference, user.id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return _dues_out(row)


@router.post("/leagues/{league_id}/dues/{dues_id}/status", response_model=DuesOut)
def set_dues_status(
    league_id: int, dues_id: int, payload: DuesStatusRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: moves a payment record to another status (e.g. paid -> refunded)."""
    league = assert_commissioner(db, user, league_id)
    row = db.get(SeasonDues, dues_id)
    if row is None or row.league_id != league_id:
        raise HTTPException(404, "Payment record not found")
    try:
        dues.set_status(db, league, row, payload.status, user.id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return _dues_out(row)


class ReplaceManagerRequest(BaseModel):
    user_id: int


@router.post("/leagues/{league_id}/teams/{team_id}/replace-manager")
def replace_team_manager(
    league_id: int, team_id: int, payload: ReplaceManagerRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: hands a franchise to a replacement manager. Prepaid dues and platform fees stay on the franchise, so
    the replacement inherits them (the response lists that credit). Pending offers the old manager made or was sent are cancelled."""
    assert_commissioner(db, user, league_id)
    team = db.get(Team, team_id)
    if team is None or team.league_id != league_id:
        raise HTTPException(404, "Team not found in this league")
    try:
        result = manager_replacement.replace_manager(db, team, payload.user_id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return result
