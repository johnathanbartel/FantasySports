"""
Conference assignment. A league's teams are grouped into conferences by
program_size tier (see db/models/core.py::Conference and
platform/team_tiers.py for the tier list). Every league starts with one
conference per tier; growing conference count is only ever done one full
tier-set at a time (expand_conferences), so the total is always a multiple
of the number of tiers - the rule the commissioner described (a 2-tier
league could only grow 2 at a time). Per-league tier subsetting - a league
that only uses some of the tiers - isn't implemented; every league gets all
of PROGRAM_SIZES today, so that "multiple" is always len(PROGRAM_SIZES).

Domain-rule violations raise ValueError, same convention as
platform/auction/engine.py - the API layer catches it and raises an
HTTPException.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import Conference, Team
from fantasy_sports.platform.team_tiers import PROGRAM_SIZES


def create_baseline_conferences(db: Session, league_id: int) -> list[Conference]:
    """One conference per tier - the minimum every league starts with."""
    conferences = [
        Conference(league_id=league_id, program_size=size, name=f"{size.capitalize()} Conference 1")
        for size in PROGRAM_SIZES
    ]
    db.add_all(conferences)
    db.flush()
    return conferences


def expand_conferences(db: Session, league_id: int) -> list[Conference]:
    """Adds exactly one new conference per tier, keeping every tier's
    conference count equal."""
    new_conferences = []
    for size in PROGRAM_SIZES:
        existing_count = (
            db.query(Conference)
            .filter(Conference.league_id == league_id, Conference.program_size == size)
            .count()
        )
        if existing_count == 0:
            raise ValueError(f"League has no '{size}' conference yet - create baseline conferences first")
        new_conferences.append(
            Conference(
                league_id=league_id,
                program_size=size,
                name=f"{size.capitalize()} Conference {existing_count + 1}",
            )
        )
    db.add_all(new_conferences)
    db.flush()
    return new_conferences


def assign_team_to_conference(db: Session, league_id: int, program_size: str) -> Conference:
    """Places a team into the least-populated conference of its tier -
    simple load balancing across however many conferences that tier has."""
    conferences = (
        db.query(Conference)
        .filter(Conference.league_id == league_id, Conference.program_size == program_size)
        .all()
    )
    if not conferences:
        raise ValueError(f"League has no '{program_size}' conference yet")

    conference_ids = [c.id for c in conferences]
    team_counts = {conference_id: 0 for conference_id in conference_ids}
    for (conference_id,) in db.query(Team.conference_id).filter(Team.conference_id.in_(conference_ids)):
        team_counts[conference_id] += 1

    return min(conferences, key=lambda c: team_counts[c.id])
