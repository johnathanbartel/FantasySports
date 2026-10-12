"""
How an AI-owned team (or a human whose clock ran out) picks in a snake draft.

The old rule was "best last-season points on the board", which ignores the lineup: a team could draft six centres and
no goalie. This rule drafts for the lineup:

  * Value over replacement. A player's worth is his points minus the points of the replacement-level player at his
    position - the player who would be left once every team has filled its open starting slots there. That makes a
    scarce position (a goalie, a tight end) worth reaching for, and a deep one (wingers) worth waiting on.
  * Needs first. A player who fills an open starting slot counts at full value; one who would only sit on the bench
    counts at a quarter of it, and a second player at a one-slot position (a goalie, a kicker) at nothing.
  * Never miss a starter. Once the picks left equal the starting slots still open, only players who fill one of those
    slots are considered, single-position slots before flex slots, so the lineup always ends up full.
  * Deterministic: ties go to the better last-season points, then the board order. No randomness, so a draft replays.

Personality (Team.personality_profile) isn't consulted yet.
"""

from __future__ import annotations

import math
from collections import defaultdict

from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.api.schemas import DraftBoardAsset
from fantasy_sports.platform import lineups, pecking_order
from fantasy_sports.platform.db.models.core import Draft, League, RosterEntry, Team
from fantasy_sports.platform.league_features import roster_limit
from fantasy_sports.platform.roster_fill import fill_slots

BENCH_WEIGHT = 0.25
STARTER_BONUS = 0.001   # a starter beats an equal-value bench player


def _pts(a: DraftBoardAsset) -> float:
    return a.points if a.points is not None else 0.0


def choose_pick(db: Session, draft: Draft, team_id: int, board: list[DraftBoardAsset]) -> DraftBoardAsset:
    """The board asset this team should take. `board` is best-first; it must not be empty."""
    try:
        slots = lineups.league_slots(db, draft.league_id)
    except ValueError:
        return board[0]
    league = db.get(League, draft.league_id)
    teams = [t.id for t in db.query(Team).filter(Team.league_id == draft.league_id)]
    rosters: dict[int, list] = defaultdict(list)
    for e in db.query(RosterEntry).filter(RosterEntry.team_id.in_(teams)).order_by(RosterEntry.id):
        rosters[e.team_id].append((e.draftable_asset_id, draftable_asset_position(e.draftable_asset)))

    starter_codes = {s.slot_code for s in slots if s.is_starter}
    on_board = {a.position for a in board if a.position}

    def open_starters(team: int):
        """[(slot, free spots)] for the starting slots this team has not filled."""
        filled, _ = fill_slots(slots, rosters[team])
        return [(s, s.slot_count - len(c)) for s, c in filled if s.slot_code in starter_codes and len(c) < s.slot_count]

    # League-wide demand for each position's starting slots; a flex slot's spots are shared across its positions.
    demand: dict[str, float] = defaultdict(float)
    for t in teams:
        for s, free in open_starters(t):
            eligible = [p for p in s.eligible_positions if p in on_board]
            for p in eligible:
                demand[p] += free / len(eligible)

    by_pos: dict[str, list[float]] = defaultdict(list)
    for a in board:
        if a.position:
            by_pos[a.position].append(_pts(a))
    replacement = {
        p: (pts[int(round(demand[p]))] if int(round(demand[p])) < len(pts) else 0.0) for p, pts in by_pos.items()
    }

    mine = open_starters(team_id)
    flex_positions = set()
    for s, _ in mine:
        for p in s.eligible_positions:
            if any(p in o.eligible_positions for o in slots if o.is_starter and o.id != s.id):
                flex_positions.add(p)
    need_positions = {p for s, _ in mine for p in s.eligible_positions}
    specific_positions = {p for s, _ in mine if not set(s.eligible_positions) & flex_positions
                          for p in s.eligible_positions}
    open_total = sum(free for _, free in mine)
    picks_left = (roster_limit(db, league) if league else 0) - len(rosters[team_id])
    forced = open_total > 0 and picks_left <= open_total
    if forced:
        allowed = (specific_positions or need_positions)
        if not any(a.position in allowed for a in board):
            allowed = need_positions

    # Starting spots at each position already held, to stop a second goalie/kicker being drafted for the bench.
    have: dict[str, int] = defaultdict(int)
    for _, p in rosters[team_id]:
        if p:
            have[p] += 1
    starter_slots_for = {p: sum(s.slot_count for s in slots if s.is_starter and p in s.eligible_positions) for p in on_board}

    # A hockey roster is exactly the four lines (C 4, W 8, D 8, G 4 = 24): a player beyond his group's depth would sit
    # on no line, so take one only when nothing else is left.
    depth_have: dict[str, int] = defaultdict(int)
    hockey = False
    try:
        pecking_order.assert_hockey_team(db, db.get(Team, team_id))
        hockey = True
        for _, p in rosters[team_id]:
            depth_have[pecking_order.GROUP_BY_POSITION.get(p)] += 1
    except ValueError:
        pass

    best, best_key = None, None
    for i, a in enumerate(board):
        p = a.position
        vorp = _pts(a) - replacement.get(p, 0.0)
        group = pecking_order.GROUP_BY_POSITION.get(p)
        if hockey and group and depth_have[group] >= pecking_order.DEPTH[group] and not forced:
            key = (-1e9, _pts(a), -i)
            if best_key is None or key > best_key:
                best, best_key = a, key
            continue
        if p in need_positions:
            value = vorp + STARTER_BONUS
        elif forced:
            continue
        elif starter_slots_for.get(p, 0) <= 1 and have[p] >= starter_slots_for.get(p, 0):
            value = 0.0
        else:
            value = BENCH_WEIGHT * max(vorp, 0.0)
        if forced and p not in allowed:
            continue
        key = (value, _pts(a), -i)
        if best_key is None or key > best_key:
            best, best_key = a, key
    return best or board[0]
