"""
Trades between teams in a league.

Enables trades between all teams. Trades with AI teams require league majority approval:
when an AI team is involved (as proposer or counterparty), the trade enters 'voting' status
and requires more than 50% of the teams in the league to approve before executing.
Human-to-human trades are proposed directly to the counterparty, who can accept or decline.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

import logging

from fantasy_sports.platform import game_lock, pecking_order, pick_trading, trade_eligibility
from fantasy_sports.platform.ai import ai_roster
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    FreeAgencyTransaction,
    League,
    LineupEntry,
    RosterEntry,
    Team,
    Trade,
    TradeItem,
    TradeApproval,
    TradeVote,
)
from fantasy_sports.platform.draft_stats import stats_by_asset
from fantasy_sports.platform.league_features import roster_limit

log = logging.getLogger(__name__)


PickRef = tuple[int, int, int]      # (season_number of the draft, round, original team id)


def _roster_count(db: Session, team_id: int) -> int:
    return db.query(RosterEntry).filter_by(team_id=team_id).count()


def _players(trade: Trade, team_id: int) -> list[int]:
    return [it.draftable_asset_id for it in trade.items if it.sender_team_id == team_id and it.draftable_asset_id is not None]


def _picks(trade: Trade, team_id: int) -> list[PickRef]:
    return [(it.pick_season, it.pick_round, it.pick_original_team_id) for it in trade.items
            if it.sender_team_id == team_id and it.draftable_asset_id is None]


def evaluate_ai_votes(db: Session, trade: Trade, now: datetime.datetime) -> None:
    """AI teams in the league evaluate trades involving AI teams and cast approval or rejection votes.

    Fairness heuristic:
    - Counterparty AI compares what it receives from proposer vs what it gives up.
    - Third-party AI teams in the league evaluate fairness to protect competitive integrity.
    """
    p_team = trade.proposer_team
    c_team = trade.counterparty_team
    p_asset_ids = _players(trade, p_team.id)
    c_asset_ids = _players(trade, c_team.id)
    p_picks, c_picks = _picks(trade, p_team.id), _picks(trade, c_team.id)

    stats = stats_by_asset(db, p_asset_ids + c_asset_ids)
    p_val = sum(stats.get(aid, {}).get("points") or 0.0 for aid in p_asset_ids)
    c_val = sum(stats.get(aid, {}).get("points") or 0.0 for aid in c_asset_ids)
    has_picks = bool(p_picks or c_picks)
    if has_picks:
        # A pick is worth the points of the player who would go at its projected spot if the season ended today.
        league = db.get(League, trade.league_id)
        order = pick_trading.projected_order(db, league)
        points = pick_trading.ranked_points(db, league)
        value = lambda picks: sum(pick_trading.pick_value(points, pick_trading.projected_overall(league, order, r, o))
                                  for _, r, o in picks)
        p_val += value(p_picks)
        c_val += value(c_picks)

    ai_teams = db.query(Team).filter_by(league_id=trade.league_id, is_ai_owned=True).all()
    for team in ai_teams:
        if team.id == p_team.id:
            continue  # proposer already votes approve

        if team.id == c_team.id:
            # The counterparty AI receives p_val and gives up c_val
            # Approves if offer is fair (receives >= 85% of given value, or difference is small)
            if has_picks:
                is_fair = p_val >= c_val        # picks are valued exactly, so the AI has to come out ahead
            else:
                is_fair = (p_val >= c_val * 0.85) or (c_val - p_val <= 1.5) or (c_val == 0.0)
            vote = "approve" if is_fair else "reject"
        else:
            # Third-party AI team checks that the human didn't severely fleece the AI
            if c_team.is_ai_owned:
                is_fair = (p_val >= c_val * 0.80) or (c_val - p_val <= 2.0) or (c_val == 0.0)
            elif p_team.is_ai_owned:
                is_fair = (c_val >= p_val * 0.80) or (p_val - c_val <= 2.0) or (p_val == 0.0)
            else:
                is_fair = True
            vote = "approve" if is_fair else "reject"

        existing = db.query(TradeVote).filter_by(trade_id=trade.id, team_id=team.id).first()
        if existing:
            existing.vote = vote
        else:
            db.add(TradeVote(trade_id=trade.id, team_id=team.id, user_id=None, vote=vote, created_at=now))
    db.flush()


def check_majority_vote(db: Session, trade: Trade, now: datetime.datetime) -> bool:
    """Checks whether the voting trade has reached league majority approval or rejection.

    Returns True if trade was executed, False otherwise.
    """
    total_teams = db.query(Team).filter_by(league_id=trade.league_id).count()
    majority_needed = (total_teams // 2) + 1
    approvals = db.query(TradeVote).filter_by(trade_id=trade.id, vote="approve").count()
    rejections = db.query(TradeVote).filter_by(trade_id=trade.id, vote="reject").count()

    if approvals >= majority_needed:
        # A trade that also needs a sign-off (or dues) waits; execute_trade rechecks everything and records a refusal.
        if trade_eligibility.evaluate_trade(db, trade, now).status in (
                trade_eligibility.AWAITING_PAYMENT, trade_eligibility.AWAITING_COMMISSIONER_APPROVAL):
            return False
        execute_trade(db, trade, now)
        return trade.status == "accepted"
    elif rejections > (total_teams - majority_needed):
        trade.status = "rejected"
        trade.resolved_at = now
        db.flush()
    return False


def propose_trade(
    db: Session,
    proposer_team: Team,
    counterparty_team: Team,
    send_asset_ids: list[int],
    receive_asset_ids: list[int],
    user_id: int | None = None,
    now: datetime.datetime | None = None,
    send_picks: list[PickRef] | None = None,
    receive_picks: list[PickRef] | None = None,
) -> Trade:
    """Proposes a trade between proposer_team and counterparty_team.

    If either team is AI-owned, the trade requires league majority approval and enters 'voting' status.
    Otherwise, it enters 'proposed' status waiting for the counterparty to accept or decline.
    """
    now = now or datetime.datetime.utcnow()
    league = db.get(League, proposer_team.league_id)
    if league is None:
        raise ValueError("League not found")
    game_lock.assert_unlocked(db, league.id, [*send_asset_ids, *receive_asset_ids], now)
    if proposer_team.league_id != counterparty_team.league_id:
        raise ValueError("Teams must belong to the same league")
    if proposer_team.id == counterparty_team.id:
        raise ValueError("Cannot trade with your own team")
    send_picks, receive_picks = list(send_picks or []), list(receive_picks or [])
    if not (send_asset_ids or send_picks) or not (receive_asset_ids or receive_picks):
        raise ValueError("Each side of a trade has to give at least one player or draft pick")
    for picks, owner, label in ((send_picks, proposer_team, "send"), (receive_picks, counterparty_team, "receive")):
        for season, rnd, original in picks:
            why = pick_trading.pick_available(db, league, season, rnd, original)
            if why:
                raise ValueError(why)
            if pick_trading.current_owner(db, league.id, season, rnd, original) != owner.id:
                raise ValueError(f"{owner.name} doesn't own the round {rnd} pick (season {season}) you listed to {label}")
        if len(set(picks)) != len(picks):
            raise ValueError("A draft pick is listed twice")

    # Validate roster ownership
    p_roster = {r.draftable_asset_id for r in db.query(RosterEntry).filter_by(team_id=proposer_team.id)}
    c_roster = {r.draftable_asset_id for r in db.query(RosterEntry).filter_by(team_id=counterparty_team.id)}

    for aid in send_asset_ids:
        if aid not in p_roster:
            raise ValueError(f"Player {aid} is not on {proposer_team.name}'s roster")
    for aid in receive_asset_ids:
        if aid not in c_roster:
            raise ValueError(f"Player {aid} is not on {counterparty_team.name}'s roster")

    # Validate roster limits after trade
    limit = roster_limit(db, league)
    p_post_count = len(p_roster) - len(send_asset_ids) + len(receive_asset_ids)
    c_post_count = len(c_roster) - len(receive_asset_ids) + len(send_asset_ids)
    if p_post_count > limit:
        raise ValueError(f"{proposer_team.name} would exceed roster limit ({limit})")
    if c_post_count > limit:
        raise ValueError(f"{counterparty_team.name} would exceed roster limit ({limit})")

    # CFG-06: deadline, eliminated teams, dues and special approvals (platform/trade_eligibility.py).
    eligibility = trade_eligibility.evaluate_proposal(db, league, proposer_team, counterparty_team, send_asset_ids,
                                                      receive_asset_ids, send_picks, receive_picks, now)
    if not eligibility.executable:
        raise ValueError(eligibility.message())
    special = bool({trade_eligibility.ELIMINATED_FREE_LEAGUE_TRADE, trade_eligibility.ELIMINATED_PICK_TRADE_VOTE}
                   & set(eligibility.approval_requirements))

    has_ai = proposer_team.is_ai_owned or counterparty_team.is_ai_owned
    voting = has_ai or special       # a special trade is put to the league whoever owns the teams
    status = "voting" if voting else "proposed"

    trade = Trade(
        league_id=league.id,
        proposer_team_id=proposer_team.id,
        counterparty_team_id=counterparty_team.id,
        status=status,
        created_at=now,
    )
    db.add(trade)
    db.flush()

    for aid in send_asset_ids:
        db.add(TradeItem(trade_id=trade.id, sender_team_id=proposer_team.id, draftable_asset_id=aid))
    for aid in receive_asset_ids:
        db.add(TradeItem(trade_id=trade.id, sender_team_id=counterparty_team.id, draftable_asset_id=aid))
    for season, rnd, original in send_picks:
        db.add(TradeItem(trade_id=trade.id, sender_team_id=proposer_team.id, pick_season=season, pick_round=rnd,
                         pick_original_team_id=original))
    for season, rnd, original in receive_picks:
        db.add(TradeItem(trade_id=trade.id, sender_team_id=counterparty_team.id, pick_season=season, pick_round=rnd,
                         pick_original_team_id=original))
    db.flush()

    log.info("trade %s proposed league=%s status=%s requirements=%s", trade.id, league.id, status, eligibility.approval_requirements)
    if voting:
        # Proposer automatically votes approve
        db.add(TradeVote(trade_id=trade.id, team_id=proposer_team.id, user_id=user_id, vote="approve", created_at=now))
        db.flush()
        # Evaluate AI teams' votes across the league
        evaluate_ai_votes(db, trade, now)
        check_majority_vote(db, trade, now)

    return trade


def respond_trade(
    db: Session, trade: Trade, team: Team, accept: bool, now: datetime.datetime | None = None
) -> Trade:
    """Counterparty team responds to a direct trade proposal ('proposed' status)."""
    now = now or datetime.datetime.utcnow()
    if trade.status != "proposed":
        raise ValueError(f"Trade is not pending response (current status: {trade.status})")
    if team.id != trade.counterparty_team_id:
        raise ValueError("Only the counterparty team can respond to this trade")

    if not accept:
        trade.status = "declined"
        trade.resolved_at = now
        db.flush()
        return trade

    execute_trade(db, trade, now)
    return trade


def vote_trade(
    db: Session, trade: Trade, team: Team, vote: str, user_id: int | None = None, now: datetime.datetime | None = None
) -> Trade:
    """Casts a league vote on a trade requiring league majority approval ('voting' status)."""
    now = now or datetime.datetime.utcnow()
    if trade.status != "voting":
        raise ValueError(f"Trade is not currently in voting (current status: {trade.status})")
    if team.league_id != trade.league_id:
        raise ValueError("Team is not part of this league")
    if vote not in ("approve", "reject"):
        raise ValueError("Vote must be 'approve' or 'reject'")

    existing = db.query(TradeVote).filter_by(trade_id=trade.id, team_id=team.id).first()
    if existing:
        existing.vote = vote
    else:
        db.add(TradeVote(trade_id=trade.id, team_id=team.id, user_id=user_id, vote=vote, created_at=now))
    db.flush()

    check_majority_vote(db, trade, now)
    return trade


def commissioner_decide(
    db: Session, trade: Trade, user_id: int, decision: str, now: datetime.datetime | None = None
) -> Trade:
    """The commissioner's sign-off (or rejection) on a free-league trade involving an eliminated team.

    Only the league's commissioner may; it cannot stand in for the league vote (execution still needs the majority), and a
    commissioner who manages a team in the trade cannot decide it (the conflict-of-interest policy is still open)."""
    now = now or datetime.datetime.utcnow()
    league = db.get(League, trade.league_id)
    if league is None or league.commissioner_user_id != user_id:
        raise PermissionError("Only the league commissioner can do that")
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be 'approve' or 'reject'")
    if trade.status != "voting":
        raise ValueError(f"Trade is not awaiting approval (current status: {trade.status})")
    res = trade_eligibility.evaluate_trade(db, trade, now)
    if trade_eligibility.ELIMINATED_FREE_LEAGUE_TRADE not in res.approval_requirements:
        raise ValueError("This trade doesn't need commissioner sign-off")
    if any(t.owner_user_id == user_id for t in (trade.proposer_team, trade.counterparty_team)):
        raise ValueError("The commissioner manages a team in this trade, so can't sign it off - the league hasn't set a "
                         "conflict-of-interest policy yet")
    if trade_eligibility.commissioner_approval(db, trade.id) is not None:
        raise ValueError("The commissioner has already decided this trade")
    db.add(TradeApproval(trade_id=trade.id, approval_type=trade_eligibility.ELIMINATED_FREE_LEAGUE_TRADE,
                         user_id=user_id, decision=decision, created_at=now))
    db.flush()
    log.info("trade %s commissioner %s by user %s", trade.id, decision, user_id)
    if decision == "reject":
        trade.status = "rejected"
        trade.resolved_at = now
        db.flush()
    else:
        check_majority_vote(db, trade, now)
    return trade


def cancel_trade(
    db: Session, trade: Trade, team: Team, now: datetime.datetime | None = None
) -> Trade:
    """Proposer cancels an active trade proposal."""
    now = now or datetime.datetime.utcnow()
    if trade.status not in ("proposed", "voting"):
        raise ValueError(f"Cannot cancel trade in status '{trade.status}'")
    if team.id != trade.proposer_team_id:
        raise ValueError("Only the proposing team can cancel the trade")

    trade.status = "cancelled"
    trade.resolved_at = now
    db.flush()
    return trade


def execute_trade(db: Session, trade: Trade, now: datetime.datetime | None = None) -> None:
    """Executes an approved trade: transfers roster entries, updates pecking order/lineups, logs transactions."""
    now = now or datetime.datetime.utcnow()
    db.refresh(trade, with_for_update=True)       # serialises concurrent executions of the same trade
    if trade.status not in ("proposed", "voting"):
        raise ValueError(f"Trade is already {trade.status}")
    # CFG-06: recheck the deadline, ownership, elimination, dues, votes and sign-off right before anything moves.
    eligibility = trade_eligibility.evaluate_trade(db, trade, now)
    if eligibility.status == trade_eligibility.BLOCKED:
        trade.status = "blocked"
        trade.failure_reason = eligibility.message()[:500]
        trade.resolved_at = now
        db.flush()
        log.warning("trade %s blocked at execution: %s", trade.id, eligibility.blocking_codes)
        return
    if not eligibility.executable:
        raise ValueError(eligibility.message() or "This trade still needs approval")
    p_team = trade.proposer_team
    c_team = trade.counterparty_team
    p_items = _players(trade, p_team.id)
    c_items = _players(trade, c_team.id)
    p_picks, c_picks = _picks(trade, p_team.id), _picks(trade, c_team.id)
    game_lock.assert_unlocked(db, trade.league_id, [*p_items, *c_items], now)   # proposed before his game, accepted after

    p_roster ={r.draftable_asset_id for r in db.query(RosterEntry).filter_by(team_id=p_team.id)}
    c_roster = {r.draftable_asset_id for r in db.query(RosterEntry).filter_by(team_id=c_team.id)}

    for aid in p_items:
        if aid not in p_roster:
            raise ValueError(f"Asset {aid} is no longer on {p_team.name}'s roster")
    for aid in c_items:
        if aid not in c_roster:
            raise ValueError(f"Asset {aid} is no longer on {c_team.name}'s roster")

    league = db.get(League, trade.league_id)
    for picks, owner in ((p_picks, p_team), (c_picks, c_team)):
        for season, rnd, original in picks:
            why = pick_trading.pick_available(db, league, season, rnd, original)
            if why or pick_trading.current_owner(db, league.id, season, rnd, original) != owner.id:
                raise ValueError(why or f"{owner.name} no longer owns the round {rnd} pick (season {season})")
    limit = roster_limit(db, league)
    if len(p_roster) - len(p_items) + len(c_items) > limit:
        raise ValueError(f"{p_team.name} would exceed roster limit ({limit})")
    if len(c_roster) - len(c_items) + len(p_items) > limit:
        raise ValueError(f"{c_team.name} would exceed roster limit ({limit})")

    # Move p_items from p_team to c_team
    for aid in p_items:
        db.query(RosterEntry).filter_by(team_id=p_team.id, draftable_asset_id=aid).delete()
        db.query(LineupEntry).filter_by(team_id=p_team.id, draftable_asset_id=aid).delete()
        pecking_order.unrank_player(db, p_team, aid)

        db.add(RosterEntry(team_id=c_team.id, draftable_asset_id=aid, acquired_via="trade"))
        db.flush()
        pecking_order.rank_new_player(db, c_team, aid)

        db.add(FreeAgencyTransaction(
            league_id=trade.league_id, team_id=p_team.id, draftable_asset_id=aid,
            kind="drop", price=0, reason=f"Traded to {c_team.name}", created_at=now,
        ))
        db.add(FreeAgencyTransaction(
            league_id=trade.league_id, team_id=c_team.id, draftable_asset_id=aid,
            kind="add", price=0, reason=f"Traded from {p_team.name}", created_at=now,
        ))

    # Move c_items from c_team to p_team
    for aid in c_items:
        db.query(RosterEntry).filter_by(team_id=c_team.id, draftable_asset_id=aid).delete()
        db.query(LineupEntry).filter_by(team_id=c_team.id, draftable_asset_id=aid).delete()
        pecking_order.unrank_player(db, c_team, aid)

        db.add(RosterEntry(team_id=p_team.id, draftable_asset_id=aid, acquired_via="trade"))
        db.flush()
        pecking_order.rank_new_player(db, p_team, aid)

        db.add(FreeAgencyTransaction(
            league_id=trade.league_id, team_id=c_team.id, draftable_asset_id=aid,
            kind="drop", price=0, reason=f"Traded to {p_team.name}", created_at=now,
        ))
        db.add(FreeAgencyTransaction(
            league_id=trade.league_id, team_id=p_team.id, draftable_asset_id=aid,
            kind="add", price=0, reason=f"Traded from {c_team.name}", created_at=now,
        ))

    pick_trading.transfer(db, league, p_picks, c_team.id)
    pick_trading.transfer(db, league, c_picks, p_team.id)

    trade.status = "accepted"
    trade.resolved_at = now
    db.flush()
    log.info("trade %s executed league=%s", trade.id, trade.league_id)

    if p_team.is_ai_owned:
        ai_roster.set_lines(db, p_team, now)
    if c_team.is_ai_owned:
        ai_roster.set_lines(db, c_team, now)
