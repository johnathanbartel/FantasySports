"""
Caches full FBS seasons of CFBD play-by-play, game results, and the
1st-and-10 expected-points curve (indexed by yards from own goal) to data/cache/ (gitignored), so the EPA
calibration and the game replay never re-hit the API.

Files written (per year):
    data/cache/cfb_plays_<year>.jsonl.gz   one play per line
    data/cache/cfb_games_<year>.json       game id, week, teams, final score
    data/cache/cfb_roster_<year>.json      player id, team, position
    data/cache/cfb_defstats_<year>.json    per-player season defensive stats (with position)
plus, once:
    college/data/ep_curve.json             EP of a 1st-and-10 by yard line (yards from the offense's own goal)

Usage (repo root, .venv - it has the cfbd package):
    .venv/Scripts/python.exe src/fantasy_sports/sports/football/college/scripts/fetch_season_plays.py --years 2022 2023 2024
Idempotent: a year already cached is skipped unless --force.
"""

import argparse
import gzip
import json
import os
import re
import time
from pathlib import Path

import cfbd

ROOT = Path(__file__).resolve().parents[6]
CACHE = ROOT / "data" / "cache"
EP_CURVE_PATH = Path(__file__).resolve().parents[1] / "data" / "ep_curve.json"  # small, committed

PLAY_FIELDS = (
    "id", "game_id", "drive_id", "drive_number", "play_number", "period", "offense", "defense",
    "offense_score", "defense_score", "down", "distance", "yards_to_goal", "yards_gained",
    "play_type", "play_text", "ppa", "scoring",
)


def api_client() -> cfbd.ApiClient:
    key = os.environ.get("CFBD_API_KEY")
    if not key and (ROOT / ".env").exists():
        m = re.search(r"^CFBD_API_KEY=(.*)$", (ROOT / ".env").read_text(), re.M)
        key = m.group(1).strip() if m else None
    if not key:
        raise SystemExit("CFBD_API_KEY not set - see .env.example at the repo root.")
    return cfbd.ApiClient(cfbd.Configuration(access_token=key))


def play_row(p) -> dict:
    # attribute access, not to_dict(): to_dict() emits camelCase aliases
    row = {k: getattr(p, k, None) for k in PLAY_FIELDS}
    clock = getattr(p, "clock", None)
    row["clock_seconds"] = ((getattr(clock, "minutes", 0) or 0) * 60 + (getattr(clock, "seconds", 0) or 0))
    return row


def fetch_year(client, year: int) -> None:
    plays_api, games_api = cfbd.PlaysApi(client), cfbd.GamesApi(client)
    games = []
    for season_type in ("regular", "postseason"):
        for g in games_api.get_games(year=year, season_type=season_type, classification="fbs"):
            games.append({
                "id": g.id, "week": g.week, "season_type": season_type,
                "home_team": g.home_team, "away_team": g.away_team,
                "home_points": g.home_points, "away_points": g.away_points,
                "home_conference": g.home_conference, "away_conference": g.away_conference,
            })
    fbs_game_ids = {g["id"] for g in games}
    (CACHE / f"cfb_games_{year}.json").write_text(json.dumps(games))

    n = 0
    with gzip.open(CACHE / f"cfb_plays_{year}.jsonl.gz", "wt", encoding="utf-8") as out:
        for season_type, weeks in (("regular", range(1, 16)), ("postseason", [1])):
            for week in weeks:
                plays = plays_api.get_plays(year=year, week=week, season_type=season_type,
                                            classification="fbs")
                for p in plays:
                    if p.game_id in fbs_game_ids:
                        out.write(json.dumps(play_row(p)) + "\n")
                        n += 1
                print(f"  {year} {season_type} wk{week}: {len(plays)} plays")
                time.sleep(0.3)
    print(f"{year}: {len(games)} games, {n} plays")


def fetch_roster(client, year: int) -> None:
    """Every FBS player's position, so defenders named in play text (sacks,
    interceptions, forced fumbles, breakups) can be mapped to DL/LB/DB."""
    rows = cfbd.TeamsApi(client).get_roster(year=year)
    (CACHE / f"cfb_roster_{year}.json").write_text(json.dumps([
        {"id": x.id, "team": x.team, "first": x.first_name, "last": x.last_name, "position": x.position}
        for x in rows]))
    print(f"{year}: {len(rows)} roster rows")


def fetch_defense_stats(client, year: int) -> None:
    """Season tackles / sacks / deflections / hurries / interceptions per player
    (with position), used to weight each team's DL/LB/DB split of unnamed plays."""
    api = cfbd.StatsApi(client)
    rows = []
    for category in ("defensive", "interceptions"):
        for x in api.get_player_season_stats(year=year, category=category):
            rows.append({"player_id": x.player_id, "team": x.team, "position": x.position,
                         "category": category, "stat_type": x.stat_type, "stat": x.stat})
    (CACHE / f"cfb_defstats_{year}.json").write_text(json.dumps(rows))
    print(f"{year}: {len(rows)} defensive stat rows")


def fetch_ep_curve(client) -> None:
    """EP of a 1st-and-10 at each yards-to-goal - the value of a field position,
    used to price where a punt lands independent of who returns it."""
    api = cfbd.MetricsApi(client)
    rows = api.get_predicted_points(down=1, distance=10)
    EP_CURVE_PATH.write_text(json.dumps(
        {"source": "CFBD MetricsApi.get_predicted_points(down=1, distance=10)",
         "rows": sorted(({"yard_line": r.yard_line, "predicted_points": r.predicted_points}
                         for r in rows), key=lambda r: r["yard_line"])}))
    print(f"ep curve: {len(rows)} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    client = api_client()
    if args.force or not EP_CURVE_PATH.exists():
        fetch_ep_curve(client)
    for year in args.years:
        if (CACHE / f"cfb_plays_{year}.jsonl.gz").exists() and not args.force:
            print(f"{year}: cached, skipping")
            continue
        fetch_year(client, year)
    for year in args.years:
        if args.force or not (CACHE / f"cfb_roster_{year}.json").exists():
            fetch_roster(client, year)
        if args.force or not (CACHE / f"cfb_defstats_{year}.json").exists():
            fetch_defense_stats(client, year)


if __name__ == "__main__":
    main()
