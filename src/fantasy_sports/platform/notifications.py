"""
League emails. The platform has no mail provider yet, so this is the seam one plugs into:

  * queue_*  writes EmailOutbox rows (inside the caller's transaction, so a rolled-back change sends nothing).
  * deliver_pending(db) sends what is queued. With no SMTP server configured (FSC_SMTP_HOST unset) a row is marked
    "not_configured" - recorded, never sent - so development and the smoke tests make no outside calls.

SMTP settings come from the environment: FSC_SMTP_HOST, FSC_SMTP_PORT (587), FSC_SMTP_USER, FSC_SMTP_PASSWORD,
FSC_SMTP_FROM (defaults to the user), FSC_SMTP_STARTTLS (1). The send runs after the response (FastAPI BackgroundTasks),
and a failure is recorded on the row, never raised at the person who changed the setting.

Who is told about a league settings change: every person in the league - managers of person-owned teams and the
commissioners - once each, with the list of what changed and who changed it.
"""

from __future__ import annotations

import datetime
import logging
import os
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from fantasy_sports.platform import commissioner
from fantasy_sports.platform.db.models.core import EmailOutbox, League, Team, User

log = logging.getLogger(__name__)

SETTINGS_CHANGED = "settings_changed"

# League columns whose change is worth an email, with the label people know them by and how to show the value.
WATCHED_SETTINGS: dict[str, str] = {
    "name": "League name",
    "league_format": "League format",
    "league_type": "League type",
    "annual_dues_cents": "Annual dues",
    "keeper_draft_style": "Keeper draft style",
    "keeper_limit": "Keeper limit",
    "keeper_deadline": "Keeper deadline",
    "pick_trading_enabled": "Draft pick trading",
    "pick_trade_seasons": "Seasons of picks that can be traded",
    "trade_deadline": "Trade deadline",
    "trade_vote_extra_votes": "Extra votes a league-vote trade needs",
    "commissioner_trade_vote_extra_votes": "Extra votes when the commissioner's team trades",
    "trade_review_hours": "Trade review window (hours)",
    "trade_review_mode": "Who can veto a trade",
    "platform_plan": "Platform plan",
    "standings_mode": "Standings sorted by",
    "points_per_win": "Points per win",
    "points_per_tie": "Points per tie",
    "playoff_teams": "Playoff teams",
    "tiebreakers": "Tiebreakers",
    "playoff_byes": "Playoff byes",
    "division_winners_qualify": "Division winners qualify",
    "division_winners_seeded_first": "Division winners seeded first",
    "playoff_consolation": "Consolation playoffs",
    "visibility": "Visibility",
    "allow_ai_takeover_in_season": "Taking over AI teams in season",
    "unfilled_slot_policy": "Unfilled slots",
    "slug": "Custom URL",
}


def snapshot(league: League) -> dict:
    """The watched settings as they are now (call before a change, and again after)."""
    return {k: getattr(league, k) for k in WATCHED_SETTINGS}


def _show(key: str, value) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "on" if value else "off"
    if key == "annual_dues_cents":
        return f"${value / 100:,.2f}"
    if isinstance(value, datetime.datetime):
        return f"{value:%Y-%m-%d %H:%M} UTC"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "default"
    return str(value)


def diff(before: dict, after: dict) -> list[str]:
    """One human-readable line per watched setting that changed."""
    return [f"{WATCHED_SETTINGS[k]}: {_show(k, before[k])} -> {_show(k, after[k])}" for k in WATCHED_SETTINGS if before[k] != after[k]]


def recipients(db: Session, league: League) -> list[User]:
    """Everyone in the league with an email: commissioners and the managers of person-owned teams, once each."""
    ids = set(commissioner.commissioner_ids(db, league))
    ids |= {t.owner_user_id for t in db.query(Team).filter_by(league_id=league.id) if t.owner_user_id is not None and not t.is_ai_owned}
    return [u for u in db.query(User).filter(User.id.in_(ids or {0})).order_by(User.id) if u.email]


def _queue(db: Session, league: League, kind: str, subject: str, body: str, now: datetime.datetime) -> list[EmailOutbox]:
    rows = [EmailOutbox(league_id=league.id, kind=kind, to_email=u.email, subject=subject[:200], body=body, status="queued",
                        created_at=now) for u in recipients(db, league)]
    db.add_all(rows)
    db.flush()
    return rows


def queue_settings_changed(db: Session, league: League, changes: list[str], who: str,
                           now: datetime.datetime | None = None) -> list[EmailOutbox]:
    """Queues one email per person in the league listing what changed and who changed it (`who`: a name, or "The league (by
    vote)"). No changes, no email."""
    if not changes:
        return []
    now = now or datetime.datetime.utcnow()
    body = (f"{who} changed the settings of {league.name}:\n\n" + "\n".join(f"  - {c}" for c in changes)
            + "\n\nYou can see the current settings on the league's League Settings tab.")
    log.info("league %s settings changed by %s: %s", league.id, who, "; ".join(changes))
    return _queue(db, league, SETTINGS_CHANGED, f"{league.name}: league settings changed", body, now)


def queue_message(db: Session, league: League, kind: str, subject: str, body: str,
                  now: datetime.datetime | None = None) -> list[EmailOutbox]:
    """A free-form league email (a proposed setting change, a new co-commissioner...)."""
    return _queue(db, league, kind, subject, body, now or datetime.datetime.utcnow())


# ---- sending ------------------------------------------------------------------------------------------------------
def smtp_configured() -> bool:
    return bool(os.environ.get("FSC_SMTP_HOST"))


def _send_smtp(row: EmailOutbox) -> None:
    user = os.environ.get("FSC_SMTP_USER")
    msg = EmailMessage()
    msg["From"] = os.environ.get("FSC_SMTP_FROM") or user or "noreply@localhost"
    msg["To"] = row.to_email
    msg["Subject"] = row.subject
    msg.set_content(row.body)
    with smtplib.SMTP(os.environ["FSC_SMTP_HOST"], int(os.environ.get("FSC_SMTP_PORT", "587")), timeout=15) as smtp:
        if os.environ.get("FSC_SMTP_STARTTLS", "1") == "1":
            smtp.starttls()
        if user:
            smtp.login(user, os.environ.get("FSC_SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def deliver_pending(db: Session | None = None) -> int:
    """Sends every queued email. Returns how many went out. Opens its own session when called as a background task."""
    from fantasy_sports.platform.db.session import SessionLocal

    own = db is None
    db = db or SessionLocal()
    sent = 0
    try:
        for row in db.query(EmailOutbox).filter_by(status="queued").order_by(EmailOutbox.id).all():
            if not smtp_configured():
                row.status = "not_configured"
                continue
            try:
                _send_smtp(row)
                row.status, row.sent_at = "sent", datetime.datetime.utcnow()
                sent += 1
            except Exception as e:                    # a mail failure must never break the request that queued it
                row.status, row.error = "failed", str(e)[:300]
                log.warning("email %s to %s failed: %s", row.id, row.to_email, e)
        db.commit()
    finally:
        if own:
            db.close()
    return sent
