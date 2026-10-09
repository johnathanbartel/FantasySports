"""Ingest NHL play-by-play events (assists, hits, blocks, giveaways, takeaways,
faceoffs, shot attempts) for games already loaded by
ingest_nhl_rosters_and_shots.py.

Source: the NHL's public play-by-play endpoint (api-web.nhle.com, no key).
MoneyPuck game id 20001 of season 2024 is NHL game 2024020001
(season * 1_000_000 + id); playoff ids follow the same rule (30111 ->
2024030111).

Run from the repository root with the data-pipeline environment:

    $env:DATABASE_URL = "sqlite:///./copy_of_user_run.db"
    .venv/Scripts/python.exe scripts/ingest_nhl_events.py --season 2024

Idempotent and resumable: events are keyed ``season:nhl_game_id:eventId``
and games that already have events are skipped, so an interrupted run just
continues. Requests are sequential with a delay and retries. ``--limit N``
fetches at most N games (for trials); ``--cache-dir`` keeps the raw JSON so a
rerun never refetches.

Each game's NHL-recorded final score is compared with the score stored from
the MoneyPuck shot file (an independent scoreboard check); mismatches are
listed, never silently changed.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./user_run.db")

import requests
from sqlalchemy import insert

from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import DraftableAsset, Game, Player, RealTeam
from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal, engine
from fantasy_sports.sports.hockey.models import GameEvent, Shot

URL = "https://api-web.nhle.com/v1/gamecenter/{game}/play-by-play"
HEADERS = {"User-Agent": "FantasySportsCompany-NHLEvents/1.0"}
EVENT_TYPES = {
    "goal", "shot-on-goal", "missed-shot", "blocked-shot",
    "hit", "giveaway", "takeaway", "faceoff",
}
SOURCE = "nhl_api"


def _fetch(nhl_game_id: int, cache_dir: Path | None, delay: float) -> dict:
    cached = cache_dir / f"{nhl_game_id}.json" if cache_dir else None
    if cached and cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    last = None
    for attempt in range(5):
        try:
            response = requests.get(URL.format(game=nhl_game_id), headers=HEADERS, timeout=(15, 60))
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.RequestException(f"HTTP {response.status_code}")
            response.raise_for_status()
            data = response.json()
            if cached:
                cached.write_text(json.dumps(data), encoding="utf-8")
            time.sleep(delay)
            return data
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"NHL play-by-play fetch failed for {nhl_game_id}: {last}")


def _seconds(period: int, clock: str) -> int:
    minutes, seconds = clock.split(":")
    return (period - 1) * 1200 + int(minutes) * 60 + int(seconds)


def _ref(value) -> str | None:
    return str(value) if value else None


def _event_rows(data: dict, season: int, game_db_id: int, game: Game, assets: dict[str, int]):
    nhl_game_id = data["id"]
    team_by_nhl = {
        data["homeTeam"]["id"]: game.home_team_id,
        data["awayTeam"]["id"]: game.away_team_id,
    }
    rows = []
    unmapped_players = set()

    def asset(ref):
        if ref and ref not in assets:
            unmapped_players.add(ref)
        return assets.get(ref) if ref else None

    for play in data["plays"]:
        kind = play["typeDescKey"]
        if kind not in EVENT_TYPES:
            continue
        if play["periodDescriptor"].get("periodType") == "SO":
            continue  # shootout attempts are not game goals and have no MoneyPuck shot rows
        d = play.get("details", {})
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
        else:  # giveaway / takeaway
            refs = (d.get("playerId"), None, None)
        refs = tuple(_ref(r) for r in refs)
        period = play["periodDescriptor"]["number"]
        rows.append({
            "game_id": game_db_id,
            "source": SOURCE,
            "external_event_id": f"{season}:{nhl_game_id}:{play['eventId']}",
            "event_type": kind,
            "period": period,
            "game_seconds": _seconds(period, play["timeInPeriod"]),
            "team_id": team_by_nhl.get(d.get("eventOwnerTeamId")),
            "actor_ref": refs[0], "secondary_ref": refs[1], "tertiary_ref": refs[2],
            "actor_asset_id": asset(refs[0]),
            "secondary_asset_id": asset(refs[1]),
            "tertiary_asset_id": asset(refs[2]),
            "x_coord": d.get("xCoord"),
            "y_coord": d.get("yCoord"),
            "zone_code": d.get("zoneCode"),
            "shot_type": d.get("shotType"),
        })
    return rows, unmapped_players


def ingest(season: int, limit: int | None, cache_dir: Path | None, delay: float, workers: int = 1) -> dict:
    Base.metadata.create_all(bind=engine)
    if workers > 1 and cache_dir is None:
        cache_dir = Path(tempfile.mkdtemp(prefix="nhl-events-"))
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    stats = {"games_fetched": 0, "games_skipped": 0, "events": 0, "unmapped_players": set(),
             "score_mismatches": [], "team_mismatches": []}
    with SessionLocal() as db:
        refs: dict[str, int] = {}
        for external_shot_id, game_id in db.query(Shot.external_shot_id, Shot.game_id).filter(
            Shot.source == "moneypuck", Shot.external_shot_id.like(f"{season}:%")
        ).distinct():
            refs.setdefault(external_shot_id.split(":")[1], game_id)
        done = {g for (g,) in db.query(GameEvent.game_id).filter(GameEvent.source == SOURCE).distinct()}
        assets = {
            ref: asset_id
            for ref, asset_id in db.query(Player.external_ref, DraftableAsset.id).join(
                DraftableAsset, DraftableAsset.player_id == Player.id
            ).filter(DraftableAsset.asset_type == "player", Player.external_ref.isnot(None))
        }
        codes = {t.id: t.external_ref for t in db.query(RealTeam)}

        todo = [r for r in sorted(refs, key=int) if refs[r] not in done]
        if limit is not None:
            todo = todo[:limit]
        if workers > 1:
            # Fetch-only parallelism (the cache makes the loop below read from disk).
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda r: _fetch(season * 1_000_000 + int(r), cache_dir, delay), todo))

        fetched = 0
        for ref in sorted(refs, key=int):
            game_db_id = refs[ref]
            if game_db_id in done:
                stats["games_skipped"] += 1
                continue
            if limit is not None and fetched >= limit:
                break
            game = db.get(Game, game_db_id)
            data = _fetch(season * 1_000_000 + int(ref), cache_dir, delay)
            fetched += 1
            for side, team_id in (("homeTeam", game.home_team_id), ("awayTeam", game.away_team_id)):
                if data[side]["abbrev"] != codes.get(team_id):
                    stats["team_mismatches"].append((ref, side, data[side]["abbrev"], codes.get(team_id)))
            rows, unmapped = _event_rows(data, season, game_db_id, game, assets)
            stats["unmapped_players"] |= unmapped
            if rows:
                db.execute(insert(GameEvent), rows)
            stats["games_fetched"] += 1
            stats["events"] += len(rows)
            nhl_score = (data["homeTeam"].get("score"), data["awayTeam"].get("score"))
            if nhl_score != (game.home_score, game.away_score):
                stats["score_mismatches"].append((ref, nhl_score, (game.home_score, game.away_score)))
            if fetched % 50 == 0:
                db.commit()
                print(f"  {fetched} games...", flush=True)
        db.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=2024)
    parser.add_argument("--limit", type=int, default=None, help="fetch at most N new games")
    parser.add_argument("--cache-dir", type=Path, default=None, help="keep raw play-by-play JSON here")
    parser.add_argument("--delay", type=float, default=0.4, help="seconds between requests (per worker)")
    parser.add_argument("--workers", type=int, default=1, help="parallel fetch workers (DB writes stay sequential)")
    args = parser.parse_args()

    print(f"Database: {DATABASE_URL}")
    s = ingest(args.season, args.limit, args.cache_dir, args.delay, args.workers)
    print(f"Games: {s['games_fetched']} fetched, {s['games_skipped']} already had events")
    print(f"Events inserted: {s['events']}")
    print(f"Players in events but not on the roster files: {len(s['unmapped_players'])}")
    print(f"Team code mismatches vs NHL: {len(s['team_mismatches'])} {s['team_mismatches'][:5]}")
    print(f"Score mismatches vs NHL final: {len(s['score_mismatches'])}")
    for ref, nhl, ours in s["score_mismatches"][:20]:
        print(f"  game {ref}: NHL home/away {nhl} vs stored {ours}")


if __name__ == "__main__":
    main()
