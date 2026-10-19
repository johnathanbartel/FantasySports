"""Mock drafts: a practice draft anyone can join, built on an ordinary league.

A mock draft is a League with is_mock_draft set, so the draft room, the AI pickers and the join rules are the ones real leagues
use. What differs:

  * The creator picks the sport, the number of teams, how many of them are human slots (the creator takes the first), a
    start time, a pick clock, and whether the draft is public (listed for anyone) or private (reached by its link).
  * The teams that aren't human slots are AI teams. Human slots nobody has claimed stay open until the start time.
  * When the start time passes, the draft starts by itself (see advance in api/routers/mock_drafts.py). Unclaimed human
    slots are resolved by League.unfilled_slot_policy: 'fill_ai' (the "AI can take empty human slots" box) turns them into AI
    teams so the draft keeps its size, 'remove' deletes them and the draft runs with the teams that are left.
  * Nothing carries on afterwards: no season, schedule or scoring. The finished board is the result.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import League, LeagueUniverse, Team, Universe

MIN_TEAMS = 2
MAX_TEAMS = 32
MAX_LEAD_TIME = datetime.timedelta(days=60)
PAST_GRACE = datetime.timedelta(minutes=1)      # a start time of "now" arrives a moment late
DEFAULT_PICK_SECONDS = 60
OPEN_SLOT_NAME = "Open slot"


class MockDraftError(ValueError):
    """A mock draft request that can't be honoured; the message is meant for the person."""


def normalize_start(value: datetime.datetime, now: datetime.datetime) -> datetime.datetime:
    """The start time as naive UTC (what the database stores), checked to be neither past nor far off."""
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    if value < now - PAST_GRACE:
        raise MockDraftError("The start time has already passed.")
    if value > now + MAX_LEAD_TIME:
        raise MockDraftError("A mock draft can be scheduled at most 60 days ahead.")
    return value


def validate_shape(size: int, human_slots: int) -> None:
    if not MIN_TEAMS <= size <= MAX_TEAMS:
        raise MockDraftError(f"A mock draft needs between {MIN_TEAMS} and {MAX_TEAMS} teams.")
    if not 1 <= human_slots <= size:
        raise MockDraftError(f"Human slots must be between 1 (you) and {size}.")


def create(db: Session, *, creator, name: str, universe: Universe, size: int, human_slots: int, rounds: int,
           start_at: datetime.datetime, visibility: str, fill_ai_if_unfilled: bool, pick_seconds: int,
           ai_names: list[str]) -> League:
    """Adds the league and its teams: the creator's team, the other human slots (open), and AI teams for the rest.
    Flushes; the caller commits."""
    league = League(
        name=name, commissioner_user_id=creator.id, size=size, draft_rounds=rounds, draft_type="snake",
        draft_scheduled_at=start_at, visibility=visibility, is_mock_draft=True, mock_pick_seconds=pick_seconds,
        unfilled_slot_policy="fill_ai" if fill_ai_if_unfilled else "remove", draft_reset_policy="off",
    )
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=universe.id, defense_mode="group"))
    db.add(Team(league_id=league.id, name=f"{creator.display_name}'s team"[:80], owner_user_id=creator.id,
                is_ai_owned=False))
    for n in range(2, human_slots + 1):
        db.add(Team(league_id=league.id, name=f"{OPEN_SLOT_NAME} {n}", is_ai_owned=True, open_human_slot=True))
    names = iter(ai_names)
    for n in range(human_slots + 1, size + 1):
        db.add(Team(league_id=league.id, name=next(names, None) or f"AI Team {n}", is_ai_owned=True))
    db.flush()
    return league


def team_kind(team: Team) -> str:
    """'human' (a person has it), 'open' (reserved for a person, unclaimed) or 'ai'."""
    if team.open_human_slot:
        return "open"
    return "ai" if team.is_ai_owned else "human"


def leave(db: Session, league: League, user_id: int) -> Team:
    """Gives the user's slot back so someone else can take it. The creator can't leave (they'd cancel instead).
    Flushes; the caller commits. Raises MockDraftError."""
    team = db.query(Team).filter_by(league_id=league.id, owner_user_id=user_id).first()
    if team is None:
        raise MockDraftError("You aren't in this mock draft.")
    if user_id == league.commissioner_user_id:
        raise MockDraftError("The creator can't leave their own mock draft; cancel it instead.")
    n = db.query(Team).filter(Team.league_id == league.id, Team.open_human_slot.is_(True)).count() + 2
    team.owner_user_id = None
    team.is_ai_owned = True
    team.open_human_slot = True
    team.name = f"{OPEN_SLOT_NAME} {n}"
    db.flush()
    return team
