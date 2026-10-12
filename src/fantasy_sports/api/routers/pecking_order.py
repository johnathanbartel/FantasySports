"""
NHL pecking order: read/reorder a team's ranked depth chart and resolve a day's starters from
activity data passed in by the caller (see platform/pecking_order.py for the rules).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_can_manage_team, current_user
from fantasy_sports.api.schemas import (
    PeckingOrderGroupOut, PeckingOrderOut, PeckingOrderPlayerOut, PeckingOrderResolveRequest,
    PeckingOrderResolvedOut, PeckingOrderSetRequest, PeckingOrderVacancyOut,
)
from fantasy_sports.platform import pecking_order as po
from fantasy_sports.platform.ai import ai_roster
from fantasy_sports.platform.player_rates import rates_by_asset
from fantasy_sports.platform.db.models.core import Team, User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(prefix="/teams", tags=["pecking-order"])


def _team(db: Session, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    try:
        po.assert_hockey_team(db, team)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return team


def _player_out(roster: dict, pid: int, priority: int | None, rates: dict | None = None) -> PeckingOrderPlayerOut:
    asset_id, player = roster[pid]
    rate = (rates or {}).get(asset_id, {})
    return PeckingOrderPlayerOut(player_id=pid, draftable_asset_id=asset_id, display_name=player.full_name,
                                 position=player.position, priority=priority, games=rate.get("games"),
                                 points_per_game=rate.get("points_per_game"))


def _read(db: Session, team: Team) -> PeckingOrderOut:
    roster = po.roster_players(db, team.id)
    order = po.load_order(db, team.id)
    rates = rates_by_asset(db, [asset_id for asset_id, _ in roster.values()])
    groups = []
    for g in po.GROUPS:
        ranked = [_player_out(roster, pid, i, rates) for i, pid in enumerate(order[g], start=1) if pid in roster]
        taken = {p.player_id for p in ranked}
        unranked = sorted((_player_out(roster, pid, None, rates) for pid, (_, pl) in roster.items()
                           if po.GROUP_BY_POSITION.get(pl.position) == g and pid not in taken),
                          key=lambda p: (p.display_name, p.player_id))
        groups.append(PeckingOrderGroupOut(position_group=g, max_depth=po.DEPTH[g], starters=po.STARTERS[g],
                                           ranked=ranked, unranked=unranked))
    return PeckingOrderOut(team_id=team.id, groups=groups)


@router.get("/{team_id}/pecking-order", response_model=PeckingOrderOut)
def get_pecking_order(team_id: int, db: Session = Depends(get_db)):
    team = _team(db, team_id)
    ai_roster.tick(db, team.league_id)      # AI teams set their own lines (platform/ai/ai_roster.py)
    db.commit()
    return _read(db, team)


@router.put("/{team_id}/pecking-order", response_model=PeckingOrderOut)
def set_pecking_order(team_id: int, payload: PeckingOrderSetRequest, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Replaces the ranking of each group named (all-or-nothing). Same authority as setting a lineup:
    the team's owner, or the commissioner (with commissioner mode) for an AI-owned team."""
    team = assert_can_manage_team(db, user, team_id)
    try:
        po.replace_groups(db, team, payload.assignments)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    return _read(db, team)


@router.post("/{team_id}/pecking-order/resolve", response_model=PeckingOrderResolvedOut)
def resolve_pecking_order(team_id: int, payload: PeckingOrderResolveRequest, db: Session = Depends(get_db)):
    """Read-only. Resolves the day's starters from the supplied scheduled-player and confirmed-goalie
    ids; nothing is fetched or stored."""
    team = _team(db, team_id)
    roster = po.roster_players(db, team.id)
    order = {g: [pid for pid in ids if pid in roster] for g, ids in po.load_order(db, team.id).items()}
    result = po.resolve_starters(order, frozenset(payload.scheduled_player_ids),
                                 frozenset(payload.confirmed_goalie_ids))
    return PeckingOrderResolvedOut(
        team_id=team.id,
        starters={g: [_player_out(roster, pid, order[g].index(pid) + 1) for pid in ids]
                  for g, ids in result.starters.items()},
        vacancies=[PeckingOrderVacancyOut(position_group=v.position_group, slot=v.slot, reason=v.reason)
                   for v in result.vacancies],
    )
