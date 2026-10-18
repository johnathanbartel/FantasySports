"""Replacing the manager of a franchise (DUE-06).

When a manager quits, the commissioner hands the franchise to a replacement. Everything prepaid - league dues and platform
fees - is recorded against the franchise (the team row), never the user, so the credit transfers by itself: the replacement
trades on those seasons without paying again, and a season can't be collected twice. This module changes the owner and
settles what the old manager left half-done; it never touches a payment record.

  * Only a person-managed team can be handed over (an AI team has no manager to replace).
  * The replacement must be a different, existing user who doesn't already manage a team in this league.
  * Trade offers the old manager made or was sent that still wait for an answer ('proposed') are cancelled: they were that
    person's decision. Trades already accepted ('in_review') or put to the league ('voting') stay: they run on, and the
    eligibility recheck at execution still applies (the credit is still on the franchise).
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import PlatformSeasonFee, SeasonDues, Team, Trade, User

log = logging.getLogger(__name__)


def credits_of(db: Session, team: Team) -> list[dict]:
    """The confirmed ('paid') credit on a franchise, newest season last: what the replacement inherits."""
    out = []
    for kind, model in (("league_dues", SeasonDues), ("platform_fee", PlatformSeasonFee)):
        for r in db.query(model).filter_by(team_id=team.id, status="paid").order_by(model.season_number, model.id):
            out.append({"kind": kind, "season_number": r.season_number, "amount_cents": r.amount_cents,
                        "committed_by_trade_id": r.committed_by_trade_id})
    return sorted(out, key=lambda c: (c["season_number"], c["kind"]))


def replace_manager(db: Session, team: Team, new_user_id: int, now: datetime.datetime | None = None) -> dict:
    """Hands a person-managed franchise to a new manager. Flushes; the caller commits. Raises ValueError."""
    now = now or datetime.datetime.utcnow()
    if team.is_ai_owned or team.owner_user_id is None:
        raise ValueError("Only a team with a human manager can be handed to a replacement")
    if new_user_id == team.owner_user_id:
        raise ValueError("That user already manages this team")
    if db.get(User, new_user_id) is None:
        raise ValueError("Replacement user not found")
    if db.query(Team).filter_by(league_id=team.league_id, owner_user_id=new_user_id).first() is not None:
        raise ValueError("That user already manages a team in this league")

    previous = team.owner_user_id
    team.owner_user_id = new_user_id
    cancelled = []
    for trade in db.query(Trade).filter(Trade.league_id == team.league_id, Trade.status == "proposed",
                                        (Trade.proposer_team_id == team.id) | (Trade.counterparty_team_id == team.id)):
        trade.status = "cancelled"
        trade.failure_reason = "The team's manager was replaced."
        trade.resolved_at = now
        cancelled.append(trade.id)
    db.flush()
    credits = credits_of(db, team)
    log.info("team %s manager %s -> %s; cancelled trades %s; credits %s", team.id, previous, new_user_id, cancelled, credits)
    return {"team_id": team.id, "previous_user_id": previous, "new_user_id": new_user_id,
            "cancelled_trade_ids": cancelled, "credits": credits}
