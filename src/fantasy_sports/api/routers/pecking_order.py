"""
NHL pecking order: read/reorder a team's ranked depth chart and resolve a day's starters from
activity data passed in by the caller (see platform/pecking_order.py for the rules).
"""

from __future__ import annotations

import datetime
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
from fantasy_sports.platform.db.models.core import League, RealTeam, Team, Universe, User
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


def _player_out(
    roster: dict,
    pid: int,
    priority: int | None,
    rates: dict | None = None,
    team_map: dict[int, RealTeam] | None = None,
) -> PeckingOrderPlayerOut:
    asset_id, player = roster[pid]
    rate = (rates or {}).get(asset_id, {})
    team = (team_map or {}).get(player.real_team_id) if player.real_team_id else None
    real_team = team.name if team else None
    real_team_code = team.external_ref if team and team.external_ref else (real_team[:3].upper() if real_team else None)
    return PeckingOrderPlayerOut(
        player_id=pid,
        draftable_asset_id=asset_id,
        display_name=player.full_name or f"Player {pid}",
        position=player.position,
        priority=priority,
        games=rate.get("games"),
        points_per_game=rate.get("points_per_game"),
        real_team=real_team,
        real_team_code=real_team_code,
    )


def series_window_of(d: datetime.date, series_start: datetime.date | None = None) -> int:
    """Returns 1, 2, or 3 corresponding to Game 1, 2, or 3 of the 7-day series.
    Sun-Tue is always window 1, Wed-Thur is always window 2, Fri-Sat is always window 3.
    """
    w = d.weekday()  # Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    if w in (6, 0, 1):
        return 1
    elif w in (2, 3):
        return 2
    else:
        return 3


def _read(db: Session, team: Team) -> PeckingOrderOut:
    from fantasy_sports.platform.db.models.core import Game, LeagueSeries
    from fantasy_sports.sports.hockey.pipeline import week_start, window_of

    roster = po.roster_players(db, team.id)
    order = po.load_order(db, team.id)
    rates = rates_by_asset(db, [asset_id for asset_id, _ in roster.values()])
    team_map = {t.id: t for t in db.query(RealTeam)}
    groups = []
    for g in po.GROUPS:
        valid_order = order[g][:po.DEPTH[g]]
        ranked = [_player_out(roster, pid, i, rates, team_map) for i, pid in enumerate(valid_order, start=1) if pid in roster]
        taken = {p.player_id for p in ranked}
        unranked = sorted((_player_out(roster, pid, None, rates, team_map) for pid, (_, pl) in roster.items()
                           if (po.GROUP_BY_POSITION.get(pl.position) == g or po.is_eligible_for_group(pl.position, g)) and pid not in taken),
                          key=lambda p: (p.display_name, p.player_id))
        groups.append(PeckingOrderGroupOut(position_group=g, max_depth=po.DEPTH[g], starters=po.STARTERS[g],
                                           ranked=ranked, unranked=unranked))

    # Real team schedule mapping for consistency across all roster views
    real_schedules: dict[str, dict[int, list[dict]]] = {}
    nhl = db.query(Universe).filter_by(code="nhl").first()
    first_series = db.query(LeagueSeries).filter_by(league_id=team.league_id).order_by(LeagueSeries.start_date).first()
    from fantasy_sports.platform.nhl_daily_sync import current_eastern_date, to_eastern_naive
    w_start = week_start(first_series.start_date) if first_series else week_start(current_eastern_date())
    w_begin = datetime.datetime.combine(w_start, datetime.time.min)
    w_end = w_begin + datetime.timedelta(days=7)

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    games = db.query(Game).filter(
        Game.universe_id == (nhl.id if nhl else 2),
        Game.kickoff_at >= w_begin - datetime.timedelta(hours=6),
        Game.kickoff_at < w_end + datetime.timedelta(hours=6)
    ).order_by(Game.kickoff_at).all()

    seen_matchups: set[tuple[str, int, str, str]] = set()
    for g in games:
        if not g.kickoff_at:
            continue
        et_dt = to_eastern_naive(g.kickoff_at) if g.kickoff_at.hour < 5 else g.kickoff_at
        g_date = et_dt.date()
        if g_date < w_start or g_date >= w_start + datetime.timedelta(days=7):
            continue
        win_idx = series_window_of(g_date, w_start)
        day_str = day_names[g_date.weekday()]
        home_t = team_map.get(g.home_team_id)
        away_t = team_map.get(g.away_team_id)
        if not home_t or not away_t:
            continue
        home_code = home_t.external_ref or home_t.name[:3].upper()
        away_code = away_t.external_ref or away_t.name[:3].upper()

        k_home = (home_code, win_idx, day_str, away_code)
        if k_home not in seen_matchups:
            seen_matchups.add(k_home)
            real_schedules.setdefault(home_code, {}).setdefault(win_idx, []).append({
                "game_id": g.id,
                "day": day_str,
                "opp": away_code,
                "is_home": True
            })

        k_away = (away_code, win_idx, day_str, home_code)
        if k_away not in seen_matchups:
            seen_matchups.add(k_away)
            real_schedules.setdefault(away_code, {}).setdefault(win_idx, []).append({
                "game_id": g.id,
                "day": day_str,
                "opp": home_code,
                "is_home": False
            })

    return PeckingOrderOut(team_id=team.id, groups=groups, real_schedules=real_schedules)


@router.get("/{team_id}/pecking-order", response_model=PeckingOrderOut)
def get_pecking_order(team_id: int, db: Session = Depends(get_db)):
    team = _team(db, team_id)
    try:
        from fantasy_sports.platform import auto_schedule
        league = db.get(League, team.league_id)
        if league:
            auto_schedule.ensure_schedule(db, league)
    except Exception:
        pass
    try:
        from fantasy_sports.platform.nhl_daily_sync import auto_sync_league_scoreboard
        auto_sync_league_scoreboard(db, team.league_id)
    except Exception:
        pass
    try:
        ai_roster.tick(db, team.league_id)      # AI teams set their own lines (platform/ai/ai_roster.py)
        db.commit()
    except Exception:
        db.rollback()
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
    team_map = {t.id: t for t in db.query(RealTeam)}
    return PeckingOrderResolvedOut(
        team_id=team.id,
        starters={g: [_player_out(roster, pid, order[g].index(pid) + 1, team_map=team_map) for pid in ids]
                  for g, ids in result.starters.items()},
        vacancies=[PeckingOrderVacancyOut(position_group=v.position_group, slot=v.slot, reason=v.reason)
                   for v in result.vacancies],
    )
