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
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit
from fantasy_sports.sports.hockey.pipeline import (
    BlockXgEstimator, EventRecord, ShotRecord, process_game, week_start,
    window_of, window_dates,
)
from fantasy_sports.sports.hockey.simulator import loader

SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{date}"
PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game}/play-by-play"
ROSTER_URL = "https://api-web.nhle.com/v1/roster/{team}/current"
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
    existing_games = db.query(Game).filter(
        Game.universe_id == nhl.id, Game.kickoff_at >= start_day, Game.kickoff_at < end_day
    ).count()
    if existing_games > 0:
        return existing_games

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

        game_start_utc = None
        if g_meta.get("startTimeUTC"):
            try:
                game_start_utc = datetime.datetime.fromisoformat(g_meta["startTimeUTC"].replace("Z", "+00:00"))
            except Exception:
                pass
        if not game_start_utc:
            game_start_utc = datetime.datetime.combine(target_date, datetime.time(19, 0))

        game_status = "final" if g_meta.get("gameState") in ("OFF", "FINAL") else "live"
        home_score = g_meta.get("homeTeam", {}).get("score", 0)
        away_score = g_meta.get("awayTeam", {}).get("score", 0)

        game = Game(
            universe_id=nhl.id, week_id=week.id, home_team_id=home_team.id, away_team_id=away_team.id,
            home_score=home_score, away_score=away_score, kickoff_at=game_start_utc.replace(tzinfo=None),
            status=game_status,
        )
        db.add(game)
        db.flush()

        pbp_data = _fetch_json(PBP_URL.format(game=nhl_game_id))
        if not pbp_data:
            games_inserted += 1
            continue

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

            game_start_utc = None
            if g_meta.get("startTimeUTC"):
                try:
                    game_start_utc = datetime.datetime.fromisoformat(g_meta["startTimeUTC"].replace("Z", "+00:00"))
                except Exception:
                    pass
            if not game_start_utc:
                game_start_utc = datetime.datetime.combine(d_date, datetime.time(19, 0))

            kickoff_naive = game_start_utc.replace(tzinfo=None)
            game_status = "final" if g_meta.get("gameState") in ("OFF", "FINAL") else "scheduled"
            home_score = g_meta.get("homeTeam", {}).get("score", 0)
            away_score = g_meta.get("awayTeam", {}).get("score", 0)

            day_start = datetime.datetime.combine(d_date, datetime.time.min)
            day_end = day_start + datetime.timedelta(days=1)
            existing = db.query(Game).filter(
                Game.universe_id == nhl.id, Game.home_team_id == home_team.id,
                Game.away_team_id == away_team.id, Game.kickoff_at >= day_start, Game.kickoff_at < day_end
            ).first()
            if not existing:
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

    try:
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
        today_et = now_et.date()
    except Exception:
        today_et = datetime.date.today()

    try:
        now_pt = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
        today_pt = now_pt.date()
    except Exception:
        today_pt = today_et

    max_active_date = max(today_et, today_pt)
    sunday = today_pt - datetime.timedelta(days=today_pt.weekday() + 1 if today_pt.weekday() != 6 else 0)

    # 1. Check schedule alignment
    first_series = db.query(LeagueSeries).filter_by(league_id=league_id).order_by(LeagueSeries.id).first()
    if first_series and first_series.start_date != sunday:
        # Check if games have started
        started_count = db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id).filter(
            LeagueSeries.league_id == league_id, SeriesDailyGame.status != "SCHEDULED"
        ).count()
        if started_count == 0:
            try:
                series_schedule.generate_schedule(db, league, sunday, 1, replace=True)
                db.flush()
            except Exception:
                pass

    # Ensure this week's full schedule is seeded so Pecking Order accurately sees all games in the window
    try:
        seed_week_schedule_if_needed(db, nhl, max_active_date)
        db.flush()
    except Exception:
        pass

    # Sync active NHL team rosters / AHL callups into the player pool
    try:
        sync_nhl_roster_callups(db, nhl)
        db.flush()
    except Exception:
        pass

    # 2. Check unfinalized fantasy daily games up to max_active_date
    unfinalized_dates = [
        d[0] for d in (
            db.query(SeriesDailyGame.calendar_date)
            .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(
                LeagueSeries.league_id == league_id,
                SeriesDailyGame.calendar_date <= max_active_date,
                SeriesDailyGame.status != "FINAL",
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
        real_games = db.query(Game).filter(
            Game.universe_id == nhl.id, Game.kickoff_at >= start_day, Game.kickoff_at < end_day
        ).all()
        any_real_started = bool(real_games) and any(g.status in ("live", "final") for g in real_games)
        all_real_final = bool(real_games) and all(g.status == "final" for g in real_games)

        daily_games = (
            db.query(SeriesDailyGame)
            .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id, SeriesDailyGame.calendar_date == sync_date)
            .all()
        )

        for dg in daily_games:
            sim = service.get_sim(db, dg.id)
            if sim is None:
                try:
                    sim = loader.publish_game(db, dg)
                except Exception:
                    continue
            if dg.status == "SCHEDULED" and (any_real_started or all_real_final):
                try:
                    service.start_game(db, sim, now_utc)
                except Exception:
                    pass

            # Check whether this game's multi-day window has concluded
            w_start_d, w_idx = window_of(dg.calendar_date)
            w_dates = window_dates(w_start_d, w_idx)
            is_window_concluded = (max_active_date >= w_dates[-1])

            if is_window_concluded and all_real_final and dg.status != "FINAL":
                try:
                    shape = service.shape_of(sim)
                    sim.started_at = now_utc - datetime.timedelta(seconds=shape.window_seconds + 120)
                    service.sync(db, sim, now_utc)
                except Exception:
                    pass
            elif any_real_started or all_real_final:
                try:
                    service.sync(db, sim, now_utc)
                except Exception:
                    pass

    db.commit()
