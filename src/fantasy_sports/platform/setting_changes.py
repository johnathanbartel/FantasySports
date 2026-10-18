"""Midseason changes to league_type (FREE/PAID) and league_format (redraft / keeper / dynasty) need every manager's approval.

People draft to the league's rules (a roster of 35-year-old veterans is ruined by a switch to dynasty), so once rosters exist
and the league isn't in its offseason the commissioner's change is put to a vote instead of applied. It applies when every
person-managed team has approved; a single rejection ends it. AI-owned and unassigned teams don't vote. In the offseason, or
before anyone has drafted, the change applies at once.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from fantasy_sports.platform import commissioner
from fantasy_sports.platform.db.models.core import (
    League, LeagueSettingChange, LeagueSettingVote, RosterEntry, Team,
)

log = logging.getLogger(__name__)

VOTED_FIELDS = ("league_type", "league_format")


def needs_vote(db: Session, league: League) -> bool:
    """A change is midseason when the league isn't in its offseason and rosters already exist."""
    if league.offseason:
        return False
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league.id)]
    return bool(team_ids) and db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids)).first() is not None


def voters(db: Session, league_id: int) -> list[Team]:
    return [t for t in db.query(Team).filter_by(league_id=league_id) if not t.is_ai_owned and t.owner_user_id is not None]


def open_change(db: Session, league_id: int, field: str) -> LeagueSettingChange | None:
    return db.query(LeagueSettingChange).filter_by(league_id=league_id, field=field, status="voting").first()


def propose(db: Session, league: League, field: str, new_value: str, user_id: int,
            now: datetime.datetime | None = None) -> LeagueSettingChange:
    """Opens a vote on changing `field` to `new_value`. The commissioner's own team, if they manage one, approves by proposing."""
    now = now or datetime.datetime.utcnow()
    if field not in VOTED_FIELDS:
        raise ValueError(f"{field} isn't a voted setting")
    if getattr(league, field) == new_value:
        raise ValueError(f"The league's {field} is already {new_value}")
    existing = open_change(db, league.id, field)
    if existing is not None:
        raise ValueError(f"A vote to change {field} to {existing.new_value} is already open")
    change = LeagueSettingChange(league_id=league.id, field=field, old_value=getattr(league, field), new_value=new_value,
                                 proposed_by_user_id=user_id, status="voting", created_at=now)
    db.add(change)
    db.flush()
    log.info("league %s setting change %s proposed: %s %s -> %s", league.id, change.id, field, change.old_value, new_value)
    for team in voters(db, league.id):
        if team.owner_user_id == user_id:
            db.add(LeagueSettingVote(change_id=change.id, team_id=team.id, user_id=user_id, vote="approve", created_at=now))
    db.flush()
    _settle(db, league, change, now)
    return change


def vote(db: Session, change: LeagueSettingChange, user_id: int, decision: str,
         now: datetime.datetime | None = None) -> LeagueSettingChange:
    now = now or datetime.datetime.utcnow()
    if change.status != "voting":
        raise ValueError(f"This vote is closed ({change.status})")
    if decision not in ("approve", "reject"):
        raise ValueError("Vote must be 'approve' or 'reject'")
    league = _league(db, change)
    team = next((t for t in voters(db, league.id) if t.owner_user_id == user_id), None)
    if team is None:
        raise PermissionError("Only a manager in this league can vote on this change")
    existing = db.query(LeagueSettingVote).filter_by(change_id=change.id, team_id=team.id).first()
    if existing:
        existing.vote = decision
        existing.created_at = now
    else:
        db.add(LeagueSettingVote(change_id=change.id, team_id=team.id, user_id=user_id, vote=decision, created_at=now))
    db.flush()
    _settle(db, league, change, now)
    return change


def cancel(db: Session, change: LeagueSettingChange, user_id: int, now: datetime.datetime | None = None) -> LeagueSettingChange:
    now = now or datetime.datetime.utcnow()
    league = _league(db, change)
    if not commissioner.is_commissioner(db, league, user_id):
        raise PermissionError("Only the commissioner can withdraw a change")
    if change.status != "voting":
        raise ValueError(f"This vote is closed ({change.status})")
    change.status, change.resolved_at = "cancelled", now
    db.flush()
    return change


def _league(db: Session, change: LeagueSettingChange) -> League:
    return db.get(League, change.league_id)


def _settle(db: Session, league: League, change: LeagueSettingChange, now: datetime.datetime) -> None:
    """Applies the change once every voter has approved, kills it on any rejection. Voters are read fresh, so a manager who
    joined or left since the vote opened is counted as they stand now."""
    votes = {v.team_id: v.vote for v in change.votes}
    teams = voters(db, league.id)
    if any(votes.get(t.id) == "reject" for t in teams):
        change.status, change.resolved_at = "rejected", now
    elif teams and all(votes.get(t.id) == "approve" for t in teams):
        if change.field == "league_type" and change.new_value == "PAID" and not league.annual_dues_cents:
            change.status, change.resolved_at = "rejected", now        # can't charge dues that were never set
            log.warning("league %s setting change %s dropped: no dues amount", league.id, change.id)
        else:
            setattr(league, change.field, change.new_value)
            change.status, change.resolved_at = "applied", now
            log.info("league %s setting change %s applied: %s = %s", league.id, change.id, change.field, change.new_value)
    elif not teams:         # nobody to ask
        setattr(league, change.field, change.new_value)
        change.status, change.resolved_at = "applied", now
    db.flush()
