"""League dues in a PAID league (CFG-06): payment records per franchise per season.

There is no payment processor in the platform, so a commissioner records a confirmed payment by hand (with the processor's
or the cash receipt's reference). Only 'paid' records count; pending, failed, refunded and disputed ones never establish
eligibility. Credit belongs to the franchise (the team row), so a replacement manager inherits it. Records are never deleted.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import League, SeasonDues, Team

log = logging.getLogger(__name__)

STATUSES = ("pending", "paid", "failed", "refunded", "disputed")


def secured_cents(db: Session, team_id: int, season: int) -> int:
    return sum(r.amount_cents for r in db.query(SeasonDues).filter_by(team_id=team_id, season_number=season, status="paid"))


def is_secured(db: Session, league: League, team_id: int, season: int) -> bool:
    """The season's dues are fully covered by confirmed payments."""
    return bool(league.annual_dues_cents) and secured_cents(db, team_id, season) >= league.annual_dues_cents


def record_payment(db: Session, league: League, team: Team, season: int, amount_cents: int, status: str,
                   reference: str, user_id: int | None, now: datetime.datetime | None = None) -> SeasonDues:
    """Records a payment. Idempotent on the reference: repeating a reference returns the existing record. Refuses a 'paid'
    record for a season that is already secured, so the same season is never collected twice. Flushes."""
    now = now or datetime.datetime.utcnow()
    if league.league_type != "PAID" or not league.annual_dues_cents:
        raise ValueError("This league doesn't charge dues")
    if team.league_id != league.id:
        raise ValueError("Team is not in this league")
    if status not in STATUSES:
        raise ValueError(f"Status must be one of {', '.join(STATUSES)}")
    if amount_cents <= 0:
        raise ValueError("Amount must be positive")
    if season < league.season_number:
        raise ValueError("That season is already over")
    existing = db.query(SeasonDues).filter_by(league_id=league.id, payment_reference=reference).one_or_none()
    if existing is not None:
        if (existing.team_id, existing.season_number, existing.amount_cents) != (team.id, season, amount_cents):
            raise ValueError("That payment reference was already used for a different payment")
        return existing
    if status == "paid" and is_secured(db, league, team.id, season):
        raise ValueError(f"Season {season} dues are already secured for {team.name}")
    row = SeasonDues(league_id=league.id, team_id=team.id, season_number=season, required_cents=league.annual_dues_cents,
                     amount_cents=amount_cents, status=status, payment_reference=reference,
                     recorded_by_user_id=user_id, created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    log.info("dues recorded league=%s team=%s season=%s status=%s ref=%s by=%s", league.id, team.id, season, status, reference, user_id)
    return row


def set_status(db: Session, league: League, row: SeasonDues, status: str, user_id: int | None,
               now: datetime.datetime | None = None) -> SeasonDues:
    """Moves a record to a new status (pending -> paid, paid -> refunded / disputed, ...). A record that backed an executed
    future-pick trade (committed_by_trade_id) can't be refunded, only disputed or failed. Flushes."""
    now = now or datetime.datetime.utcnow()
    if row.league_id != league.id:
        raise ValueError("Payment is not in this league")
    if status not in STATUSES:
        raise ValueError(f"Status must be one of {', '.join(STATUSES)}")
    if status == "refunded" and row.committed_by_trade_id is not None:
        raise ValueError(f"This payment backed executed trade {row.committed_by_trade_id} and can't be refunded")
    if row.committed_by_trade_id is not None and status in ("disputed", "failed") and row.status == "paid":
        # A chargeback is possible; the executed trade stays, and the season is unsecured again for the next pick trade.
        log.warning("dues on committed trade %s moved to %s: team %s season %s is no longer secured",
                    row.committed_by_trade_id, status, row.team_id, row.season_number)
    if status == "paid" and row.status != "paid":
        others = sum(r.amount_cents for r in db.query(SeasonDues).filter_by(team_id=row.team_id, season_number=row.season_number,
                                                                          status="paid"))
        if others >= (league.annual_dues_cents or 0):
            raise ValueError(f"Season {row.season_number} dues are already secured for that team")
    log.info("dues status league=%s row=%s %s->%s by=%s", league.id, row.id, row.status, status, user_id)
    row.status = status
    row.updated_at = now
    db.flush()
    return row
