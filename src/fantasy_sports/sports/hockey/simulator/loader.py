"""
Database side of the hockey adapter: resolve each team's starters for a daily game's calendar date and load the
credits the pipeline already stored (shot_credits / event_credits). Nothing is recomputed here and nothing is
fetched from outside; run sports/hockey/scripts/run_game_validation.py --write first to store credits.

Lineup for a date: the team's pecking order resolved with platform/pecking_order.resolve_starters, where a
player is ACTIVE if his real team has a stored game kicking off on that calendar date, and a goalie also needs
to have faced a shot in one of those games (the only evidence of a start the database holds). Calendar date is
the stored (naive) kickoff_at date.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import pecking_order as po
from fantasy_sports.platform.db.models.core import Game, LeagueUniverse, SeriesDailyGame, Team, Universe
from fantasy_sports.platform.simulator import service
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit
from fantasy_sports.sports.hockey.simulator.adapter import HockeySimulatorAdapter
from fantasy_sports.sports.hockey.simulator.model import (
    DEFAULT_WINDOW_SECONDS, EPS, ROLES, CreditLine, HockeyGameInputs, InputError, ShotAttempt, SideInput,
)


def _second(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def real_game_ids(db: Session, league_id: int, date: datetime.date) -> list[int]:
    nhl = db.query(Universe).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id).filter(
        LeagueUniverse.league_id == league_id, Universe.code == "nhl").first()
    if nhl is None:
        raise InputError("Not an NHL league")
    start = datetime.datetime.combine(date, datetime.time.min)
    rows = db.query(Game.id).filter(Game.universe_id == nhl.id, Game.kickoff_at >= start,
                                    Game.kickoff_at < start + datetime.timedelta(days=1)).order_by(Game.id)
    return [r[0] for r in rows]


def resolve_day_starters(db: Session, team: Team, game_ids: list[int]) -> dict[str, list[int]]:
    """group -> starting player ids for the real games in game_ids."""
    roster = po.roster_players(db, team.id)
    real_teams = set()
    for g in db.query(Game).filter(Game.id.in_(game_ids)) if game_ids else []:
        real_teams.update((g.home_team_id, g.away_team_id))
    scheduled = {pid for pid, (_, pl) in roster.items() if pl.real_team_id in real_teams}
    goalie_assets = {r[0] for r in db.query(Shot.goalie_asset_id).filter(
        Shot.game_id.in_(game_ids), Shot.goalie_asset_id.isnot(None)).distinct()} if game_ids else set()
    confirmed = {pid for pid, (aid, _) in roster.items() if aid in goalie_assets}
    order = {g: [pid for pid in ids if pid in roster] for g, ids in po.load_order(db, team.id).items()}
    return po.resolve_starters(order, frozenset(scheduled), frozenset(confirmed)).starters


def _side(db: Session, team: Team, game_ids: list[int]) -> SideInput:
    roster = po.roster_players(db, team.id)
    starters = resolve_day_starters(db, team, game_ids)
    assets = {roster[pid][0]: roster[pid][1] for ids in starters.values() for pid in ids}
    lines: list[CreditLine] = []
    attempts: list[ShotAttempt] = []
    if assets and game_ids:
        rows = (db.query(ShotCredit, Shot).join(Shot, Shot.id == ShotCredit.shot_id)
                .filter(Shot.game_id.in_(game_ids), ShotCredit.draftable_asset_id.in_(list(assets)))
                .order_by(ShotCredit.id).all())
        for credit, shot in rows:
            player = assets[credit.draftable_asset_id]
            lines.append(CreditLine(
                line_id=f"sc:{credit.id}", group_id=f"shot:{shot.id}", asset_id=credit.draftable_asset_id,
                player_name=player.full_name, position=po.GROUP_BY_POSITION.get(player.position, player.position),
                role=credit.role, points=credit.credit_points, real_second=_second(shot.clock), xg=shot.pre_shot_xg,
                is_goal=bool(shot.is_goal), empty_net=bool(shot.is_empty_net)))
        rows = (db.query(EventCredit, GameEvent).join(GameEvent, GameEvent.id == EventCredit.event_id)
                .filter(GameEvent.game_id.in_(game_ids), EventCredit.draftable_asset_id.in_(list(assets)))
                .order_by(EventCredit.id).all())
        for credit, event in rows:
            if credit.role not in ROLES:
                if abs(credit.credit_points) > EPS:
                    raise InputError(f"event credit {credit.id}: role {credit.role!r} carries points but the "
                                     "hockey simulator adapter does not support it")
                continue
            player = assets[credit.draftable_asset_id]
            lines.append(CreditLine(
                line_id=f"ec:{credit.id}", group_id=f"event:{event.id}", asset_id=credit.draftable_asset_id,
                player_name=player.full_name, position=po.GROUP_BY_POSITION.get(player.position, player.position),
                role=credit.role, points=credit.credit_points, real_second=_second(event.game_seconds)))
        for shot in (db.query(Shot).filter(Shot.game_id.in_(game_ids), Shot.shooter_asset_id.in_(list(assets)),
                                           Shot.is_goal.is_(False), Shot.is_empty_net.is_(False)).order_by(Shot.id)):
            attempts.append(ShotAttempt(
                attempt_id=f"shot:{shot.id}", asset_id=shot.shooter_asset_id,
                player_name=assets[shot.shooter_asset_id].full_name, real_second=_second(shot.clock),
                xg=shot.pre_shot_xg or 0.0))
    return SideInput(team_id=team.id, name=team.name, lines=tuple(lines), attempts=tuple(attempts))


def build_inputs(db: Session, daily_game: SeriesDailyGame) -> HockeyGameInputs:
    league_id = daily_game.series.league_id
    game_ids = real_game_ids(db, league_id, daily_game.calendar_date)
    home = db.get(Team, daily_game.home_team_id)
    away = db.get(Team, daily_game.away_team_id)
    return HockeyGameInputs(daily_game_id=daily_game.id, calendar_date=daily_game.calendar_date.isoformat(),
                            home=_side(db, home, game_ids), away=_side(db, away, game_ids))


def publish_game(db: Session, daily_game: SeriesDailyGame, seed: int | None = None,
                 window_seconds: int = DEFAULT_WINDOW_SECONDS):
    """Build and publish the game's script. The default seed is the daily game id, so a game always replays
    the same way. Flushes, does not commit. Raises InputError when neither side has any credit."""
    inputs = build_inputs(db, daily_game)
    if not any(s.lines or s.attempts for s in (inputs.home, inputs.away)):
        raise InputError("Neither lineup has any stored credit for this date; nothing to simulate")
    script = HockeySimulatorAdapter(window_seconds).build_script(inputs, daily_game.id if seed is None else seed)
    return service.publish_script(db, daily_game, script)
