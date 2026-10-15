"""
Injuries and the injured list (hockey).

A player's real-world injury report lives on the Player row (status + note). A fantasy team may move an injured
player of its own onto its injured list (IL): he comes off the lines, is skipped when starters are resolved and
scored (those paths read pecking_order.roster_players, which leaves IL players out), and stops counting toward the
roster limit so the manager can sign a replacement. Activating him puts him back on the roster; he lands at the end
of his position's lines if there is room, otherwise in "Not on a line".

Nothing here fetches injury data: statuses are set by set_injury() (a feed or a commissioner tool can call it).
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import pecking_order
from fantasy_sports.platform.db.models.core import Player, RosterEntry, Team

IL_CAPACITY = 3

# Day-to-day players may still play, so they stay on the lines; the others are out for a stretch.
STATUSES = {"DTD": "Day-to-day", "OUT": "Out", "IR": "Injured reserve", "LTIR": "Long-term injured reserve"}
IL_ELIGIBLE = {"OUT", "IR", "LTIR"}


def set_injury(db: Session, player: Player, status: str | None, note: str | None = None,
               now: datetime.datetime | None = None) -> None:
    """Sets (or with status None clears) a player's injury report. A player who heals while on an injured list stays
    there until his manager activates him. Flushes, does not commit. Raises ValueError for an unknown status."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"Unknown injury status {status!r} (use {', '.join(STATUSES)})")
    player.injury_status = status
    player.injury_note = (note or None) if status else None
    player.injury_updated_at = now or datetime.datetime.utcnow()
    db.flush()


def injured_list_entries(db: Session, team_id: int) -> list[RosterEntry]:
    return db.query(RosterEntry).filter_by(team_id=team_id, on_injured_list=True).order_by(RosterEntry.id).all()


def _entry(db: Session, team: Team, asset_id: int) -> RosterEntry:
    entry = db.query(RosterEntry).filter_by(team_id=team.id, draftable_asset_id=asset_id).first()
    if entry is None:
        raise ValueError("That player isn't on this team's roster")
    return entry


def place_on_injured_list(db: Session, team: Team, asset_id: int) -> None:
    """Raises ValueError (message meant for the user) unless the player is on this hockey team, out injured, and the
    list has room. Takes him off the lines. Flushes, does not commit."""
    pecking_order.assert_hockey_team(db, team)
    entry = _entry(db, team, asset_id)
    asset = entry.draftable_asset
    player = asset.player if asset.asset_type == "player" else None
    if player is None:
        raise ValueError("Only players can go on the injured list")
    if entry.on_injured_list:
        raise ValueError("He's already on the injured list")
    if player.injury_status not in IL_ELIGIBLE:
        raise ValueError(f"Only injured players can go on the injured list (status must be {', '.join(sorted(IL_ELIGIBLE))})")
    if len(injured_list_entries(db, team.id)) >= IL_CAPACITY:
        raise ValueError(f"The injured list is full ({IL_CAPACITY}) - activate someone first")
    pecking_order.unrank_player(db, team, asset_id)
    entry.on_injured_list = True
    db.flush()


def activate_from_injured_list(db: Session, team: Team, asset_id: int, roster_limit: int) -> bool:
    """Brings a player back onto the active roster (even if he is still hurt: the manager's call). Raises ValueError if
    he isn't on the list or the active roster is full. Returns whether he was ranked on a line."""
    pecking_order.assert_hockey_team(db, team)
    entry = _entry(db, team, asset_id)
    if not entry.on_injured_list:
        raise ValueError("He isn't on the injured list")
    active = db.query(RosterEntry).filter_by(team_id=team.id, on_injured_list=False).count()
    if active >= roster_limit:
        raise ValueError(f"Roster is full ({roster_limit}) - drop a player to make room")
    entry.on_injured_list = False
    db.flush()
    return pecking_order.rank_new_player(db, team, asset_id)
