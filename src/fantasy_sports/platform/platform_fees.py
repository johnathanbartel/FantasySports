"""Platform plans and the per-season platform fee (the "deposit") - FS-036.

A league runs on one of two plans: AD_SUPPORTED (default; we earn ad revenue) or AD_FREE (each person-managed team pays a
flat per-season platform fee and sees no ads). The fee is a flat amount, never a percentage of league dues. It is
snapshotted onto the league when the plan becomes AD_FREE, so a later change to PLATFORM_FEE_CENTS doesn't alter a league
that is already running.

The deposit IS the platform fee for the season of the picks being traded: it is collected early, credited to that season,
and charged once per team per season. Nothing here moves money. A record is entered by hand (scripts/record_platform_fee.py),
the same way dues.py works; only 'paid' records count; the credit belongs to the franchise (the team row), so a replacement
manager inherits it; rows are never deleted. AI-owned teams have nobody to pay and owe nothing.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import League, PlatformSeasonFee, Team

log = logging.getLogger(__name__)

AD_SUPPORTED = "AD_SUPPORTED"
AD_FREE = "AD_FREE"
PLANS = (AD_SUPPORTED, AD_FREE)
STATUSES = ("pending", "paid", "failed", "refunded", "disputed")

# Placeholder until real ad data exists.
PLATFORM_FEE_CENTS = 1200


def secured_cents(db: Session, team_id: int, season: int) -> int:
    return sum(r.amount_cents for r in db.query(PlatformSeasonFee).filter_by(team_id=team_id, season_number=season, status="paid"))


def is_secured(db: Session, league: League, team_id: int, season: int) -> bool:
    """The season's platform fee is fully covered by confirmed payments. Only an AD_FREE league with a fee set can be secured."""
    return (league.platform_plan == AD_FREE and bool(league.platform_fee_cents)
            and secured_cents(db, team_id, season) >= league.platform_fee_cents)


def set_plan(db: Session, league: League, plan: str) -> League:
    """Switches the league's plan. Becoming AD_FREE snapshots the fee (an existing snapshot is kept). Going back to
    AD_SUPPORTED keeps every payment record: the credits stay with the franchises. Flushes."""
    if plan not in PLANS:
        raise ValueError(f"Plan must be one of {', '.join(PLANS)}")
    if plan == AD_FREE and not league.platform_fee_cents:
        league.platform_fee_cents = PLATFORM_FEE_CENTS
    league.platform_plan = plan
    db.flush()
    log.info("league %s platform plan -> %s fee=%s", league.id, plan, league.platform_fee_cents)
    return league


def record_payment(db: Session, league: League, team: Team, season: int, amount_cents: int, status: str,
                   reference: str, recorded_by: str | None, now: datetime.datetime | None = None) -> PlatformSeasonFee:
    """Records a payment. Idempotent on the reference: repeating a reference returns the existing record. Refuses a 'paid'
    record for a season that is already secured, so the same season is never collected twice. Flushes."""
    now = now or datetime.datetime.utcnow()
    if league.platform_plan != AD_FREE or not league.platform_fee_cents:
        raise ValueError("This league isn't on the ad-free plan")
    if team.league_id != league.id:
        raise ValueError("Team is not in this league")
    if team.is_ai_owned:
        raise ValueError("AI-owned teams owe no platform fee")
    if status not in STATUSES:
        raise ValueError(f"Status must be one of {', '.join(STATUSES)}")
    if amount_cents <= 0:
        raise ValueError("Amount must be positive")
    if season < league.season_number:
        raise ValueError("That season is already over")
    existing = db.query(PlatformSeasonFee).filter_by(league_id=league.id, payment_reference=reference).one_or_none()
    if existing is not None:
        if (existing.team_id, existing.season_number, existing.amount_cents) != (team.id, season, amount_cents):
            raise ValueError("That payment reference was already used for a different payment")
        return existing
    if status == "paid" and is_secured(db, league, team.id, season):
        raise ValueError(f"Season {season} platform fee is already secured for {team.name}")
    row = PlatformSeasonFee(league_id=league.id, team_id=team.id, season_number=season, required_cents=league.platform_fee_cents,
                            amount_cents=amount_cents, status=status, payment_reference=reference, recorded_by=recorded_by,
                            created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    log.info("platform fee recorded league=%s team=%s season=%s status=%s ref=%s by=%s",
             league.id, team.id, season, status, reference, recorded_by)
    return row


def set_status(db: Session, league: League, row: PlatformSeasonFee, status: str, recorded_by: str | None,
               now: datetime.datetime | None = None) -> PlatformSeasonFee:
    """Moves a record to a new status. A record that backed an executed future-pick trade can't be refunded; it can still be
    disputed or fail (chargebacks happen), which leaves the season unsecured so the next pick trade is blocked. Flushes."""
    now = now or datetime.datetime.utcnow()
    if row.league_id != league.id:
        raise ValueError("Payment is not in this league")
    if status not in STATUSES:
        raise ValueError(f"Status must be one of {', '.join(STATUSES)}")
    if status == "refunded" and row.committed_by_trade_id is not None:
        raise ValueError(f"This payment backed executed trade {row.committed_by_trade_id} and can't be refunded")
    if status == "paid" and row.status != "paid":
        others = sum(r.amount_cents for r in db.query(PlatformSeasonFee).filter_by(
            team_id=row.team_id, season_number=row.season_number, status="paid"))
        if others >= (league.platform_fee_cents or 0):
            raise ValueError(f"Season {row.season_number} platform fee is already secured for that team")
    if row.committed_by_trade_id is not None and status in ("disputed", "failed") and row.status == "paid":
        log.warning("platform fee on committed trade %s moved to %s: team %s season %s is no longer secured",
                    row.committed_by_trade_id, status, row.team_id, row.season_number)
    log.info("platform fee status league=%s row=%s %s->%s by=%s", league.id, row.id, row.status, status, recorded_by)
    row.status = status
    row.updated_at = now
    db.flush()
    return row


def ads_enabled(db: Session, league: League, team: Team) -> bool:
    """False only when the league is AD_FREE and this team's current-season fee is secured. AI teams have no viewer, so True."""
    if team.is_ai_owned or league.platform_plan != AD_FREE:
        return True
    return not is_secured(db, league, team.id, league.season_number)
