"""
Weekly lineups: which slots a league has, what a valid lineup is, and the best
lineup a roster can field.

A lineup is a set of (slot, player/defense-group) assignments for one week. The
slots come from RosterSlotDefinition (QB, RB x2, WR x2, TE, FLEX, DL, LB, DB, K,
plus a bench), filtered to the league's defense mode. Validation rules: every
assigned asset is on the team's roster, sits in a slot that accepts its
position, no slot is over its count, and no asset is used twice.

best_lineup() is what an AI team (or the "auto-fill" button) uses: it fills
each position slot with the highest scorer at that position, then the FLEX from
what's left, then the bench. "Highest scorer" is last season's ESPN-standard
points, with the demo dataset's weekly EPA total as the tiebreak (which is all
that separates defense groups, who have no ESPN points).

MOCK-ONLY SHORTCUT: like the recruiting AI, this ranks by full-2024 stats.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    LeagueUniverse,
    LineupEntry,
    RosterEntry,
    RosterSlotDefinition,
    Team,
)
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset
from fantasy_sports.sports.football.models import WeeklyPlayerScore

BENCH = "BENCH"


def league_slots(db: Session, league_id: int) -> list[RosterSlotDefinition]:
    """The lineup slots for a league's (first) universe and defense mode, in a
    stable display order: starters first (position slots, then FLEX), bench last."""
    lu = db.query(LeagueUniverse).filter_by(league_id=league_id).first()
    if lu is None:
        raise ValueError("League has no universe assigned")
    rows = [
        s for s in db.query(RosterSlotDefinition).filter_by(universe_id=lu.universe_id)
        if s.defense_mode is None or s.defense_mode == lu.defense_mode
    ]

    def order(s: RosterSlotDefinition):
        return (not s.is_starter, len(s.eligible_positions) > 1, s.id)

    return sorted(rows, key=order)


def roster_assets(db: Session, team_id: int) -> list[DraftableAsset]:
    return [e.draftable_asset for e in db.query(RosterEntry).filter_by(team_id=team_id)]


def build_lineup(
    db: Session, team: Team, assignments: dict[str, int | list[int]]
) -> list[tuple[RosterSlotDefinition, int]]:
    """Validates a slot_code -> asset id (or list of ids, for slots that hold
    several players) mapping and returns (slot, asset_id) pairs. Raises
    ValueError with a message meant for the user."""
    slots = {s.slot_code: s for s in league_slots(db, team.league_id)}
    on_roster = {a.id: a for a in roster_assets(db, team.id)}
    pairs: list[tuple[RosterSlotDefinition, int]] = []
    used: set[int] = set()
    for code, value in assignments.items():
        slot = slots.get(code)
        if slot is None:
            raise ValueError(f"Unknown roster slot '{code}' for this league")
        ids = value if isinstance(value, list) else [value]
        if len(ids) > slot.slot_count:
            raise ValueError(f"Too many players in {code}: it holds {slot.slot_count}, got {len(ids)}")
        for asset_id in ids:
            asset = on_roster.get(asset_id)
            if asset is None:
                raise ValueError(f"Asset {asset_id} isn't on this team's roster")
            if asset_id in used:
                raise ValueError("A player can only be in one slot")
            position = draftable_asset_position(asset)
            if position not in slot.eligible_positions:
                raise ValueError(f"A {position} can't play {code} (eligible: {', '.join(slot.eligible_positions)})")
            used.add(asset_id)
            pairs.append((slot, asset_id))
    return pairs


def save_lineup(db: Session, team: Team, week_id: int, pairs: list[tuple[RosterSlotDefinition, int]]) -> list[LineupEntry]:
    """Replaces the team's lineup for the week."""
    db.query(LineupEntry).filter(LineupEntry.team_id == team.id, LineupEntry.week_id == week_id).delete()
    entries = []
    for slot, asset_id in pairs:
        entry = LineupEntry(team_id=team.id, week_id=week_id, draftable_asset_id=asset_id, roster_slot_id=slot.id)
        db.add(entry)
        entries.append(entry)
    db.flush()
    return entries


def best_lineup(db: Session, team: Team) -> dict[str, list[int]]:
    """slot_code -> asset ids: the best legal lineup this roster can field,
    bench included. Slots with nobody to fill them are left short (partial
    rosters are allowed)."""
    slots = league_slots(db, team.league_id)
    roster = roster_assets(db, team.id)
    season = season_points_by_asset(db)
    weekly: dict[int, float] = defaultdict(float)
    for w in db.query(WeeklyPlayerScore).filter(WeeklyPlayerScore.draftable_asset_id.in_([a.id for a in roster] or [0])):
        weekly[w.draftable_asset_id] += w.points

    def strength(a: DraftableAsset):
        return (season.get(a.id, 0.0), weekly.get(a.id, 0.0))

    remaining = sorted(roster, key=strength, reverse=True)
    out: dict[str, list[int]] = {}
    for slot in slots:   # starters (position slots first, FLEX after), then bench
        chosen = []
        for a in list(remaining):
            if len(chosen) == slot.slot_count:
                break
            if draftable_asset_position(a) in slot.eligible_positions:
                chosen.append(a)
                remaining.remove(a)
        if chosen:
            out[slot.slot_code] = [a.id for a in chosen]
    return out
