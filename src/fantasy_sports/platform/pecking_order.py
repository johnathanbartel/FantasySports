"""
NHL pecking order (docs/hockey_spec.md section 4): a team ranks its players per position group and
each day's starters are resolved from that ranking.

Depth: C and G rank 3 deep; W and D rank 6 deep. A daily game dresses 1 C, 2 W, 2 D, 1 G: the
highest-ranked ACTIVE players of each group (W and D are pooled, so the top two active of the six
dress), and the goalie must also be CONFIRMED as the starter. Activity and goalie confirmation are
passed in as data; nothing here fetches from an NHL service, and nothing here scores anything.

All ids in this module are Player ids (hockey rosters hold only players).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import LeagueUniverse, RosterEntry, Team, TeamPeckingOrder, Universe

GROUPS = ("C", "W", "D", "G")
DEPTH = {"C": 3, "W": 6, "D": 6, "G": 3}
STARTERS = {"C": 1, "W": 2, "D": 2, "G": 1}
GROUP_BY_POSITION = {"C": "C", "W": "W", "LW": "W", "RW": "W", "D": "D", "G": "G"}

NO_RANKED_PLAYERS = "NO_RANKED_PLAYERS"
INSUFFICIENT_RANKED = "INSUFFICIENT_RANKED"
NO_ACTIVE_PLAYER = "NO_ACTIVE_PLAYER"
NO_CONFIRMED_GOALIE = "NO_CONFIRMED_GOALIE"


@dataclass
class Vacancy:
    position_group: str
    slot: int  # 1-based starter slot within the group
    reason: str


@dataclass
class Resolution:
    starters: dict[str, list[int]] = field(default_factory=dict)
    vacancies: list[Vacancy] = field(default_factory=list)


def resolve_starters(
    order: dict[str, list[int]], scheduled_player_ids: set[int] | frozenset[int],
    confirmed_goalie_ids: set[int] | frozenset[int],
) -> Resolution:
    """Pure and deterministic. order maps a group to its player ids, highest priority first. A player
    is active if in scheduled_player_ids; a goalie additionally needs to be in confirmed_goalie_ids."""
    result = Resolution()
    for group in GROUPS:
        ranked = list(order.get(group, []))
        need = STARTERS[group]
        picked: list[int] = []
        for pid in ranked:
            if len(picked) == need:
                break
            if pid not in scheduled_player_ids:
                continue
            if group == "G" and pid not in confirmed_goalie_ids:
                continue
            picked.append(pid)
        result.starters[group] = picked
        for slot in range(len(picked) + 1, need + 1):
            if not ranked:
                reason = NO_RANKED_PLAYERS
            elif len(ranked) <= len(picked):
                reason = INSUFFICIENT_RANKED
            elif group == "G" and any(p in scheduled_player_ids for p in ranked):
                reason = NO_CONFIRMED_GOALIE
            else:
                reason = NO_ACTIVE_PLAYER
            result.vacancies.append(Vacancy(group, slot, reason))
    return result


WING_POSITIONS = {"W", "LW", "RW"}
CENTER_POSITIONS = {"C"}


def is_eligible_for_group(position: str | None, group: str) -> bool:
    """Returns True if the player's position is eligible for the position group.
    Supports dual forward eligibility (e.g. C/LW, C/RW, F) where a player can play both Center and Wing.
    """
    if not position:
        return False
    pos_parts = set(p.strip().upper() for p in position.replace('/', ',').replace('-', ',').split(','))
    if "F" in pos_parts:
        return group in ("C", "W")
    if group == "C":
        return bool(pos_parts & CENTER_POSITIONS)
    if group == "W":
        return bool(pos_parts & WING_POSITIONS)
    if group == "D":
        return bool(pos_parts & {"D", "LD", "RD", "DEF"})
    if group == "G":
        return bool(pos_parts & {"G", "GOALIE"})
    return False


def assert_hockey_team(db: Session, team: Team) -> None:
    nhl = (
        db.query(LeagueUniverse).join(Universe, Universe.id == LeagueUniverse.universe_id)
        .filter(LeagueUniverse.league_id == team.league_id, Universe.code == "nhl").first()
    )
    if nhl is None:
        raise ValueError("Pecking order is only available for NHL teams")


def roster_players(db: Session, team_id: int) -> dict[int, tuple[int, object]]:
    """player_id -> (draftable_asset_id, Player) for the team's rostered players."""
    out = {}
    for entry in db.query(RosterEntry).filter_by(team_id=team_id):
        asset = entry.draftable_asset
        if asset.asset_type == "player" and asset.player is not None:
            out[asset.player_id] = (asset.id, asset.player)
    return out


def load_order(db: Session, team_id: int) -> dict[str, list[int]]:
    """group -> player ids in priority order (every group present, possibly empty)."""
    order: dict[str, list[int]] = {g: [] for g in GROUPS}
    rows = db.query(TeamPeckingOrder).filter_by(team_id=team_id).order_by(TeamPeckingOrder.priority).all()
    for row in rows:
        if row.position_group in order and row.draftable_asset and row.draftable_asset.player_id:
            order[row.position_group].append(row.draftable_asset.player_id)
    return order


def replace_groups(db: Session, team: Team, assignments: dict[str, list[int]]) -> None:
    """Replaces the ranking of each named group (others untouched). Raises ValueError, before any
    change is flushed, for an unknown group, an over-long list, a duplicate, or a player who is
    missing, not on this team's roster, or not eligible for the group. The caller commits, or rolls
    back, so the update is all-or-nothing."""
    assert_hockey_team(db, team)
    if not assignments:
        return
    roster = roster_players(db, team.id)
    seen: set[int] = set()
    for group, ids in assignments.items():
        if group not in DEPTH:
            raise ValueError(f"Unknown position group {group!r} (use C, W, D or G)")
        if len(ids) > DEPTH[group]:
            raise ValueError(f"{group} can rank at most {DEPTH[group]} players, got {len(ids)}")
        for pid in ids:
            if pid in seen:
                raise ValueError(f"Player {pid} is listed more than once")
            seen.add(pid)
            if pid not in roster:
                raise ValueError(f"Player {pid} is not on this team's roster")
            position = roster[pid][1].position
            if not is_eligible_for_group(position, group):
                raise ValueError(f"Player {pid} ({position}) is not eligible for {group}")

    assigned_asset_ids = [roster[pid][0] for ids in assignments.values() for pid in ids if pid in roster]
    (db.query(TeamPeckingOrder)
     .filter(TeamPeckingOrder.team_id == team.id,
             (TeamPeckingOrder.position_group.in_(list(assignments))) |
             (TeamPeckingOrder.draftable_asset_id.in_(assigned_asset_ids)))
     .delete(synchronize_session=False))
    db.flush()
    for group, ids in assignments.items():
        for rank, pid in enumerate(ids, start=1):
            db.add(TeamPeckingOrder(team_id=team.id, position_group=group, priority=rank,
                                    draftable_asset_id=roster[pid][0]))
    db.flush()


def rank_new_player(db: Session, team: Team, asset_id: int) -> bool:
    """Puts a newly rostered NHL player at the end of his group's ranking, if the group has room and he is not already
    ranked, so a drafted team's lines fill in as it drafts. Returns whether he was ranked. Does nothing for a
    non-NHL team or a non-player asset. Flushes, does not commit."""
    try:
        assert_hockey_team(db, team)
    except ValueError:
        return False
    roster = roster_players(db, team.id)
    entry = next(((pid, pl) for pid, (aid, pl) in roster.items() if aid == asset_id), None)
    if entry is None:
        return False
    group = GROUP_BY_POSITION.get(entry[1].position)
    if group is None:
        return False
    # If primary forward group is full, flex to other forward group if room exists
    if group in ("C", "W"):
        cur_c = db.query(TeamPeckingOrder).filter_by(team_id=team.id, position_group="C").count()
        cur_w = db.query(TeamPeckingOrder).filter_by(team_id=team.id, position_group="W").count()
        if group == "C" and cur_c >= DEPTH["C"] and cur_w < DEPTH["W"]:
            group = "W"
        elif group == "W" and cur_w >= DEPTH["W"] and cur_c < DEPTH["C"]:
            group = "C"

    existing = db.query(TeamPeckingOrder).filter_by(team_id=team.id, position_group=group).count()
    if existing >= DEPTH[group] or db.query(TeamPeckingOrder).filter_by(team_id=team.id, draftable_asset_id=asset_id).first():
        return False
    db.add(TeamPeckingOrder(team_id=team.id, position_group=group, priority=existing + 1, draftable_asset_id=asset_id))
    db.flush()
    return True


def unrank_player(db: Session, team: Team, asset_id: int) -> bool:
    """Takes a player off his group's ranking (he was dropped) and closes the gap so the players behind him move up one.
    Does nothing for a non-NHL team or an unranked player. Flushes, does not commit."""
    try:
        assert_hockey_team(db, team)
    except ValueError:
        return False
    row = db.query(TeamPeckingOrder).filter_by(team_id=team.id, draftable_asset_id=asset_id).first()
    if row is None:
        return False
    group, priority = row.position_group, row.priority
    db.delete(row)
    db.flush()
    for later in (db.query(TeamPeckingOrder).filter(TeamPeckingOrder.team_id == team.id,
                                                    TeamPeckingOrder.position_group == group,
                                                    TeamPeckingOrder.priority > priority)
                  .order_by(TeamPeckingOrder.priority)):
        later.priority -= 1
        db.flush()
    return True
