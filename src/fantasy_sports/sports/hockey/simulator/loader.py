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
from typing import NamedTuple

from sqlalchemy.orm import Session

from fantasy_sports.platform import pecking_order as po
from fantasy_sports.platform.db.models.core import Game, LeagueUniverse, RealTeam, SeriesDailyGame, Team, Universe
from fantasy_sports.platform.simulator import service
from fantasy_sports.sports.hockey import pipeline
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, NhlStandingsSnapshot, Shot, ShotCredit
from fantasy_sports.sports.hockey.simulator.adapter import HockeySimulatorAdapter
from fantasy_sports.sports.hockey.simulator.model import (
    DEFAULT_WINDOW_SECONDS, EPS, ROLES, CountingGame, CreditLine, HockeyGameInputs, InputError, RealGame, ShotAttempt,
    SideInput, WindowContext,
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


def _credits_for(db: Session, assets: dict, game_ids: list[int], tag_games: bool = False):
    """(credit lines, shot attempts) the stored credits give `assets` (asset id -> Player) in the real games. With
    tag_games each line and attempt carries its real game id (window scoring); otherwise it does not, so a plain
    game's inputs and digest are exactly what they were before windows."""
    lines: list[CreditLine] = []
    attempts: list[ShotAttempt] = []
    if not (assets and game_ids):
        return lines, attempts
    rows = (db.query(ShotCredit, Shot).join(Shot, Shot.id == ShotCredit.shot_id)
            .filter(Shot.game_id.in_(game_ids), ShotCredit.draftable_asset_id.in_(list(assets)))
            .order_by(ShotCredit.id).all())
    for credit, shot in rows:
        player = assets[credit.draftable_asset_id]
        lines.append(CreditLine(
            line_id=f"sc:{credit.id}", group_id=f"shot:{shot.id}", asset_id=credit.draftable_asset_id,
            player_name=player.full_name, position=po.GROUP_BY_POSITION.get(player.position, player.position),
            role=credit.role, points=credit.credit_points, real_second=_second(shot.clock), xg=shot.pre_shot_xg,
            is_goal=bool(shot.is_goal), empty_net=bool(shot.is_empty_net),
            game_id=shot.game_id if tag_games else None))
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
            role=credit.role, points=credit.credit_points, real_second=_second(event.game_seconds),
            game_id=event.game_id if tag_games else None))
    for shot in (db.query(Shot).filter(Shot.game_id.in_(game_ids), Shot.shooter_asset_id.in_(list(assets)),
                                       Shot.is_goal.is_(False), Shot.is_empty_net.is_(False)).order_by(Shot.id)):
        attempts.append(ShotAttempt(
            attempt_id=f"shot:{shot.id}", asset_id=shot.shooter_asset_id,
            player_name=assets[shot.shooter_asset_id].full_name, real_second=_second(shot.clock),
            xg=shot.pre_shot_xg or 0.0, game_id=shot.game_id if tag_games else None))
    return lines, attempts


def _value_lines_for(db: Session, assets: dict, game_ids: list[int], tag_games: bool = False):
    """(credit lines, no attempts) of the VALUE model for `assets` (asset id -> Player) in the real games, built straight from the
    stored shots and play-by-play. Every action creates value:

      * a shot on goal (saved or scored) is worth its xG: the shooter keeps the part the assists do not take; a goal's assists share
        ASSIST_SHARE of the xG (primary 2/3, secondary 1/3; one assist takes it all). Who assisted is read from the stored goal credit.
      * a block is worth the xG it prevented (the stored block credit); a hit HIT_VALUE; a takeaway TAKEAWAY_VALUE.
      * a goalie's save is worth the xG of the shot he stopped (xGSA); a goal against costs him nothing here: the shot's value already
        counts for the shooter, and a save is what takes that value away (attribution.build_value_ledger).

    Every shot is a line group, so the shots themselves are the shot count and no separate attempts are returned."""
    from collections import defaultdict
    from fantasy_sports.sports.hockey.simulator.model import ASSIST_SHARE, HIT_VALUE, TAKEAWAY_VALUE

    lines: list[CreditLine] = []
    if not (assets and game_ids):
        return lines, []
    ids = set(assets)

    def line(line_id, group_id, asset_id, role, points, second, shot=None, game_id=None):
        player = assets[asset_id]
        lines.append(CreditLine(
            line_id=line_id, group_id=group_id, asset_id=asset_id, player_name=player.full_name,
            position=po.GROUP_BY_POSITION.get(player.position, player.position), role=role, points=points,
            real_second=second, xg=None if shot is None else shot.pre_shot_xg, is_goal=bool(shot is not None and shot.is_goal),
            empty_net=bool(shot is not None and shot.is_empty_net), game_id=game_id if tag_games else None))

    assists: dict[int, dict[str, int]] = defaultdict(dict)
    for credit in (db.query(ShotCredit).join(Shot, Shot.id == ShotCredit.shot_id)
                   .filter(Shot.game_id.in_(game_ids), ShotCredit.role.in_(("primary_assist", "secondary_assist")))):
        assists[credit.shot_id][credit.role] = credit.draftable_asset_id
    for shot in db.query(Shot).filter(Shot.game_id.in_(game_ids)).order_by(Shot.id):
        xg = max(shot.pre_shot_xg or 0.0, 0.0)
        second, group = _second(shot.clock), f"shot:{shot.id}"
        given = [(role, assists[shot.id][role]) for role in ("primary_assist", "secondary_assist") if role in assists.get(shot.id, {})]
        shares = {}
        if given:
            pool = xg * ASSIST_SHARE
            parts = (pool,) if len(given) == 1 else (pool * 2 / 3, pool / 3)
            shares = {role: part for (role, _), part in zip(given, parts)}
        shooter_value = xg - sum(shares.values())
        if shot.shooter_asset_id in ids and shooter_value > EPS:
            line(f"v:{shot.id}:shooter", group, shot.shooter_asset_id, "shooter", shooter_value, second, shot, shot.game_id)
        for role, asset_id in given:
            if asset_id in ids and shares[role] > EPS:
                line(f"v:{shot.id}:{role}", group, asset_id, role, shares[role], second, shot, shot.game_id)
        if shot.goalie_asset_id in ids and not shot.is_goal and not shot.is_empty_net and xg > EPS:
            line(f"v:{shot.id}:goalie", group, shot.goalie_asset_id, "goalie", xg, second, shot, shot.game_id)
    for credit, event in (db.query(EventCredit, GameEvent).join(GameEvent, GameEvent.id == EventCredit.event_id)
                          .filter(GameEvent.game_id.in_(game_ids), EventCredit.role == "blocker",
                                  EventCredit.draftable_asset_id.in_(list(ids))).order_by(EventCredit.id)):
        if credit.credit_points > EPS:
            line(f"v:ec:{credit.id}", f"event:{event.id}", credit.draftable_asset_id, "blocker", credit.credit_points,
                 _second(event.game_seconds), None, event.game_id)
    for event in (db.query(GameEvent).filter(GameEvent.game_id.in_(game_ids), GameEvent.event_type.in_(("hit", "takeaway")),
                                             GameEvent.actor_asset_id.in_(list(ids))).order_by(GameEvent.id)):
        role, value = ("hitter", HIT_VALUE) if event.event_type == "hit" else ("takeaway", TAKEAWAY_VALUE)
        if value > EPS:
            line(f"v:ev:{event.id}", f"event:{event.id}", event.actor_asset_id, role, value, _second(event.game_seconds), None, event.game_id)
    return lines, []


def _side(db: Session, team: Team, game_ids: list[int]) -> tuple[SideInput, dict[int, int | None]]:
    """(the side's inputs, starter asset id -> his real team id)."""
    roster = po.roster_players(db, team.id)
    starters = resolve_day_starters(db, team, game_ids)
    assets = {roster[pid][0]: roster[pid][1] for ids in starters.values() for pid in ids}
    lines, attempts = _credits_for(db, assets, game_ids)
    side = SideInput(team_id=team.id, name=team.name, lines=tuple(lines), attempts=tuple(attempts),
                     starters=tuple(sorted((a, p.full_name) for a, p in assets.items())))
    return side, {a: p.real_team_id for a, p in assets.items()}


def _team_labels(db: Session, team_ids) -> dict[int, str]:
    """Real team id -> short label (the NHL abbreviation when stored, else the name)."""
    return {t.id: t.external_ref or t.name for t in db.query(RealTeam).filter(RealTeam.id.in_(sorted(set(team_ids)) or [0]))}


def _real_game(game: Game, labels: dict[int, str]) -> RealGame:
    if game.kickoff_at is None:
        raise InputError(f"real game {game.id} has no kickoff time; the chronological division needs it")
    return RealGame(game.id, game.kickoff_at, game.home_team_id, game.away_team_id,
                    labels.get(game.home_team_id, ""), labels.get(game.away_team_id, ""))


def _counting_games(key: str, side: SideInput, asset_team: dict[int, int | None], by_team: dict[int, RealGame]):
    """One counting game per starter whose real team plays among `by_team` (a starter with no game is an empty slot)."""
    out = []
    for asset_id, name in side.starters:
        game = by_team.get(asset_team.get(asset_id))
        if game is None:
            continue
        at_home = asset_team[asset_id] == game.home_key
        out.append(CountingGame(asset_id, name, key, game.game_id, game.kickoff_at,
                                f"vs {game.away_label}" if at_home else f"@ {game.home_label}"))
    return out


def build_inputs(db: Session, daily_game: SeriesDailyGame) -> HockeyGameInputs:
    league_id = daily_game.series.league_id
    game_ids = real_game_ids(db, league_id, daily_game.calendar_date)
    home = db.get(Team, daily_game.home_team_id)
    away = db.get(Team, daily_game.away_team_id)
    home_side, home_teams = _side(db, home, game_ids)
    away_side, away_teams = _side(db, away, game_ids)
    rows = db.query(Game).filter(Game.id.in_(game_ids or [0])).order_by(Game.id).all()
    labels = _team_labels(db, [t for g in rows for t in (g.home_team_id, g.away_team_id)])
    by_team: dict[int, RealGame] = {}
    for g in rows:                                        # one game per real team per calendar day
        real = _real_game(g, labels)
        by_team.setdefault(g.home_team_id, real)
        by_team.setdefault(g.away_team_id, real)
    counting = (_counting_games("home", home_side, home_teams, by_team)
                + _counting_games("away", away_side, away_teams, by_team))
    return HockeyGameInputs(daily_game_id=daily_game.id, calendar_date=daily_game.calendar_date.isoformat(),
                            home=home_side, away=away_side, counting_games=tuple(counting))


def publish_game(db: Session, daily_game: SeriesDailyGame, seed: int | None = None,
                 window_seconds: int = DEFAULT_WINDOW_SECONDS):
    """Build and publish the game's script. The default seed is the daily game id, so a game always replays
    the same way. Flushes, does not commit. Raises InputError when neither side has any credit."""
    inputs = build_inputs(db, daily_game)
    if not any(s.lines or s.attempts for s in (inputs.home, inputs.away)):
        raise InputError("Neither lineup has any stored credit for this date; nothing to simulate")
    script = HockeySimulatorAdapter(window_seconds).build_script(inputs, daily_game.id if seed is None else seed)
    return service.publish_script(db, daily_game, script)


# ---- fantasy windows (docs/hockey_spec.md section 5) -------------------------------------------------------------
def week_games(db: Session, league_id: int, start: datetime.date) -> dict[int, datetime.date]:
    """Real NHL game id -> calendar date for every stored game kicking off in the fantasy week starting `start`."""
    nhl = db.query(Universe).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id).filter(
        LeagueUniverse.league_id == league_id, Universe.code == "nhl").first()
    if nhl is None:
        raise InputError("Not an NHL league")
    begin = datetime.datetime.combine(start, datetime.time.min)
    rows = db.query(Game.id, Game.kickoff_at).filter(
        Game.universe_id == nhl.id, Game.kickoff_at >= begin, Game.kickoff_at < begin + datetime.timedelta(days=7)
    ).order_by(Game.id)
    return {gid: kickoff.date() for gid, kickoff in rows}


def _window_side(db: Session, team: Team, dates: dict[int, datetime.date], start: datetime.date, window_index: int,
                 moved_assets: set[int]) -> tuple[SideInput, dict[int, int | None]]:
    """A team's starters for one window and ALL of the week's credit for them, every line tagged with its game."""
    in_window = {gid for gid, d in dates.items() if pipeline.window_of(d) == (start, window_index)}
    roster = po.roster_players(db, team.id)
    real_teams = set()
    for g in db.query(Game).filter(Game.id.in_(in_window or [0])):
        real_teams.update((g.home_team_id, g.away_team_id))
    # A player is active in the window if his real team plays in it, or his manager moved a surplus game into it.
    scheduled = {pid for pid, (aid, pl) in roster.items() if pl.real_team_id in real_teams or aid in moved_assets}
    week_ids = sorted(dates)
    goalie_assets = {r[0] for r in db.query(Shot.goalie_asset_id).filter(
        Shot.game_id.in_(week_ids or [0]), Shot.goalie_asset_id.isnot(None)).distinct()}
    confirmed = {pid for pid, (aid, _) in roster.items() if aid in goalie_assets}
    order = {g: [pid for pid in ids if pid in roster] for g, ids in po.load_order(db, team.id).items()}
    starters = po.resolve_starters(order, frozenset(scheduled), frozenset(confirmed)).starters
    assets = {roster[pid][0]: roster[pid][1] for ids in starters.values() for pid in ids}
    lines, attempts = _credits_for(db, assets, week_ids, tag_games=True)
    side = SideInput(team_id=team.id, name=team.name, lines=tuple(lines), attempts=tuple(attempts),
                     starters=tuple(sorted((a, p.full_name) for a, p in assets.items())))
    return side, {a: p.real_team_id for a, p in assets.items()}


class WindowLoad(NamedTuple):
    """What build_window_inputs hands to HockeySimulatorAdapter.build_window_script."""
    inputs: HockeyGameInputs
    game_dates: dict[int, datetime.date]            # real game id -> date, for the whole week
    scheduled_games: dict[int, tuple[int, ...]]     # asset id -> every real game his team plays that week
    context: WindowContext                          # kickoffs, opponents and the standings snapshots


def _window_context(db: Session, home: Team, away: Team, dates: dict[int, datetime.date],
                    asset_team: dict[int, int | None]) -> WindowContext:
    """Kickoffs and opponents for the week's real games, every roster player's real team, and the stored standings
    snapshots (Best Matchup reads the snapshot dated the day a window locks; a missing one is an error, never a guess)."""
    rows = db.query(Game).filter(Game.id.in_(sorted(dates) or [0])).order_by(Game.id).all()
    labels = _team_labels(db, [t for g in rows for t in (g.home_team_id, g.away_team_id)])
    games = {g.id: _real_game(g, labels) for g in rows}
    snaps: dict[tuple[int, datetime.date], int] = {}
    if rows:
        for snap in db.query(NhlStandingsSnapshot).filter(
                NhlStandingsSnapshot.universe_id == rows[0].universe_id,
                NhlStandingsSnapshot.as_of_date.in_(sorted(set(dates.values())))):
            snaps[(snap.real_team_id, snap.as_of_date)] = snap.points
    names: dict[int, str] = {}
    for team in (home, away):                    # the resolver considers the whole roster, not just the starters
        for _, (asset_id, player) in po.roster_players(db, team.id).items():
            asset_team.setdefault(asset_id, player.real_team_id)
            names[asset_id] = player.full_name
    return WindowContext(games, asset_team, lambda team_key, lock_at: snaps.get((team_key, lock_at.date())), names)


def _scheduled_games(db: Session, teams: list[Team], dates: dict[int, datetime.date]) -> dict[int, tuple[int, ...]]:
    by_real_team: dict[int, list[int]] = {}
    for g in db.query(Game).filter(Game.id.in_(sorted(dates) or [0])).order_by(Game.id):
        for real_team in (g.home_team_id, g.away_team_id):
            by_real_team.setdefault(real_team, []).append(g.id)
    out: dict[int, tuple[int, ...]] = {}
    for team in teams:
        for pid, (asset_id, player) in po.roster_players(db, team.id).items():
            out[asset_id] = tuple(by_real_team.get(player.real_team_id, ()))
    return out


def build_window_inputs(db: Session, daily_game_id: int, home: Team, away: Team, week_start: datetime.date,
                        window_index: int, assignments=()) -> WindowLoad:
    """Inputs for one fantasy window plus what the adapter needs to resolve them. The inputs carry all the week's
    credit for each side's starters; HockeySimulatorAdapter.build_window_script picks each player's one counting game.
    `assignments` are pipeline.WindowAssignment (the manager's surplus-game moves); nothing is stored here."""
    dates = week_games(db, home.league_id, week_start)
    moved = {a.asset_id for a in assignments if a.target_window == window_index}
    window_day = pipeline.window_dates(week_start, window_index)[0]
    home_side, home_teams = _window_side(db, home, dates, week_start, window_index, moved)
    away_side, away_teams = _window_side(db, away, dates, week_start, window_index, moved)
    inputs = HockeyGameInputs(
        daily_game_id=daily_game_id, calendar_date=window_day.isoformat(), home=home_side, away=away_side,
        window_index=window_index, week_start=week_start.isoformat())
    context = _window_context(db, home, away, dates, {**home_teams, **away_teams})
    return WindowLoad(inputs, dates, _scheduled_games(db, [home, away], dates), context)
