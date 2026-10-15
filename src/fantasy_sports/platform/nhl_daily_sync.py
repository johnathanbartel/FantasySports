"""Automatic NHL daily slate synchronization for the platform.

Ensures that whenever a user views the Scoreboard or game schedules:
1. If the league's schedule was started on a non-Sunday date and no games have started, auto-aligns it to Sunday.
2. If real NHL games for today have not been ingested into the database, automatically fetches
   the day's schedule and play-by-play from the public NHL API (api-web.nhle.com), calculates xG attributions,
   and stores them.
3. Publishes simulations for today's daily fantasy matchups, starts scheduled games, and finalizes them if
   the real NHL slate has concluded.

All external HTTP calls use Python's built-in urllib with strict timeouts and safe fallback.
"""

from __future__ import annotations

import datetime
import json
import math
import urllib.request
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session

from fantasy_sports.platform import pecking_order as po, series_schedule
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, Game, League, LeagueSeries, LeagueUniverse, Player, RealTeam, SeriesDailyGame, Universe, Week,
)
from fantasy_sports.platform.simulator import service
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, NhlStandingsSnapshot, Shot, ShotCredit
from fantasy_sports.sports.hockey.pipeline import (
    BlockXgEstimator, EventRecord, ShotRecord, process_game, week_start,
    window_of, window_dates,
)
from fantasy_sports.sports.hockey.simulator import loader

SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{date}"
PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game}/play-by-play"
ROSTER_URL = "https://api-web.nhle.com/v1/roster/{team}/current"
STANDINGS_URL = "https://api-web.nhle.com/v1/standings/{date}"
HEADERS = {"User-Agent": "FantasySportsPlatform-NHLSync/1.0"}
POSITION_MAP = {"L": "LW", "R": "RW", "C": "C", "D": "D", "G": "G"}
EVENT_TYPES = {
    "goal", "shot-on-goal", "missed-shot", "blocked-shot",
    "hit", "giveaway", "takeaway", "faceoff",
}

EMPIRICAL_XG_BANDS = {
    0: 0.275, 1: 0.128, 2: 0.115, 3: 0.108, 4: 0.099,
    5: 0.083, 6: 0.068, 7: 0.050, 8: 0.035, 9: 0.027,
    10: 0.023, 11: 0.022, 12: 0.016, 13: 0.018, 14: 0.030,
}


def _fetch_json(url: str, timeout: float = 6.0) -> dict | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _calculate_xg(x: float | None, y: float | None, is_goal: bool, is_empty_net: bool) -> float:
    if is_empty_net:
        return 0.95
    if x is None or y is None:
        return 0.10 if is_goal else 0.06
    dist = math.hypot(89 - abs(x), y)
    band = min(int(dist // 5.0), 14)
    return EMPIRICAL_XG_BANDS.get(band, 0.05)


def to_eastern_naive(utc_dt: datetime.datetime) -> datetime.datetime:
    """Converts a UTC datetime to North American Eastern time (ET/EDT/EST) as a naive datetime.
    Standard library ZoneInfo with fallback to manual DST rules."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=datetime.timezone.utc)
    try:
        return utc_dt.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        year = utc_dt.year
        dst_start = datetime.datetime(year, 3, 8) + datetime.timedelta(days=(6 - datetime.datetime(year, 3, 8).weekday()) % 7)
        dst_end = datetime.datetime(year, 11, 1) + datetime.timedelta(days=(6 - datetime.datetime(year, 11, 1).weekday()) % 7)
        utc_naive = utc_dt.replace(tzinfo=None)
        is_dst = dst_start <= utc_naive < dst_end
        offset = datetime.timedelta(hours=-4 if is_dst else -5)
        return utc_naive + offset


def eastern_naive_to_utc(dt: datetime.datetime) -> datetime.datetime:
    """Converts an Eastern naive datetime to UTC naive datetime."""
    try:
        aware_et = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        aware_utc = aware_et.astimezone(datetime.timezone.utc)
        return aware_utc.replace(tzinfo=None)
    except Exception:
        year = dt.year
        dst_start = datetime.datetime(year, 3, 8) + datetime.timedelta(days=(6 - datetime.datetime(year, 3, 8).weekday()) % 7)
        dst_end = datetime.datetime(year, 11, 1) + datetime.timedelta(days=(6 - datetime.datetime(year, 11, 1).weekday()) % 7)
        is_dst = dst_start <= dt < dst_end
        offset = datetime.timedelta(hours=4 if is_dst else 5)
        return dt + offset


def current_eastern_date() -> datetime.date:
    """Returns today's calendar date in North American Eastern Time.
    Ensures date queries match authentic NHL game dates rather than UTC server midnight rollovers.
    """
    try:
        return to_eastern_naive(datetime.datetime.now(datetime.timezone.utc)).date()
    except Exception:
        return datetime.date.today()


def is_real_game_started(game: Game, now_utc: datetime.datetime | None = None) -> bool:
    """Returns True if the real NHL game has actually started (puck dropped) in real time."""
    return game.status in ("live", "final", "in_progress")


def get_matchup_real_games(db: Session, dg: SeriesDailyGame, real_games_on_date: list[Game]) -> list[Game]:
    """Finds the real NHL games on this date that involve players on either team's roster."""
    if not real_games_on_date:
        return []
    home_roster = po.roster_players(db, dg.home_team_id)
    away_roster = po.roster_players(db, dg.away_team_id)
    matchup_real_team_ids = set()
    for _, (_, p) in home_roster.items():
        if p and p.real_team_id:
            matchup_real_team_ids.add(p.real_team_id)
    for _, (_, p) in away_roster.items():
        if p and p.real_team_id:
            matchup_real_team_ids.add(p.real_team_id)
    matchup_games = [
        g for g in real_games_on_date
        if g.home_team_id in matchup_real_team_ids or g.away_team_id in matchup_real_team_ids
    ]
    return matchup_games if matchup_games else real_games_on_date



def parse_standings(payload: dict | None) -> dict[str, tuple[int, int]]:
    """team abbreviation -> (NHL standings points, games played) from a standings-by-date response. Pure."""
    out: dict[str, tuple[int, int]] = {}
    for row in (payload or {}).get("standings", []):
        abbrev = (row.get("teamAbbrev") or {}).get("default")
        if abbrev and row.get("points") is not None:
            out[abbrev] = (int(row["points"]), int(row.get("gamesPlayed") or 0))
    return out


def fetch_standings(as_of: datetime.date) -> dict[str, tuple[int, int]]:
    """The NHL standings as of a date (api-web.nhle.com/v1/standings/{date}); empty on any failure."""
    return parse_standings(_fetch_json(STANDINGS_URL.format(date=as_of.isoformat())))


def store_standings_snapshot(db: Session, nhl: Universe, as_of: datetime.date,
                             standings: dict[str, tuple[int, int]]) -> int:
    """Insert-only: a team's snapshot for a date is never overwritten, so a selection made at lock stays reproducible.
    Returns the number of rows added. Flushes, does not commit."""
    teams = {t.external_ref: t for t in db.query(RealTeam).filter_by(universe_id=nhl.id) if t.external_ref}
    have = {r[0] for r in db.query(NhlStandingsSnapshot.real_team_id).filter_by(universe_id=nhl.id, as_of_date=as_of)}
    added = 0
    for abbrev, (points, played) in sorted(standings.items()):
        team = teams.get(abbrev)
        if team is None or team.id in have:
            continue
        db.add(NhlStandingsSnapshot(universe_id=nhl.id, real_team_id=team.id, as_of_date=as_of,
                                    points=points, games_played=played))
        added += 1
    db.flush()
    return added


def ensure_standings_snapshot(db: Session, nhl: Universe, as_of: datetime.date) -> int:
    """Fetch and store the day's standings once, before the day's first puck drop. A snapshot taken after a game has
    started would already include results the lock must not see, so it is skipped (returns 0)."""
    if db.query(NhlStandingsSnapshot.id).filter_by(universe_id=nhl.id, as_of_date=as_of).first():
        return 0
    start = datetime.datetime.combine(as_of, datetime.time.min)
    started = db.query(Game.id).filter(
        Game.universe_id == nhl.id, Game.kickoff_at >= start, Game.kickoff_at < start + datetime.timedelta(days=1),
        Game.status != "scheduled").first()
    if started:
        return 0
    return store_standings_snapshot(db, nhl, as_of, fetch_standings(as_of))


def _shot_clock_seconds(period: int, clock_str: str) -> int:
    try:
        minutes, seconds = clock_str.split(":")
        return (period - 1) * 1200 + int(minutes) * 60 + int(seconds)
    except Exception:
        return 0


def ingest_daily_slate_if_needed(db: Session, nhl: Universe, target_date: datetime.date) -> int:
    """Fetches and stores real NHL games for target_date from the NHL API if not already stored. Returns count of games stored."""
    start_day = datetime.datetime.combine(target_date, datetime.time.min)
    end_day = start_day + datetime.timedelta(days=1)
    games_for_day = db.query(Game).filter(
        Game.universe_id == nhl.id, Game.kickoff_at >= start_day, Game.kickoff_at < end_day
    ).all()
    game_ids = [g.id for g in games_for_day]
    try:
        with db.begin_nested():
            ensure_standings_snapshot(db, nhl, target_date)  # Best Matchup reads the snapshot dated the lock day
    except Exception:
        pass
    has_shots = db.query(Shot).filter(Shot.game_id.in_(game_ids)).count() > 0 if game_ids else False
    all_final = bool(games_for_day) and all(g.status == "final" for g in games_for_day)
    if has_shots and all_final:
        return len(games_for_day)

    sched_data = _fetch_json(SCHEDULE_URL.format(date=target_date.isoformat()))
    if not sched_data:
        return 0

    game_week = sched_data.get("gameWeek", [])
    today_block = next((day for day in game_week if day.get("date") == target_date.isoformat()), None)
    if not today_block or not today_block.get("games"):
        return 0

    games_today = today_block["games"]

    teams = db.query(RealTeam).filter_by(universe_id=nhl.id).all()
    teams_by_ref = {t.external_ref: t for t in teams if t.external_ref}

    asset_by_ref: dict[str, int] = {}
    for ref, asset_id in db.query(Player.external_ref, DraftableAsset.id).join(
        DraftableAsset, DraftableAsset.player_id == Player.id
    ).filter(DraftableAsset.asset_type == "player", Player.external_ref.isnot(None)):
        asset_by_ref[str(ref)] = asset_id

    players_by_ref: dict[str, Player] = {
        p.external_ref: p
        for p in db.query(Player).filter(Player.universe_id == nhl.id, Player.external_ref.isnot(None)).all()
    }

    season_year = target_date.year if target_date.month >= 9 else target_date.year - 1
    w_start = week_start(target_date)
    w_end = w_start + datetime.timedelta(days=6)
    week = db.query(Week).filter(
        Week.universe_id == nhl.id, Week.season_year == season_year, Week.start_date <= target_date, Week.end_date >= target_date
    ).first()
    if not week:
        existing_numbers = {
            w.week_number for w in db.query(Week.week_number).filter(
                Week.universe_id == nhl.id, Week.season_year == season_year
            )
        }
        next_num = 0
        while next_num in existing_numbers:
            next_num += 1
        week = Week(
            universe_id=nhl.id, season_year=season_year, week_number=next_num, start_date=w_start, end_date=w_end, is_playoffs=False,
        )
        db.add(week)
        db.flush()

    games_inserted = 0
    for g_meta in games_today:
        nhl_game_id = g_meta["id"]
        away_abbrev = g_meta.get("awayTeam", {}).get("abbrev")
        home_abbrev = g_meta.get("homeTeam", {}).get("abbrev")
        home_team = teams_by_ref.get(home_abbrev)
        away_team = teams_by_ref.get(away_abbrev)
        if not home_team or not away_team:
            continue

        game_start_et = None
        if g_meta.get("startTimeUTC"):
            try:
                game_start_utc = datetime.datetime.fromisoformat(g_meta["startTimeUTC"].replace("Z", "+00:00"))
                game_start_et = to_eastern_naive(game_start_utc)
            except Exception:
                pass
        if not game_start_et:
            game_start_et = datetime.datetime.combine(target_date, datetime.time(19, 0))

        raw_state = g_meta.get("gameState", "")
        if raw_state in ("OFF", "FINAL"):
            game_status = "final"
        elif raw_state in ("LIVE", "CRIT"):
            game_status = "live"
        else:
            game_status = "scheduled"
        home_score = g_meta.get("homeTeam", {}).get("score", 0)
        away_score = g_meta.get("awayTeam", {}).get("score", 0)

        # Match existing Game or insert a new one (+/- 18h window catches legacy UTC kickoff rows)
        win_start = game_start_et - datetime.timedelta(hours=18)
        win_end = game_start_et + datetime.timedelta(hours=18)
        existing_games = db.query(Game).filter(
            Game.universe_id == nhl.id, Game.home_team_id == home_team.id,
            Game.away_team_id == away_team.id, Game.kickoff_at >= win_start, Game.kickoff_at <= win_end
        ).all()
        if existing_games:
            game = existing_games[0]
            game.home_score = home_score
            game.away_score = away_score
            game.status = game_status
            game.kickoff_at = game_start_et
            if len(existing_games) > 1:
                for dup in existing_games[1:]:
                    db.delete(dup)
            db.flush()
        else:
            game = Game(
                universe_id=nhl.id, week_id=week.id, home_team_id=home_team.id, away_team_id=away_team.id,
                home_score=home_score, away_score=away_score, kickoff_at=game_start_et,
                status=game_status,
            )
            db.add(game)
            db.flush()

        # If already final and has shots, no need to re-fetch PBP
        existing_shot_count = db.query(Shot).filter_by(game_id=game.id).count()
        if existing_shot_count > 0 and game.status == "final":
            games_inserted += 1
            continue

        pbp_data = _fetch_json(PBP_URL.format(game=nhl_game_id))
        if not pbp_data:
            games_inserted += 1
            continue

        # If re-syncing an existing game, clear prior live shots/events/credits before re-inserting
        if existing_shot_count > 0:
            shot_ids = [s[0] for s in db.query(Shot.id).filter_by(game_id=game.id).all()]
            event_ids = [e[0] for e in db.query(GameEvent.id).filter_by(game_id=game.id).all()]
            if shot_ids:
                db.query(ShotCredit).filter(ShotCredit.shot_id.in_(shot_ids)).delete(synchronize_session=False)
                db.query(Shot).filter(Shot.id.in_(shot_ids)).delete(synchronize_session=False)
            if event_ids:
                db.query(EventCredit).filter(EventCredit.event_id.in_(event_ids)).delete(synchronize_session=False)
                db.query(GameEvent).filter(GameEvent.id.in_(event_ids)).delete(synchronize_session=False)
            db.flush()

        team_by_nhl_id = {
            pbp_data["homeTeam"]["id"]: home_team.id,
            pbp_data["awayTeam"]["id"]: away_team.id,
        }
        for spot in pbp_data.get("rosterSpots", []):
            p_id_str = str(spot["playerId"])
            p_team_id = team_by_nhl_id.get(spot.get("teamId"), home_team.id)
            existing_player = players_by_ref.get(p_id_str)
            if existing_player:
                if not existing_player.active or existing_player.real_team_id != p_team_id:
                    existing_player.active = True
                    existing_player.real_team_id = p_team_id
                    db.flush()
                if p_id_str not in asset_by_ref:
                    new_asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=existing_player.id)
                    db.add(new_asset)
                    db.flush()
                    asset_by_ref[p_id_str] = new_asset.id
            else:
                first = spot.get("firstName", {}).get("default", "")
                last = spot.get("lastName", {}).get("default", "")
                p_name = f"{first} {last}".strip() or f"Player {p_id_str}"
                pos = POSITION_MAP.get(spot.get("positionCode"), "C")
                new_player = Player(
                    universe_id=nhl.id, real_team_id=p_team_id, full_name=p_name, position=pos,
                    external_ref=p_id_str, active=True,
                )
                db.add(new_player)
                db.flush()
                players_by_ref[p_id_str] = new_player
                new_asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=new_player.id)
                db.add(new_asset)
                db.flush()
                asset_by_ref[p_id_str] = new_asset.id

        shot_records = []
        event_records = []
        shots_to_add = []
        events_to_add = []

        for play in pbp_data.get("plays", []):
            kind = play.get("typeDescKey")
            if kind not in EVENT_TYPES:
                continue
            if play.get("periodDescriptor", {}).get("periodType") == "SO":
                continue

            period = play.get("periodDescriptor", {}).get("number", 1)
            clock_str = play.get("timeInPeriod", "00:00")
            game_seconds = _shot_clock_seconds(period, clock_str)
            d = play.get("details", {})
            owner_nhl_id = d.get("eventOwnerTeamId")
            shooting_team_id = home_team.id if owner_nhl_id == pbp_data["homeTeam"]["id"] else away_team.id
            defending_team_id = away_team.id if shooting_team_id == home_team.id else home_team.id

            if kind == "goal":
                refs = (d.get("scoringPlayerId"), d.get("assist1PlayerId"), d.get("assist2PlayerId"))
            elif kind == "blocked-shot":
                refs = (d.get("blockingPlayerId"), d.get("shootingPlayerId"), None)
            elif kind == "hit":
                refs = (d.get("hittingPlayerId"), d.get("hitteePlayerId"), None)
            else:
                refs = (d.get("shootingPlayerId"), None, None)

            act_id = asset_by_ref.get(str(refs[0])) if refs[0] else None
            sec_id = asset_by_ref.get(str(refs[1])) if refs[1] else None
            ter_id = asset_by_ref.get(str(refs[2])) if refs[2] else None

            x_coord = d.get("xCoord")
            y_coord = d.get("yCoord")
            zone_code = d.get("zoneCode")
            shot_type = d.get("shotType")

            is_goal = kind == "goal"
            is_empty_net = False
            pre_shot_xg = _calculate_xg(x_coord, y_coord, is_goal, is_empty_net)

            if kind in ("goal", "shot-on-goal"):
                goalie_ref = d.get("goalieInNetId")
                goalie_asset_id = asset_by_ref.get(str(goalie_ref)) if goalie_ref else None
                shot_row = Shot(
                    game_id=game.id, source="nhl_api", external_shot_id=f"{nhl_game_id}:{play.get('eventId', len(shots_to_add))}",
                    shooting_team_id=shooting_team_id, goalie_team_id=defending_team_id,
                    shooter_asset_id=act_id, goalie_asset_id=goalie_asset_id,
                    pre_shot_xg=pre_shot_xg, is_goal=is_goal, is_empty_net=is_empty_net,
                    period=period, clock=str(game_seconds), shot_type=shot_type,
                    strength_state="ev",
                )
                shots_to_add.append(shot_row)
                shot_records.append(ShotRecord(
                    shot_id=len(shots_to_add), shooting_team_id=shooting_team_id, goalie_team_id=defending_team_id,
                    shooter_asset_id=act_id, goalie_asset_id=goalie_asset_id,
                    pre_shot_xg=pre_shot_xg, is_goal=is_goal, is_empty_net=is_empty_net, game_seconds=game_seconds,
                ))

            evt_row = GameEvent(
                game_id=game.id, source="nhl_api", external_event_id=f"{nhl_game_id}:{play.get('eventId', len(events_to_add))}",
                event_type=kind, period=period, game_seconds=game_seconds,
                team_id=shooting_team_id, actor_ref=str(refs[0]) if refs[0] else None,
                secondary_ref=str(refs[1]) if refs[1] else None, tertiary_ref=str(refs[2]) if refs[2] else None,
                actor_asset_id=act_id, secondary_asset_id=sec_id, tertiary_asset_id=ter_id,
                x_coord=x_coord, y_coord=y_coord, zone_code=zone_code, shot_type=shot_type,
            )
            events_to_add.append(evt_row)
            event_records.append(EventRecord(
                event_id=len(events_to_add), event_type=kind, team_id=shooting_team_id,
                game_seconds=game_seconds, actor_asset_id=act_id, secondary_asset_id=sec_id, tertiary_asset_id=ter_id,
                x=x_coord, y=y_coord,
            ))

        db.add_all(shots_to_add)
        db.add_all(events_to_add)
        db.flush()

        # Update IDs
        for idx, s_row in enumerate(shots_to_add):
            shot_records[idx] = ShotRecord(
                shot_id=s_row.id, shooting_team_id=shot_records[idx].shooting_team_id,
                goalie_team_id=shot_records[idx].goalie_team_id, shooter_asset_id=shot_records[idx].shooter_asset_id,
                goalie_asset_id=shot_records[idx].goalie_asset_id, pre_shot_xg=shot_records[idx].pre_shot_xg,
                is_goal=shot_records[idx].is_goal, is_empty_net=shot_records[idx].is_empty_net,
                game_seconds=shot_records[idx].game_seconds,
            )
        for idx, e_row in enumerate(events_to_add):
            event_records[idx] = EventRecord(
                event_id=e_row.id, event_type=event_records[idx].event_type, team_id=event_records[idx].team_id,
                game_seconds=event_records[idx].game_seconds, actor_asset_id=event_records[idx].actor_asset_id,
                secondary_asset_id=event_records[idx].secondary_asset_id, tertiary_asset_id=event_records[idx].tertiary_asset_id,
                x=event_records[idx].x, y=event_records[idx].y,
            )

        estimator = BlockXgEstimator.fit(event_records) if event_records else None
        result = process_game(shot_records, event_records, estimator, teams=(home_team.id, away_team.id))
        shot_credit_rows = [
            ShotCredit(shot_id=row.shot_id, draftable_asset_id=row.asset_id, role=row.role, credit_points=row.points)
            for row in result.credits
        ]
        event_credit_rows = [
            EventCredit(event_id=row.event_id, draftable_asset_id=row.asset_id, role=row.role, credit_points=row.points)
            for row in result.event_credits
        ]
        db.add_all(shot_credit_rows)
        db.add_all(event_credit_rows)
        db.flush()
        games_inserted += 1

    return games_inserted


def seed_week_schedule_if_needed(db: Session, nhl: Universe, target_date: datetime.date) -> None:
    """Ensure upcoming games in this week are recorded with kickoff times and 'scheduled' status so Pecking Order resolves properly."""
    w_start = week_start(target_date)
    w_end = w_start + datetime.timedelta(days=7)
    start_dt = datetime.datetime.combine(w_start, datetime.time.min)
    end_dt = datetime.datetime.combine(w_end, datetime.time.min)
    count = db.query(Game).filter(Game.universe_id == nhl.id, Game.kickoff_at >= start_dt, Game.kickoff_at < end_dt).count()
    if count >= 15:
        return

    sched_data = _fetch_json(SCHEDULE_URL.format(date=w_start.isoformat()))
    if not sched_data:
        return

    teams = db.query(RealTeam).filter_by(universe_id=nhl.id).all()
    teams_by_ref = {t.external_ref: t for t in teams if t.external_ref}

    season_year = w_start.year if w_start.month >= 9 else w_start.year - 1
    week = db.query(Week).filter(
        Week.universe_id == nhl.id, Week.season_year == season_year, Week.start_date <= w_start, Week.end_date >= w_start
    ).first()
    if not week:
        existing_numbers = {
            w.week_number for w in db.query(Week.week_number).filter(
                Week.universe_id == nhl.id, Week.season_year == season_year
            )
        }
        next_num = 1
        while next_num in existing_numbers:
            next_num += 1
        week = Week(
            universe_id=nhl.id, season_year=season_year, week_number=next_num,
            start_date=w_start, end_date=w_start + datetime.timedelta(days=6), is_playoffs=False,
        )
        db.add(week)
        db.flush()

    for day_obj in sched_data.get("gameWeek", []):
        day_str = day_obj.get("date")
        if not day_str:
            continue
        try:
            d_date = datetime.date.fromisoformat(day_str)
        except Exception:
            continue
        for g_meta in day_obj.get("games", []):
            away_abbrev = g_meta.get("awayTeam", {}).get("abbrev")
            home_abbrev = g_meta.get("homeTeam", {}).get("abbrev")
            home_team = teams_by_ref.get(home_abbrev)
            away_team = teams_by_ref.get(away_abbrev)
            if not home_team or not away_team:
                continue

            game_start_et = None
            if g_meta.get("startTimeUTC"):
                try:
                    game_start_utc = datetime.datetime.fromisoformat(g_meta["startTimeUTC"].replace("Z", "+00:00"))
                    game_start_et = to_eastern_naive(game_start_utc)
                except Exception:
                    pass
            if not game_start_et:
                game_start_et = datetime.datetime.combine(d_date, datetime.time(19, 0))

            kickoff_naive = game_start_et
            raw_state = g_meta.get("gameState", "")
            if raw_state in ("OFF", "FINAL"):
                game_status = "final"
            elif raw_state in ("LIVE", "CRIT"):
                game_status = "live"
            else:
                game_status = "scheduled"
            home_score = g_meta.get("homeTeam", {}).get("score", 0)
            away_score = g_meta.get("awayTeam", {}).get("score", 0)

            win_start = kickoff_naive - datetime.timedelta(hours=18)
            win_end = kickoff_naive + datetime.timedelta(hours=18)
            existing_games = db.query(Game).filter(
                Game.universe_id == nhl.id, Game.home_team_id == home_team.id,
                Game.away_team_id == away_team.id, Game.kickoff_at >= win_start, Game.kickoff_at <= win_end
            ).all()
            if existing_games:
                existing = existing_games[0]
                existing.kickoff_at = kickoff_naive
                existing.status = game_status
                existing.home_score = home_score
                existing.away_score = away_score
                if len(existing_games) > 1:
                    for dup in existing_games[1:]:
                        db.delete(dup)
            else:
                new_game = Game(
                    universe_id=nhl.id, week_id=week.id, home_team_id=home_team.id, away_team_id=away_team.id,
                    home_score=home_score, away_score=away_score, kickoff_at=kickoff_naive, status=game_status
                )
                db.add(new_game)
    db.flush()


def sync_nhl_roster_callups(db: Session, nhl: Universe, teams: list[RealTeam] | None = None) -> int:
    """Syncs active NHL team rosters from the public NHL API so call-ups from the AHL
    are immediately marked active=True with DraftableAsset records in the player pool.
    Returns the number of players activated or added.
    """
    if teams is None:
        teams = db.query(RealTeam).filter_by(universe_id=nhl.id).all()
    if not teams:
        return 0

    players_by_ref: dict[str, Player] = {
        p.external_ref: p
        for p in db.query(Player).filter(Player.universe_id == nhl.id, Player.external_ref.isnot(None)).all()
    }
    asset_by_ref: dict[str, int] = {
        str(ref): asset_id
        for ref, asset_id in db.query(Player.external_ref, DraftableAsset.id).join(
            DraftableAsset, DraftableAsset.player_id == Player.id
        ).filter(DraftableAsset.asset_type == "player", Player.external_ref.isnot(None)).all()
    }

    activated_count = 0
    for team in teams:
        if not team.external_ref:
            continue
        roster_data = _fetch_json(ROSTER_URL.format(team=team.external_ref))
        if not roster_data:
            continue

        raw_players = (
            roster_data.get("forwards", [])
            + roster_data.get("defensemen", [])
            + roster_data.get("goalies", [])
        )
        for item in raw_players:
            p_id = item.get("id")
            if not p_id:
                continue
            p_id_str = str(p_id)
            existing = players_by_ref.get(p_id_str)
            if existing:
                if not existing.active or existing.real_team_id != team.id:
                    existing.active = True
                    existing.real_team_id = team.id
                    activated_count += 1
                if p_id_str not in asset_by_ref:
                    asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=existing.id)
                    db.add(asset)
                    db.flush()
                    asset_by_ref[p_id_str] = asset.id
            else:
                first = item.get("firstName", {}).get("default", "")
                last = item.get("lastName", {}).get("default", "")
                name = f"{first} {last}".strip() or f"Player {p_id_str}"
                pos = POSITION_MAP.get(item.get("positionCode"), "C")
                new_player = Player(
                    universe_id=nhl.id, real_team_id=team.id, full_name=name, position=pos,
                    external_ref=p_id_str, active=True,
                )
                db.add(new_player)
                db.flush()
                players_by_ref[p_id_str] = new_player
                asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=new_player.id)
                db.add(asset)
                db.flush()
                asset_by_ref[p_id_str] = asset.id
                activated_count += 1

    if activated_count:
        db.commit()
    return activated_count


def auto_sync_league_scoreboard(db: Session, league_id: int) -> None:
    """Safe, idempotent auto-sync called on read for an NHL league. Ensures schedule alignment, slate ingestion, and simulation."""
    league = db.get(League, league_id)
    if not league or league.series_length_days is None:
        return

    nhl = db.query(Universe).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id).filter(
        LeagueUniverse.league_id == league_id, Universe.code == "nhl"
    ).first()
    if not nhl:
        return

    today_et = current_eastern_date()
    max_active_date = today_et
    # Ensure this week's full schedule is seeded so Pecking Order accurately sees all games in the window
    try:
        seed_week_schedule_if_needed(db, nhl, max_active_date)
        db.commit()
    except Exception:
        db.rollback()

    # Sync active NHL team rosters / AHL callups into the player pool
    try:
        sync_nhl_roster_callups(db, nhl)
        db.commit()
    except Exception:
        db.rollback()

    # 2. Check unfinalized fantasy daily games up to max_active_date
    unfinalized_dates = [
        d[0] for d in (
            db.query(SeriesDailyGame.calendar_date)
            .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(
                LeagueSeries.league_id == league_id,
                SeriesDailyGame.calendar_date <= max_active_date,
                SeriesDailyGame.status != "FINAL",
                SeriesDailyGame.is_matchup_day.is_(True),
            )
            .distinct()
            .order_by(SeriesDailyGame.calendar_date)
            .all()
        )
    ]
    if not unfinalized_dates:
        return

    now_utc = service.utcnow()

    for sync_date in unfinalized_dates:
        # Ingest today's slate if not stored
        try:
            ingest_daily_slate_if_needed(db, nhl, sync_date)
            db.flush()
        except Exception:
            pass

        # Check if real games for sync_date are final
        start_day = datetime.datetime.combine(sync_date, datetime.time.min)
        end_day = start_day + datetime.timedelta(days=1)

        # Heal any legacy UTC kickoff rows stored with hour < 6 (belonging to yesterday evening)
        legacy_utc_games = db.query(Game).filter(
            Game.universe_id == nhl.id,
            Game.kickoff_at >= start_day,
            Game.kickoff_at < start_day + datetime.timedelta(hours=6),
        ).all()
        for lg in legacy_utc_games:
            lg.kickoff_at = to_eastern_naive(lg.kickoff_at)
            db.flush()

        real_games = db.query(Game).filter(
            Game.universe_id == nhl.id, Game.kickoff_at >= start_day, Game.kickoff_at < end_day
        ).all()
        any_real_started = bool(real_games) and any(is_real_game_started(g, now_utc) for g in real_games)
        all_real_final = bool(real_games) and all(g.status == "final" for g in real_games)

        daily_games = (
            db.query(SeriesDailyGame)
            .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(
                LeagueSeries.league_id == league_id,
                SeriesDailyGame.calendar_date == sync_date,
                SeriesDailyGame.is_matchup_day.is_(True),
            )
            .all()
        )

        # If real games exist for today's slate but zero have started or finished, ensure all fantasy games are SCHEDULED
        if bool(real_games) and not any_real_started and not all_real_final:
            for dg in daily_games:
                if dg.status != "SCHEDULED" or dg.home_score != 0 or dg.away_score != 0 or dg.current_period != 0 or dg.seconds_remaining != 1200:
                    dg.status = "SCHEDULED"
                    dg.home_score = 0
                    dg.away_score = 0
                    dg.current_period = 0
                    dg.seconds_remaining = 1200
                    dg.home_shots = 0
                    dg.away_shots = 0
                    sim = service.get_sim(db, dg.id)
                    if sim is not None:
                        sim.status = "SCHEDULED"
                        sim.started_at = None
                        sim.finalized_at = None
                    db.flush()
                # If series was marked IN_PROGRESS solely due to prematurely started game, reset to SCHEDULED
                siblings = db.query(SeriesDailyGame).filter_by(series_id=dg.series_id).all()
                if all(s.status == "SCHEDULED" for s in siblings if s.is_matchup_day):
                    series_row = db.get(LeagueSeries, dg.series_id)
                    if series_row and series_row.status != "SCHEDULED":
                        series_row.status = "SCHEDULED"
                        db.flush()
            continue

        for dg in daily_games:
            sim = service.get_sim(db, dg.id)
            if sim is None:
                try:
                    sim = loader.publish_game(db, dg)
                except Exception:
                    continue
            shape = service.shape_of(sim)

            # Determine real games that involve this specific fantasy matchup's players
            matchup_games = get_matchup_real_games(db, dg, real_games)
            matchup_started_games = [g for g in matchup_games if is_real_game_started(g, now_utc)]
            matchup_started = bool(matchup_started_games)
            matchup_all_final = bool(matchup_games) and all(g.status == "final" for g in matchup_games)

            matchup_kickoffs_utc = [eastern_naive_to_utc(g.kickoff_at) for g in matchup_started_games if g.kickoff_at]
            earliest_kickoff_utc = min(matchup_kickoffs_utc, default=now_utc)
            start_time = earliest_kickoff_utc if earliest_kickoff_utc <= now_utc else now_utc

            if dg.status == "SCHEDULED" and (matchup_started or matchup_all_final):
                try:
                    service.start_game(db, sim, start_time)
                except Exception:
                    pass

            is_past_date = (sync_date < max_active_date)
            should_finalize = matchup_all_final or (is_past_date and matchup_started)

            if should_finalize and dg.status != "FINAL":
                try:
                    sim.started_at = now_utc - datetime.timedelta(seconds=shape.window_seconds + 120)
                    service.sync(db, sim, now_utc)
                except Exception:
                    pass
            elif matchup_started and not matchup_all_final:
                try:
                    if sim.status == "LIVE":
                        elapsed = (now_utc - sim.started_at).total_seconds() if sim.started_at else 0
                        if elapsed >= shape.window_seconds:
                            sim.started_at = now_utc - datetime.timedelta(seconds=shape.window_seconds - 60)
                    service.sync(db, sim, now_utc)
                except Exception:
                    pass

    db.commit()
