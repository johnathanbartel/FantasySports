"""
Trades router: endpoints to propose, vote on, respond to, and view league trades.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_can_manage_team, current_user, optional_user
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.platform import pick_trading, trades
from fantasy_sports.platform.db.models.core import DraftableAsset, League, RealTeam, Team, Trade, TradeVote, User
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
    projected_overall: int | None = None     # where it would land if the season ended today
    value: float | None = None               # points of the player at that spot (what AI teams count it as)


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


def _format_trade(db: Session, trade: Trade, active_user: User | None = None) -> TradeOut:
    p_team = trade.proposer_team
    c_team = trade.counterparty_team
    p_items = [it.draftable_asset_id for it in trade.items if it.sender_team_id == p_team.id and it.draftable_asset_id is not None]
    c_items = [it.draftable_asset_id for it in trade.items if it.sender_team_id == c_team.id and it.draftable_asset_id is not None]

    all_ids = p_items + c_items
    assets = {a.id: a for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_(all_ids or [0]))}
    stats = stats_by_asset(db, all_ids)
    rates = rates_by_asset(db, all_ids)
    real_teams = {t.id: t.name for t in db.query(RealTeam)}

    def asset_out(aid: int) -> TradeAssetOut:
        a = assets.get(aid)
        if not a:
            return TradeAssetOut(draftable_asset_id=aid, display_name="Unknown Player", position=None, real_team=None)
        r_team = real_teams.get(a.player.real_team_id if a.player else a.defense_group.real_team_id)
        st = stats.get(aid, {})
        rt = rates.get(aid, {})
        return TradeAssetOut(
            draftable_asset_id=aid,
            display_name=draftable_asset_display_name(a),
            position=draftable_asset_position(a),
            real_team=r_team,
            points=st.get("points"),
            points_per_game=rt.get("points_per_game"),
        )

    send_assets = [asset_out(aid) for aid in p_items]
    receive_assets = [asset_out(aid) for aid in c_items]

    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=trade.league_id)}
    league = db.get(League, trade.league_id)
    pick_items = [it for it in trade.items if it.draftable_asset_id is None]
    order, points = (pick_trading.projected_order(db, league), pick_trading.ranked_points(db, league)) if pick_items else ([], [])

    def picks_out(team_id: int) -> list[TradePickOut]:
        out = []
        for it in pick_items:
            if it.sender_team_id != team_id:
                continue
            overall = pick_trading.projected_overall(league, order, it.pick_round, it.pick_original_team_id)
            out.append(TradePickOut(season=it.pick_season, round=it.pick_round, original_team_id=it.pick_original_team_id,
                                    original_team_name=names.get(it.pick_original_team_id, "?"), projected_overall=overall,
                                    value=round(pick_trading.pick_value(points, overall), 2)))
        return out

    total_teams = db.query(Team).filter_by(league_id=trade.league_id).count()
    majority_needed = (total_teams // 2) + 1

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
    order, points = pick_trading.projected_order(db, league), pick_trading.ranked_points(db, league)
    out = []
    for p in mine:
        overall = pick_trading.projected_overall(league, order, p["round"], p["original_team_id"])
        out.append(TradePickOut(season=p["season"], round=p["round"], original_team_id=p["original_team_id"],
                                original_team_name=names.get(p["original_team_id"], "?"), projected_overall=overall,
                                value=round(pick_trading.pick_value(points, overall), 2)))
    return out


@router.get("/leagues/{league_id}/trades", response_model=list[TradeOut])
def list_league_trades(
    league_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_user),
):
    """Lists all active and completed trades in the league."""
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
    """Proposing team cancels an active trade."""
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    team = assert_can_manage_team(db, user, trade.proposer_team_id)

    try:
        trade = trades.cancel_trade(db, trade, team)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))

    return _format_trade(db, trade, user)
