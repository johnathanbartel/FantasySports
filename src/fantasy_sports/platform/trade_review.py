"""Review window for person-to-person trades (FS-036): the defence against collusion.

When the counterparty accepts a person-to-person trade, it doesn't execute at once. If the league has a review window
(trade_review_hours > 0 and trade_review_mode != 'none') and the trade is eligible, it goes to 'in_review' until
review_ends_at. In that time:

  * the commissioner can veto it (mode 'commissioner' or 'both'), unless they manage a team in the trade;
  * the league can veto it (mode 'league' or 'both'): a majority (n // 2 + 1) of the person-managed teams that are NOT in the
    trade. AI teams don't veto. With no such team the league veto is unavailable;
  * either manager can cancel it (trades.cancel_trade).

Nobody can approve it early or shorten the window, the commissioner included: only process_review_queue(), after
review_ends_at, executes it, and trades.execute_trade refuses an in_review trade before then. Eligibility stays
authoritative: execute_trade rechecks it, so a deposit disputed or a deadline passed during the review blocks the trade.

Nothing schedules itself. process_review_queue(db, now) is a plain function with an injected clock (the banking.py pattern);
the API runs it for a league whenever its trades are read, and exposes POST /leagues/{id}/trades/process-review.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from fantasy_sports.platform import trade_eligibility, trades
from fantasy_sports.platform.db.models.core import League, Team, Trade, TradeApproval, TradeVeto

log = logging.getLogger(__name__)

COMMISSIONER_VETO = "COMMISSIONER_VETO"
MODES = ("none", "commissioner", "league", "both")


def review_applies(league: League) -> bool:
    return bool(league.trade_review_hours) and league.trade_review_mode != "none"


def start_review(db: Session, trade: Trade, league: League, now: datetime.datetime) -> None:
    """Puts an accepted, eligible trade into its review window. Flushes."""
    trade.status = "in_review"
    trade.review_ends_at = now + datetime.timedelta(hours=league.trade_review_hours)
    db.flush()
    log.info("trade %s in review until %s (mode %s)", trade.id, trade.review_ends_at, league.trade_review_mode)


def _in_review(trade: Trade, now: datetime.datetime) -> None:
    if trade.status != "in_review":
        raise ValueError(f"Trade is not in its review window (current status: {trade.status})")
    if trade.review_ends_at is not None and now >= trade.review_ends_at:
        raise ValueError("The review window has ended")


def eligible_veto_teams(db: Session, trade: Trade) -> list[Team]:
    """Person-managed teams in the league that aren't in the trade."""
    return [t for t in db.query(Team).filter_by(league_id=trade.league_id).order_by(Team.id)
            if not t.is_ai_owned and t.id not in (trade.proposer_team_id, trade.counterparty_team_id)]


def veto_majority(db: Session, trade: Trade) -> int | None:
    """Vetoes needed from the league, or None when no team may veto."""
    n = len(eligible_veto_teams(db, trade))
    return n // 2 + 1 if n else None


def league_veto_count(db: Session, trade: Trade) -> int:
    eligible = {t.id for t in eligible_veto_teams(db, trade)}
    return sum(1 for v in db.query(TradeVeto).filter_by(trade_id=trade.id) if v.team_id in eligible)


def commissioner_veto(db: Session, trade_id: int) -> TradeApproval | None:
    return db.query(TradeApproval).filter_by(trade_id=trade_id, approval_type=COMMISSIONER_VETO).one_or_none()


def _veto(db: Session, trade: Trade, now: datetime.datetime, why: str) -> None:
    trade.status = "vetoed"
    trade.failure_reason = why[:500]
    trade.resolved_at = now
    db.flush()
    log.info("trade %s vetoed: %s", trade.id, why)


def veto_by_commissioner(db: Session, trade: Trade, user_id: int, reason: str | None = None,
                         now: datetime.datetime | None = None) -> Trade:
    """The commissioner vetoes a trade in review. A commissioner who manages a team in the trade can't (the conflict rule
    of commissioner_decide)."""
    now = now or datetime.datetime.utcnow()
    league = db.get(League, trade.league_id)
    if league is None or league.commissioner_user_id != user_id:
        raise PermissionError("Only the league commissioner can do that")
    if league.trade_review_mode not in ("commissioner", "both"):
        raise ValueError("The commissioner can't veto trades in this league's review mode")
    _in_review(trade, now)
    if any(t.owner_user_id == user_id for t in (trade.proposer_team, trade.counterparty_team)):
        raise ValueError("The commissioner manages a team in this trade, so can't veto it")
    db.add(TradeApproval(trade_id=trade.id, approval_type=COMMISSIONER_VETO, user_id=user_id, decision="reject", created_at=now))
    _veto(db, trade, now, "Vetoed by the commissioner." + (f" {reason.strip()}" if reason and reason.strip() else ""))
    return trade


def veto_by_league(db: Session, trade: Trade, team: Team, user_id: int | None,
                   now: datetime.datetime | None = None) -> Trade:
    """A person-managed team that isn't in the trade casts a veto. The trade is vetoed once vetoes reach a majority of those
    teams (n // 2 + 1)."""
    now = now or datetime.datetime.utcnow()
    league = db.get(League, trade.league_id)
    if league is None or team.league_id != trade.league_id:
        raise ValueError("Team is not part of this league")
    if league.trade_review_mode not in ("league", "both"):
        raise ValueError("The league can't veto trades in this league's review mode")
    _in_review(trade, now)
    eligible = {t.id for t in eligible_veto_teams(db, trade)}
    if not eligible:
        raise ValueError("No team in this league is eligible to veto this trade")
    if team.id not in eligible:
        raise ValueError("Only person-managed teams that aren't in the trade can veto it")
    if db.query(TradeVeto).filter_by(trade_id=trade.id, team_id=team.id).first() is None:
        db.add(TradeVeto(trade_id=trade.id, team_id=team.id, user_id=user_id, created_at=now))
        db.flush()
    needed = len(eligible) // 2 + 1
    if league_veto_count(db, trade) >= needed:
        _veto(db, trade, now, f"Vetoed by the league ({needed} of {len(eligible)} eligible teams).")
    return trade


def process_review_queue(db: Session, now: datetime.datetime | None = None, league_id: int | None = None) -> list[dict]:
    """Executes every 'in_review' trade whose window has ended. execute_trade rechecks eligibility, so a deposit disputed or
    refunded during the review, or a passed deadline, blocks the trade. A player locked by a game in progress defers the
    trade to the next run. Returns one summary dict per trade handled."""
    now = now or datetime.datetime.utcnow()
    q = db.query(Trade).filter(Trade.status == "in_review", Trade.review_ends_at <= now)
    if league_id is not None:
        q = q.filter(Trade.league_id == league_id)
    out = []
    for trade in q.order_by(Trade.review_ends_at, Trade.id).all():
        try:
            trades.execute_trade(db, trade, now)
        except trades.GameLocked as e:
            log.info("trade %s deferred: %s", trade.id, e)
            out.append({"trade_id": trade.id, "result": "deferred", "detail": str(e)})
            continue
        except ValueError as e:
            trade.status = "blocked"
            trade.failure_reason = str(e)[:500]
            trade.resolved_at = now
            db.flush()
            log.warning("trade %s blocked after review: %s", trade.id, e)
            out.append({"trade_id": trade.id, "result": "blocked", "detail": str(e)})
            continue
        out.append({"trade_id": trade.id, "result": trade.status, "detail": trade.failure_reason})
    return out
