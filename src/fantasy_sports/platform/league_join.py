"""Joining a league: open human slots, taking over AI teams, and the pre-draft slot cutoff.

A league's teams are each a person, an AI team, or an open human slot. An open human slot is reserved for a person: until
someone claims it the AI plays it (it is AI-owned with Team.open_human_slot set), so nothing downstream needs to know about it.

  * Open slot: anyone with the league link can claim it, public or private, until the draft starts.
  * Taking over an AI team: public leagues only. Before the draft any AI team can be taken over. While the draft runs nothing
    can. Once the season has begun (draft complete) it needs League.allow_ai_takeover_in_season.
  * An hour before the scheduled draft, open slots nobody claimed are resolved by League.unfilled_slot_policy: 'fill_ai' leaves
    them as ordinary AI teams, 'remove' deletes them and shrinks the league. Starting the draft early resolves them at once.
    Nothing runs on a timer: the cutoff is applied the next time the league, its teams or its join options are read, and when
    the draft starts.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import League, Team, Trade

SLOT_CUTOFF = datetime.timedelta(hours=1)
MIN_LEAGUE_SIZE = 2


def slot_cutoff(league: League) -> datetime.datetime | None:
    """When open slots stop being claimable and the policy applies; None if no draft time is set."""
    return league.draft_scheduled_at - SLOT_CUTOFF if league.draft_scheduled_at else None


def apply_slot_cutoff(db: Session, league: League, now: datetime.datetime | None = None, force: bool = False) -> int:
    """Resolves the league's unclaimed open slots per its policy once the cutoff has passed (or at once with force, which the
    draft start uses). Flushes; the caller commits. Returns how many slots were resolved."""
    now = now or datetime.datetime.utcnow()
    cutoff = slot_cutoff(league)
    if not force and (cutoff is None or now < cutoff):
        return 0
    slots = db.query(Team).filter(Team.league_id == league.id, Team.open_human_slot.is_(True)).order_by(Team.id).all()
    if not slots:
        return 0
    removable = max(0, league.size - MIN_LEAGUE_SIZE)
    for team in slots:
        if league.unfilled_slot_policy == "remove" and removable > 0:
            db.delete(team)
            league.size -= 1
            removable -= 1
        else:
            team.open_human_slot = False        # stays an AI team
    if league.playoff_teams is not None:
        league.playoff_teams = min(league.playoff_teams, league.size)
    league.playoff_byes = min(league.playoff_byes, league.playoff_teams or league.size)
    db.flush()
    return len(slots)


def join_blocker(db: Session, league: League, user_id: int | None) -> str | None:
    """Why this user can't join the league at all right now, or None."""
    from fantasy_sports.platform.league_features import draft_state

    if user_id is not None and db.query(Team.id).filter_by(league_id=league.id, owner_user_id=user_id).first():
        return "You already manage a team in this league."
    if draft_state(db, league.id) == "in_progress":
        return "The draft is in progress; teams can't be joined until it finishes."
    return None


def joinable_teams(db: Session, league: League, user_id: int | None = None) -> tuple[list[tuple[Team, str]], str | None]:
    """(team, kind) pairs the user could join and, when there are none, the reason. kind is 'open_slot' or 'ai_takeover'."""
    from fantasy_sports.platform.league_features import draft_state

    blocker = join_blocker(db, league, user_id)
    if blocker:
        return [], blocker
    state = draft_state(db, league.id)
    teams = db.query(Team).filter(Team.league_id == league.id, Team.is_ai_owned.is_(True)).order_by(Team.id).all()
    out: list[tuple[Team, str]] = []
    for t in teams:
        if t.open_human_slot:
            out.append((t, "open_slot"))
        elif league.visibility == "public" and (state == "not_started" or league.allow_ai_takeover_in_season):
            out.append((t, "ai_takeover"))
    if out:
        return out, None
    if league.visibility != "public":
        return [], "This league has no open slots. Ask the commissioner for an invite."
    if state != "not_started" and not league.allow_ai_takeover_in_season:
        return [], "The season has begun and this league doesn't allow taking over AI teams."
    return [], "Every team in this league already has a manager."


def join(db: Session, league: League, user_id: int, team_id: int, team_name: str | None = None,
         now: datetime.datetime | None = None) -> Team:
    """Gives the user the open slot or AI team. Flushes; the caller commits. Raises ValueError with a user-facing reason."""
    now = now or datetime.datetime.utcnow()
    options, reason = joinable_teams(db, league, user_id)
    team = next((t for t, _ in options if t.id == team_id), None)
    if team is None:
        raise ValueError(reason or "That team can't be joined.")
    team.owner_user_id = user_id
    team.is_ai_owned = False
    team.open_human_slot = False
    if team_name and team_name.strip():
        team.name = team_name.strip()
    # Offers the AI made or was sent were its own decisions, not the new manager's.
    for trade in db.query(Trade).filter(Trade.league_id == league.id, Trade.status == "proposed",
                                        (Trade.proposer_team_id == team.id) | (Trade.counterparty_team_id == team.id)):
        trade.status = "cancelled"
        trade.failure_reason = "The team was taken over by a new manager."
        trade.resolved_at = now
    db.flush()
    return team
