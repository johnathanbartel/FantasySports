"""Ingest real NHL games, play-by-play, shots, and credits for a specific date (daily slate).

Source: NHL public web API (api-web.nhle.com, no key required).
- Schedule: https://api-web.nhle.com/v1/schedule/{date}
- Play-by-play: https://api-web.nhle.com/v1/gamecenter/{gameId}/play-by-play

Run from the repository root with the data-pipeline environment:

    $env:DATABASE_URL = "sqlite:///./user_run.db"
    .venv/Scripts/python.exe scripts/ingest_nhl_daily_slate.py --date 2026-10-11

Features:
- Idempotent and resumable: cleans up previous runs for the same game before re-inserting.
- Auto-resolves players and draftable assets from roster spots.
- Computes empirical distance-based xG for every shot attempt.
- Runs process_game attribution to populate shot_credits and event_credits.
- Optional --publish-league <ID> to publish and start the day's fantasy series matchup.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./user_run.db")

import requests
from sqlalchemy import delete

from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    Game,
    League,
    Player,
    RealTeam,
    SeriesDailyGame,
    Universe,
    Week,
)
from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal, engine
from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, Shot, ShotCredit
from fantasy_sports.sports.hockey.pipeline import (
    BlockXgEstimator,
    EventRecord,
    ShotRecord,
    process_game,
    week_start,
)

SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{date}"
PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game}/play-by-play"
HEADERS = {"User-Agent": "FantasySportsCompany-NHLDailyIngest/1.0"}
SOURCE = "nhl_api"
POSITION_MAP = {"L": "LW", "R": "RW", "C": "C", "D": "D", "G": "G"}
EVENT_TYPES = {
    "goal", "shot-on-goal", "missed-shot", "blocked-shot",
    "hit", "giveaway", "takeaway", "faceoff",
}

# Empirical xG rates by 5-foot distance band from 120,000 NHL shots in the repository
EMPIRICAL_XG_BANDS = {
    0: 0.275, 1: 0.128, 2: 0.115, 3: 0.108, 4: 0.099,
    5: 0.083, 6: 0.068, 7: 0.050, 8: 0.035, 9: 0.027,
    10: 0.023, 11: 0.022, 12: 0.016, 13: 0.018, 14: 0.030,
}


def _fetch_json(url: str, cache_file: Path | None = None, delay: float = 0.5) -> dict:
    if cache_file and cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    last_err = None
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=(15, 60))
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.RequestException(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            if cache_file:
                cache_file.write_text(json.dumps(data), encoding="utf-8")
            time.sleep(delay)
            return data
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def _shot_clock_seconds(period: int, clock_str: str) -> int:
    try:
        minutes, seconds = clock_str.split(":")
        return (period - 1) * 1200 + int(minutes) * 60 + int(seconds)
    except Exception:
        return 0


def _calculate_xg(x: float | None, y: float | None, is_goal: bool, is_empty_net: bool) -> float:
    if is_empty_net:
        return 0.95
    if x is None or y is None:
        return 0.10 if is_goal else 0.06
    dist = math.hypot(89 - abs(x), y)
    band = min(int(dist // 5.0), 14)
    return EMPIRICAL_XG_BANDS.get(band, 0.05)


def to_eastern_naive(utc_dt: datetime.datetime) -> datetime.datetime:
    """Converts a UTC datetime to North American Eastern time (ET/EDT/EST) as a naive datetime."""
    year = utc_dt.year
    dst_start = datetime.datetime(year, 3, 8) + datetime.timedelta(days=(6 - datetime.datetime(year, 3, 8).weekday()) % 7)
    dst_end = datetime.datetime(year, 11, 1) + datetime.timedelta(days=(6 - datetime.datetime(year, 11, 1).weekday()) % 7)
    utc_naive = utc_dt.replace(tzinfo=None) if utc_dt.tzinfo else utc_dt
    is_dst = dst_start <= utc_naive < dst_end
    offset = datetime.timedelta(hours=-4 if is_dst else -5)
    return utc_naive + offset



def ingest_daily_slate(target_date: datetime.date, cache_dir: Path | None = None, publish_league_id: int | None = None) -> dict:
    Base.metadata.create_all(bind=engine)
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    date_str = target_date.isoformat()
    sched_cache = cache_dir / f"schedule_{date_str}.json" if cache_dir else None
    print(f"Fetching NHL schedule for {date_str}...")
    sched_data = _fetch_json(SCHEDULE_URL.format(date=date_str), sched_cache)

    games_today = []
    for week_obj in sched_data.get("gameWeek", []):
        if week_obj.get("date") == date_str:
            games_today = week_obj.get("games", [])
            break

    if not games_today:
        print(f"No NHL games found on {date_str}.")
        return {"games": 0, "shots": 0, "events": 0, "credits": 0}

    print(f"Found {len(games_today)} NHL games on {date_str}:")
    for g in games_today:
        away = g.get("awayTeam", {}).get("abbrev", "?")
        home = g.get("homeTeam", {}).get("abbrev", "?")
        state = g.get("gameState", "SCHEDULED")
        score = f"{g.get('awayTeam', {}).get('score', 0)} - {g.get('homeTeam', {}).get('score', 0)}"
        print(f"  Game {g.get('id')}: {away} at {home} ({score}, state={state})")

    stats = {
        "games_processed": 0,
        "shots_ingested": 0,
        "events_ingested": 0,
        "credits_ingested": 0,
        "new_players": 0,
    }

    with SessionLocal() as db:
        nhl = db.query(Universe).filter_by(code="nhl").first()
        if not nhl:
            raise RuntimeError("NHL universe not found in database. Run seed_hockey_universe.py first.")

        # Real teams map
        teams = db.query(RealTeam).filter_by(universe_id=nhl.id).all()
        teams_by_ref = {t.external_ref: t for t in teams if t.external_ref}

        # Player & Asset lookup
        asset_by_ref: dict[str, int] = {}
        for ref, asset_id in db.query(Player.external_ref, DraftableAsset.id).join(
            DraftableAsset, DraftableAsset.player_id == Player.id
        ).filter(DraftableAsset.asset_type == "player", Player.external_ref.isnot(None)):
            asset_by_ref[str(ref)] = asset_id

        players_by_ref: dict[str, Player] = {
            p.external_ref: p
            for p in db.query(Player).filter(Player.universe_id == nhl.id, Player.external_ref.isnot(None)).all()
        }

        # Week container for this date
        season_year = target_date.year if target_date.month >= 9 else target_date.year - 1
        w_start = week_start(target_date)
        w_end = w_start + datetime.timedelta(days=6)
        week = db.query(Week).filter(
            Week.universe_id == nhl.id,
            Week.season_year == season_year,
            Week.start_date <= target_date,
            Week.end_date >= target_date,
        ).first()
        if not week:
            max_wk = db.query(Week.week_number).filter_by(universe_id=nhl.id, season_year=season_year).order_by(Week.week_number.desc()).first()
            next_wk = (max_wk[0] + 1) if max_wk else 1
            week = Week(
                universe_id=nhl.id,
                season_year=season_year,
                week_number=next_wk,
                start_date=w_start,
                end_date=w_end,
                is_playoffs=False,
            )
            db.add(week)
            db.flush()

        for g_meta in games_today:
            nhl_game_id = g_meta["id"]
            away_abbrev = g_meta.get("awayTeam", {}).get("abbrev")
            home_abbrev = g_meta.get("homeTeam", {}).get("abbrev")
            home_team = teams_by_ref.get(home_abbrev)
            away_team = teams_by_ref.get(away_abbrev)
            if not home_team or not away_team:
                print(f"Warning: Could not match teams {away_abbrev} vs {home_abbrev}; skipping.")
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

            # Match or create Game row
            start_day = datetime.datetime.combine(target_date, datetime.time.min)
            end_day = start_day + datetime.timedelta(days=1)
            game = db.query(Game).filter(
                Game.universe_id == nhl.id,
                Game.home_team_id == home_team.id,
                Game.away_team_id == away_team.id,
                Game.kickoff_at >= start_day,
                Game.kickoff_at < end_day,
            ).first()

            if not game:
                game = Game(
                    universe_id=nhl.id,
                    week_id=week.id,
                    home_team_id=home_team.id,
                    away_team_id=away_team.id,
                    home_score=home_score,
                    away_score=away_score,
                    kickoff_at=game_start_et,
                    status=game_status,
                )
                db.add(game)
                db.flush()
            else:
                game.home_score = home_score
                game.away_score = away_score
                game.status = game_status
                game.kickoff_at = game_start_et
                db.flush()

            # Clean previous shots, events, and credits for this game for clean idempotency
            shot_ids = [s_id for (s_id,) in db.query(Shot.id).filter_by(game_id=game.id).all()]
            if shot_ids:
                db.query(ShotCredit).filter(ShotCredit.shot_id.in_(shot_ids)).delete(synchronize_session=False)
                db.query(Shot).filter_by(game_id=game.id).delete(synchronize_session=False)

            event_ids = [e_id for (e_id,) in db.query(GameEvent.id).filter_by(game_id=game.id).all()]
            if event_ids:
                db.query(EventCredit).filter(EventCredit.event_id.in_(event_ids)).delete(synchronize_session=False)
                db.query(GameEvent).filter_by(game_id=game.id).delete(synchronize_session=False)
            db.flush()

            # Fetch play-by-play
            pbp_cache = cache_dir / f"{nhl_game_id}.json" if cache_dir else None
            pbp_data = _fetch_json(PBP_URL.format(game=nhl_game_id), pbp_cache)

            # Auto-resolve / insert any new roster spot players
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
                        new_asset = DraftableAsset(
                            universe_id=nhl.id,
                            asset_type="player",
                            player_id=existing_player.id,
                        )
                        db.add(new_asset)
                        db.flush()
                        asset_by_ref[p_id_str] = new_asset.id
                else:
                    first = spot.get("firstName", {}).get("default", "")
                    last = spot.get("lastName", {}).get("default", "")
                    p_name = f"{first} {last}".strip() or f"Player {p_id_str}"
                    pos = POSITION_MAP.get(spot.get("positionCode"), "C")
                    new_player = Player(
                        universe_id=nhl.id,
                        real_team_id=p_team_id,
                        full_name=p_name,
                        position=pos,
                        external_ref=p_id_str,
                        active=True,
                    )
                    db.add(new_player)
                    db.flush()
                    players_by_ref[p_id_str] = new_player
                    new_asset = DraftableAsset(
                        universe_id=nhl.id,
                        asset_type="player",
                        player_id=new_player.id,
                    )
                    db.add(new_asset)
                    db.flush()
                    asset_by_ref[p_id_str] = new_asset.id
                    stats["new_players"] += 1

            # Ingest events & shots
            shot_records = []
            event_records = []
            shots_to_add = []
            events_to_add = []

            for play in pbp_data.get("plays", []):
                kind = play.get("typeDescKey")
                if kind not in EVENT_TYPES:
                    continue
                if play.get("periodDescriptor", {}).get("periodType") == "SO":
                    continue  # Shootout attempts do not have regular shot rows

                period = play.get("periodDescriptor", {}).get("number", 1)
                clock_str = play.get("timeInPeriod", "00:00")
                game_seconds = _shot_clock_seconds(period, clock_str)
                d = play.get("details", {})
                owner_nhl_id = d.get("eventOwnerTeamId")
                shooting_team_id = home_team.id if owner_nhl_id == pbp_data["homeTeam"]["id"] else away_team.id
                defending_team_id = away_team.id if shooting_team_id == home_team.id else home_team.id

                # References
                if kind == "goal":
                    refs = (d.get("scoringPlayerId"), d.get("assist1PlayerId"), d.get("assist2PlayerId"))
                elif kind == "blocked-shot":
                    refs = (d.get("blockingPlayerId"), d.get("shootingPlayerId"), None)
                elif kind == "hit":
                    refs = (d.get("hittingPlayerId"), d.get("hitteePlayerId"), None)
                elif kind == "faceoff":
                    refs = (d.get("winningPlayerId"), d.get("losingPlayerId"), None)
                elif kind in ("shot-on-goal", "missed-shot"):
                    refs = (d.get("shootingPlayerId"), None, None)
                else:
                    refs = (d.get("playerId"), None, None)

                ref_strs = tuple(str(r) if r else None for r in refs)
                asset_ids = tuple(asset_by_ref.get(r) if r else None for r in ref_strs)

                # Add GameEvent
                event_ext_id = f"{season_year}:{nhl_game_id}:{play['eventId']}"
                event_obj = GameEvent(
                    game_id=game.id,
                    source=SOURCE,
                    external_event_id=event_ext_id,
                    event_type=kind,
                    period=period,
                    game_seconds=game_seconds,
                    team_id=shooting_team_id,
                    actor_ref=ref_strs[0],
                    secondary_ref=ref_strs[1],
                    tertiary_ref=ref_strs[2],
                    actor_asset_id=asset_ids[0],
                    secondary_asset_id=asset_ids[1],
                    tertiary_asset_id=asset_ids[2],
                    x_coord=d.get("xCoord"),
                    y_coord=d.get("yCoord"),
                    zone_code=d.get("zoneCode"),
                    shot_type=d.get("shotType"),
                )
                db.add(event_obj)
                events_to_add.append(event_obj)

                # Add Shot if applicable
                if kind in ("goal", "shot-on-goal", "missed-shot"):
                    is_goal = (kind == "goal")
                    shooter_asset = asset_ids[0]
                    goalie_nhl_id = d.get("goalieInNetId")
                    goalie_asset = asset_by_ref.get(str(goalie_nhl_id)) if goalie_nhl_id else None
                    is_empty_net = (goalie_nhl_id is None)
                    xg = _calculate_xg(d.get("xCoord"), d.get("yCoord"), is_goal, is_empty_net)

                    shot_ext_id = f"{season_year}:{nhl_game_id}:{play['eventId']}"
                    shot_obj = Shot(
                        game_id=game.id,
                        source=SOURCE,
                        external_shot_id=shot_ext_id,
                        period=period,
                        clock=str(game_seconds),
                        strength_state=None,
                        shot_type=d.get("shotType"),
                        shooting_team_id=shooting_team_id,
                        goalie_team_id=defending_team_id,
                        shooter_asset_id=shooter_asset,
                        goalie_asset_id=goalie_asset,
                        pre_shot_xg=xg,
                        is_goal=is_goal,
                        is_empty_net=is_empty_net,
                    )
                    db.add(shot_obj)
                    shots_to_add.append(shot_obj)

            db.flush()

            # Build ShotRecords and EventRecords for pipeline attribution
            for s in shots_to_add:
                shot_records.append(
                    ShotRecord(
                        shot_id=s.id,
                        shooting_team_id=s.shooting_team_id,
                        goalie_team_id=s.goalie_team_id,
                        shooter_asset_id=s.shooter_asset_id,
                        goalie_asset_id=s.goalie_asset_id,
                        pre_shot_xg=s.pre_shot_xg or 0.0,
                        is_goal=bool(s.is_goal),
                        is_empty_net=bool(s.is_empty_net),
                        game_seconds=int(s.clock) if s.clock else 0,
                    )
                )
            for e in events_to_add:
                event_records.append(
                    EventRecord(
                        event_id=e.id,
                        event_type=e.event_type,
                        team_id=e.team_id,
                        game_seconds=e.game_seconds,
                        actor_ref=e.actor_ref,
                        secondary_ref=e.secondary_ref,
                        tertiary_ref=e.tertiary_ref,
                        actor_asset_id=e.actor_asset_id,
                        secondary_asset_id=e.secondary_asset_id,
                        tertiary_asset_id=e.tertiary_asset_id,
                        x=e.x_coord,
                        y=e.y_coord,
                    )
                )

            # Fit estimator and run pipeline attribution
            estimator = BlockXgEstimator.fit(event_records) if event_records else None
            res = process_game(shot_records, event_records, estimator, teams=(home_team.id, away_team.id))

            for cr in res.credits:
                db.add(ShotCredit(shot_id=cr.shot_id, draftable_asset_id=cr.asset_id, role=cr.role, credit_points=cr.points))
            for ec in res.event_credits:
                db.add(EventCredit(event_id=ec.event_id, draftable_asset_id=ec.asset_id, role=ec.role, credit_points=ec.points))

            db.flush()
            stats["games_processed"] += 1
            stats["shots_ingested"] += len(shots_to_add)
            stats["events_ingested"] += len(events_to_add)
            stats["credits_ingested"] += len(res.credits) + len(res.event_credits)

            print(
                f"  Scored Game {nhl_game_id} ({away_abbrev} at {home_abbrev}): "
                f"{len(shots_to_add)} shots ({sum(1 for s in shots_to_add if s.is_goal)} goals), "
                f"{len(events_to_add)} events, {len(res.credits)} shot credits, {len(res.event_credits)} event credits."
            )

        db.commit()

        # Optional: League publishing & live simulator activation
        if publish_league_id:
            from fantasy_sports.platform.simulator import service
            from fantasy_sports.sports.hockey.simulator import loader

            print(f"\nChecking series daily games for League {publish_league_id} on {date_str}...")
            daily_games = (
                db.query(SeriesDailyGame)
                .join(SeriesDailyGame.series)
                .filter(SeriesDailyGame.calendar_date == target_date)
                .all()
            )
            published = 0
            for dg in daily_games:
                if dg.series.league_id == publish_league_id:
                    try:
                        sim = loader.publish_game(db, dg)
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        service.start_game(db, sim, now_utc)
                        published += 1
                        print(f"  Published and started Daily Game {dg.id} (status: LIVE_SIMULATING)")
                    except Exception as exc:
                        print(f"  Could not publish Daily Game {dg.id}: {exc}")
            db.commit()
            if published:
                print(f"Successfully published {published} daily games for League {publish_league_id}.")

    print(
        f"\nIngestion summary for {date_str}:\n"
        f"  Games: {stats['games_processed']}\n"
        f"  Shots: {stats['shots_ingested']}\n"
        f"  Events: {stats['events_ingested']}\n"
        f"  Credits: {stats['credits_ingested']}\n"
        f"  New players registered: {stats['new_players']}"
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Ingest NHL schedule, play-by-play, shots, and credits for a daily slate.")
    parser.add_argument("--date", type=str, default=None, help="Slate date YYYY-MM-DD (defaults to today)")
    parser.add_argument("--cache-dir", type=str, default=None, help="Directory to cache raw NHL API responses")
    parser.add_argument("--publish-league", type=int, default=None, help="League ID to publish and simulate daily games for")
    args = parser.parse_args()

    if args.date:
        target_date = datetime.date.fromisoformat(args.date)
    else:
        target_date = datetime.date.today()

    cache_path = Path(args.cache_dir) if args.cache_dir else None
    ingest_daily_slate(target_date, cache_dir=cache_path, publish_league_id=args.publish_league)


if __name__ == "__main__":
    main()
