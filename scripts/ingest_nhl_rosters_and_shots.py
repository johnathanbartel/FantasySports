"""Ingest MoneyPuck NHL rosters and one season of shot-level xG data.

MoneyPuck publishes season-summary CSVs for skaters and goalies plus a ZIP
containing every unblocked shot attempt in a season. This script maps those
files into the shared platform's NHL teams, players, draftable assets, games,
and hockey ``shots`` table.

Run from the repository root with the data-pipeline environment:

    $env:DATABASE_URL = "sqlite:///./user_run.db"
    .venv/Scripts/python.exe scripts/ingest_nhl_rosters_and_shots.py --season 2026 --rosters-only

MoneyPuck names a season by its starting year, so ``--season 2026`` means the
2026-27 NHL season. The operation is idempotent: roster records are updated
only when source fields change, and shots use a stable
``season:game_id:shotID`` source key. Existing shot rows are never duplicated.

The shot download does not contain game dates. Games therefore attach to a
single week-number-0 season container. This preserves the required Game ->
Week relationship without pretending that the source supplied fantasy-week
boundaries.

MoneyPuck data is free for non-commercial and journalistic use. Other uses
require permission from MoneyPuck: https://www.moneypuck.com/data.htm
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./user_run.db")

import requests
from sqlalchemy import bindparam, insert, update

from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    Game,
    Player,
    RealTeam,
    RosterSlotDefinition,
    Universe,
    Week,
)
from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal, engine
from fantasy_sports.platform.nhl_game_type import season_type_from_nhl_game_id
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401
from seed_hockey_universe import ROSTER_SLOTS


DOWNLOAD_ROOT = "https://www.moneypuck.com/moneypuck/playerData/seasonSummary"
SHOT_URL = "https://peter-tanner.com/moneypuck/downloads/shots_{season}.zip"
REQUEST_HEADERS = {"User-Agent": "FantasySportsCompany-NHLIngest/1.0"}
POSITION_MAP = {"L": "LW", "R": "RW", "C": "C", "D": "D", "G": "G"}
TEAM_NAMES = {
    "ANA": "Anaheim Ducks",
    "ARI": "Arizona Coyotes",
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CAR": "Carolina Hurricanes",
    "CBJ": "Columbus Blue Jackets",
    "CGY": "Calgary Flames",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens",
    "NJD": "New Jersey Devils",
    "NSH": "Nashville Predators",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SEA": "Seattle Kraken",
    "SJS": "San Jose Sharks",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Hockey Club",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WPG": "Winnipeg Jets",
    "WSH": "Washington Capitals",
}
ROSTER_COLUMNS = {"playerId", "name", "team", "position", "situation"}
SHOT_COLUMNS = {
    "shotID",
    "game_id",
    "period",
    "time",
    "homeSkatersOnIce",
    "awaySkatersOnIce",
    "homeTeamCode",
    "awayTeamCode",
    "teamCode",
    "shotType",
    "xGoal",
    "goal",
    "shotOnEmptyNet",
    "shooterPlayerId",
    "goalieIdForShot",
    "homeTeamWon",
}


def _download(url: str, destination: Path) -> None:
    try:
        with requests.get(
            url,
            headers=REQUEST_HEADERS,
            stream=True,
            timeout=(15, 180),
        ) as response:
            response.raise_for_status()
            if response.url.endswith("/data_license.htm"):
                raise RuntimeError(
                    "MoneyPuck redirected the download to its data-license page; "
                    "review https://www.moneypuck.com/data.htm before retrying"
                )
            with destination.open("wb") as target:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        target.write(chunk)
    except requests.RequestException as exc:
        raise RuntimeError(f"MoneyPuck download failed: {url}: {exc}") from exc


def _read_roster(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing = ROSTER_COLUMNS - set(reader.fieldnames or ())
        if missing:
            raise RuntimeError(f"{path.name} is missing columns: {sorted(missing)}")
        rows = [row for row in reader if row["situation"] == "all"]
    if not rows:
        raise RuntimeError(f"{path.name} contains no situation=all roster rows")
    return rows


def _shot_reader(archive: zipfile.ZipFile, season: int):
    expected_name = f"shots_{season}.csv"
    names = archive.namelist()
    if expected_name not in names:
        raise RuntimeError(f"Shot ZIP does not contain {expected_name}; found {names}")
    raw = archive.open(expected_name)
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    reader = csv.DictReader(text)
    missing = SHOT_COLUMNS - set(reader.fieldnames or ())
    if missing:
        text.close()
        raise RuntimeError(f"{expected_name} is missing columns: {sorted(missing)}")
    return text, reader


def _source_files(
    season: int, directory: Path, *, rosters_only: bool = False
) -> tuple[Path, Path, Path | None]:
    skaters = directory / f"skaters_{season}.csv"
    goalies = directory / f"goalies_{season}.csv"
    shots = directory / f"shots_{season}.zip"
    _download(f"{DOWNLOAD_ROOT}/{season}/regular/skaters.csv", skaters)
    _download(f"{DOWNLOAD_ROOT}/{season}/regular/goalies.csv", goalies)
    if not rosters_only:
        _download(SHOT_URL.format(season=season), shots)
    else:
        shots = None
    return skaters, goalies, shots


def _seed_reference_data(db) -> tuple[Universe, int]:
    universe = db.query(Universe).filter_by(code="nhl").one_or_none()
    if universe is None:
        universe = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
        db.add(universe)
        db.flush()

    existing_slots = {
        (slot.slot_code, slot.defense_mode)
        for slot in db.query(RosterSlotDefinition).filter_by(universe_id=universe.id)
    }
    slots_created = 0
    for slot_code, eligible, count, is_starter in ROSTER_SLOTS:
        if (slot_code, None) in existing_slots:
            continue
        db.add(RosterSlotDefinition(
            universe_id=universe.id,
            slot_code=slot_code,
            eligible_positions=eligible,
            slot_count=count,
            is_starter=is_starter,
        ))
        slots_created += 1
    db.flush()
    return universe, slots_created


def _team_codes(rosters: list[dict[str, str]], shots_path: Path, season: int) -> set[str]:
    codes = {row["team"].strip() for row in rosters if row["team"].strip()}
    with zipfile.ZipFile(shots_path) as archive:
        text, shots = _shot_reader(archive, season)
        try:
            for row in shots:
                codes.add(row["homeTeamCode"].strip())
                codes.add(row["awayTeamCode"].strip())
        finally:
            text.close()
    codes.discard("")
    unknown = codes - set(TEAM_NAMES)
    if unknown:
        raise RuntimeError(f"Unrecognized MoneyPuck team code(s): {sorted(unknown)}")
    return codes


def _sync_teams(db, universe: Universe, codes: set[str]) -> tuple[dict[str, RealTeam], int, int]:
    existing = {
        team.external_ref: team
        for team in db.query(RealTeam).filter_by(universe_id=universe.id)
        if team.external_ref
    }
    teams: dict[str, RealTeam] = {}
    created = updated = 0
    for code in sorted(codes):
        team = existing.get(code)
        if team is None:
            team = RealTeam(universe_id=universe.id, external_ref=code, name=TEAM_NAMES[code])
            db.add(team)
            created += 1
        else:
            values = {"name": TEAM_NAMES[code], "classification": "nhl"}
            if any(getattr(team, field) != value for field, value in values.items()):
                updated += 1
            team.name = values["name"]
        team.classification = "nhl"
        teams[code] = team
    db.flush()
    return teams, created, updated


def _sync_players(db, universe: Universe, teams, rosters) -> dict[str, int]:
    players = {
        player.external_ref: player
        for player in db.query(Player).filter_by(universe_id=universe.id)
        if player.external_ref
    }
    asset_player_ids = {
        player_id for (player_id,) in db.query(DraftableAsset.player_id).filter_by(
            universe_id=universe.id,
            asset_type="player",
        ) if player_id is not None
    }
    rostered_player_ids = {
        r.draftable_asset.player_id
        for r in db.query(RosterEntry).join(DraftableAsset, DraftableAsset.id == RosterEntry.draftable_asset_id).filter(
            DraftableAsset.player_id.isnot(None)
        ).all()
    }
    deactivated = 0
    for player_ref, player in players.items():
        if player_ref not in current_player_refs and player.active:
            if player.id not in rostered_player_ids and player.real_team_id is None:
                player.active = False
                deactivated += 1

    created = updated = assets_created = 0
    for row in rosters:
        player_ref = row["playerId"].strip()
        source_position = row["position"].strip().upper()
        if source_position not in POSITION_MAP:
            raise RuntimeError(
                f"Unrecognized MoneyPuck position {source_position!r} for player {player_ref}"
            )
        values = {
            "full_name": row["name"].strip(),
            "position": POSITION_MAP[source_position],
            "real_team_id": teams[row["team"].strip()].id,
            "active": True,
        }
        player = players.get(player_ref)
        if player is None:
            player = Player(universe_id=universe.id, external_ref=player_ref, **values)
            db.add(player)
            db.flush()
            players[player_ref] = player
            created += 1
        else:
            if any(getattr(player, field) != value for field, value in values.items()):
                updated += 1
            for field, value in values.items():
                setattr(player, field, value)
        if player.id not in asset_player_ids:
            db.add(DraftableAsset(
                universe_id=universe.id,
                asset_type="player",
                player_id=player.id,
            ))
            asset_player_ids.add(player.id)
            assets_created += 1
    db.flush()
    return {
        "players": len(rosters),
        "players_created": created,
        "players_updated": updated,
        "players_deactivated": deactivated,
        "assets_created": assets_created,
    }


def _season_week(db, universe: Universe, season: int) -> tuple[Week, int]:
    week = db.query(Week).filter_by(
        universe_id=universe.id,
        season_year=season,
        week_number=0,
    ).one_or_none()
    if week is not None:
        return week, 0
    week = Week(universe_id=universe.id, season_year=season, week_number=0, is_playoffs=False)
    db.add(week)
    db.flush()
    return week, 1


def _existing_shots_and_games(db, season: int) -> tuple[set[str], dict[str, int]]:
    rows = db.query(Shot.external_shot_id, Shot.game_id).filter(
        Shot.source == "moneypuck",
        Shot.external_shot_id.like(f"{season}:%"),
    )
    shot_ids: set[str] = set()
    games: dict[str, int] = {}
    for external_shot_id, game_id in rows:
        parts = external_shot_id.split(":", 2)
        if len(parts) != 3:
            raise RuntimeError(f"Malformed MoneyPuck shot key in database: {external_shot_id}")
        shot_ids.add(external_shot_id)
        games.setdefault(parts[1], game_id)
    return shot_ids, games


def _bool(value: str) -> bool:
    return value.strip() in {"1", "1.0", "true", "True"}


def _optional_int(value: str) -> int | None:
    value = value.strip()
    return int(float(value)) if value else None


def _optional_float(value: str) -> float | None:
    value = value.strip()
    return float(value) if value else None


def _strength_state(row: dict[str, str]) -> str | None:
    home = _optional_int(row["homeSkatersOnIce"])
    away = _optional_int(row["awaySkatersOnIce"])
    if home is None or away is None:
        return None
    if row["teamCode"] == row["homeTeamCode"]:
        return f"{home}v{away}"
    return f"{away}v{home}"


def _asset_by_player_ref(db, universe: Universe) -> dict[str, int]:
    """MoneyPuck playerId -> draftable asset id, for the universe's players."""
    rows = db.query(Player.external_ref, DraftableAsset.id).join(
        DraftableAsset, DraftableAsset.player_id == Player.id
    ).filter(
        Player.universe_id == universe.id,
        DraftableAsset.asset_type == "player",
        Player.external_ref.isnot(None),
    )
    return {ref: asset_id for ref, asset_id in rows}


def _sync_shots(db, universe: Universe, teams, week: Week, shots_path: Path, season: int):
    existing_shot_ids, games = _existing_shots_and_games(db, season)
    assets = _asset_by_player_ref(db, universe)
    needs_players = {
        external_shot_id: shot_id
        for external_shot_id, shot_id in db.query(Shot.external_shot_id, Shot.id).filter(
            Shot.source == "moneypuck",
            Shot.external_shot_id.like(f"{season}:%"),
            Shot.shooter_asset_id.is_(None),
        )
    }
    games_created = shots_created = source_rows = 0
    backfilled = unknown_players = 0
    goals: dict[str, list[int]] = {}  # game ref -> [home goals, away goals]
    home_won: dict[str, bool] = {}
    pending: list[dict] = []
    backfill: list[dict] = []

    def player_asset(ref: str) -> int | None:
        nonlocal unknown_players
        ref = ref.strip()
        if not ref or ref in {"0", "0.0"}:
            return None
        asset_id = assets.get(ref)
        if asset_id is None:
            unknown_players += 1
        return asset_id

    with zipfile.ZipFile(shots_path) as archive:
        text, shots = _shot_reader(archive, season)
        try:
            for row in shots:
                source_rows += 1
                game_ref = row["game_id"].strip()
                shot_ref = row["shotID"].strip()
                if not game_ref or not shot_ref:
                    raise RuntimeError(f"MoneyPuck row {source_rows} has no game_id or shotID")
                external_shot_id = f"{season}:{game_ref}:{shot_ref}"
                home_code = row["homeTeamCode"].strip()
                away_code = row["awayTeamCode"].strip()
                shooting_code = row["teamCode"].strip()

                tally = goals.setdefault(game_ref, [0, 0])
                home_won[game_ref] = row["homeTeamWon"].strip() in {"1", "1.0"}
                if _bool(row["goal"]):
                    tally[0 if shooting_code == home_code else 1] += 1

                if external_shot_id in existing_shot_ids:
                    shot_id = needs_players.get(external_shot_id)
                    if shot_id is not None:
                        backfill.append({
                            "b_id": shot_id,
                            "shooter": player_asset(row["shooterPlayerId"]),
                            "goalie": player_asset(row["goalieIdForShot"]),
                        })
                        if len(backfill) == 5000:
                            backfilled += _flush_backfill(db, backfill)
                    continue

                game_db_id = games.get(game_ref)
                if game_db_id is None:
                    game = Game(
                        universe_id=universe.id,
                        week_id=week.id,
                        home_team_id=teams[home_code].id,
                        away_team_id=teams[away_code].id,
                        status="final",
                        season_type=season_type_from_nhl_game_id(game_ref),
                    )
                    db.add(game)
                    db.flush()
                    game_db_id = game.id
                    games[game_ref] = game_db_id
                    games_created += 1

                if shooting_code not in {home_code, away_code}:
                    raise RuntimeError(
                        f"Shot {external_shot_id} has shooting team {shooting_code!r} "
                        f"outside game teams {away_code!r}/{home_code!r}"
                    )
                defending_code = away_code if shooting_code == home_code else home_code
                pending.append({
                    "game_id": game_db_id,
                    "source": "moneypuck",
                    "external_shot_id": external_shot_id,
                    "period": _optional_int(row["period"]),
                    "clock": row["time"].strip() or None,
                    "strength_state": _strength_state(row),
                    "shot_type": row["shotType"].strip().lower() or None,
                    "shooting_team_id": teams[shooting_code].id,
                    "goalie_team_id": teams[defending_code].id,
                    "shooter_asset_id": player_asset(row["shooterPlayerId"]),
                    "goalie_asset_id": player_asset(row["goalieIdForShot"]),
                    "pre_shot_xg": _optional_float(row["xGoal"]),
                    "is_goal": _bool(row["goal"]),
                    "is_empty_net": _bool(row["shotOnEmptyNet"]),
                })
                existing_shot_ids.add(external_shot_id)
                if len(pending) == 2000:
                    db.execute(insert(Shot), pending)
                    shots_created += len(pending)
                    pending.clear()
            if pending:
                db.execute(insert(Shot), pending)
                shots_created += len(pending)
            if backfill:
                backfilled += _flush_backfill(db, backfill)
        finally:
            text.close()

    scores_set = _set_game_scores(db, games, goals, home_won)
    return {
        "source_shots": source_rows,
        "shots_created": shots_created,
        "shots_backfilled": backfilled,
        "unknown_players": unknown_players,
        "games": len(games),
        "games_created": games_created,
        "scores_set": scores_set,
    }


def _flush_backfill(db, rows: list[dict]) -> int:
    statement = update(Shot).where(Shot.id == bindparam("b_id")).values(
        shooter_asset_id=bindparam("shooter"),
        goalie_asset_id=bindparam("goalie"),
    )
    db.connection().execute(statement, rows)
    count = len(rows)
    rows.clear()
    return count


def _set_game_scores(db, games: dict[str, int], goals, home_won) -> int:
    """Final score = shot-level goals (regulation + overtime), plus one for the
    winner of a shootout. MoneyPuck carries no shootout shots, but a game tied
    on shot goals can only have ended in a shootout, and homeTeamWon names the
    winner. Playoff games cannot tie, so they never get the +1."""
    changed = 0
    for game_ref, game_db_id in games.items():
        home, away = goals.get(game_ref, (0, 0))
        if home == away:
            if home_won.get(game_ref):
                home += 1
            else:
                away += 1
        game = db.get(Game, game_db_id)
        if game.home_score != home or game.away_score != away:
            game.home_score, game.away_score = home, away
            changed += 1
    db.flush()
    return changed


def ingest(season: int, *, rosters_only: bool = False) -> dict[str, int]:
    Base.metadata.create_all(bind=engine)
    with tempfile.TemporaryDirectory(prefix="nhl-moneypuck-") as temporary:
        skaters_path, goalies_path, shots_path = _source_files(
            season, Path(temporary), rosters_only=rosters_only
        )
        rosters = _read_roster(skaters_path) + _read_roster(goalies_path)
        player_ids = [row["playerId"] for row in rosters]
        if len(player_ids) != len(set(player_ids)):
            raise RuntimeError(f"MoneyPuck {season} roster contains duplicate player ids")
        codes = (
            {row["team"].strip() for row in rosters if row["team"].strip()}
            if shots_path is None
            else _team_codes(rosters, shots_path, season)
        )
        unknown = codes - set(TEAM_NAMES)
        if unknown:
            raise RuntimeError(f"Unrecognized MoneyPuck team code(s): {sorted(unknown)}")

        with SessionLocal() as db:
            universe, slots_created = _seed_reference_data(db)
            teams, teams_created, teams_updated = _sync_teams(db, universe, codes)
            result = _sync_players(db, universe, teams, rosters)
            weeks_created = 0
            if shots_path is not None:
                week, weeks_created = _season_week(db, universe, season)
                result.update(_sync_shots(db, universe, teams, week, shots_path, season))
            result.update({
                "slots_created": slots_created,
                "teams": len(teams),
                "teams_created": teams_created,
                "teams_updated": teams_updated,
                "weeks_created": weeks_created,
            })
            db.commit()
            return result


def main() -> None:
    today = date.today()
    current_season = today.year if today.month >= 10 else today.year - 1
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--season",
        type=int,
        default=current_season,
        help=(
            "NHL season starting year (2026 means 2026-27); defaults to the "
            "current NHL season"
        ),
    )
    parser.add_argument(
        "--rosters-only",
        action="store_true",
        help="Refresh teams, players, and draftable assets without downloading shot data",
    )
    args = parser.parse_args()
    if args.season < 2007:
        parser.error("MoneyPuck shot data begins with the 2007-08 season")

    print(f"Database: {DATABASE_URL}")
    print(f"Source: MoneyPuck {args.season}-{str(args.season + 1)[-2:]}")
    result = ingest(args.season, rosters_only=args.rosters_only)
    print(
        f"Rosters: {result['teams']} teams "
        f"({result['teams_created']} new, {result['teams_updated']} updated), "
        f"{result['players']} players "
        f"({result['players_created']} new, {result['players_updated']} updated), "
        f"{result['players_deactivated']} no longer current, "
        f"{result['assets_created']} new draftable assets"
    )
    if not args.rosters_only:
        print(
            f"Shots: {result['source_shots']} source rows, "
            f"{result['shots_created']} new shots, "
            f"{result['shots_backfilled']} backfilled with players "
            f"({result['unknown_players']} player ids not in the roster files); "
            f"{result['games']} games ({result['games_created']} new, "
            f"{result['scores_set']} scores set or changed)"
        )
    print(
        f"Reference data: {result['slots_created']} new roster slots, "
        f"{result['weeks_created']} new season containers"
    )


if __name__ == "__main__":
    main()
