"""Ingest an NFL roster snapshot and one week of nflverse player statistics.

The NFL specification selects nflfastR/nflverse as the professional-football
source.  ``nfl_data_py`` (already installed in the data-pipeline environment)
downloads the corresponding roster and weekly player-stat parquet releases.

Weekly source statistics are stored as a traceable snapshot in
``player_season_points``.  The row's scoring system includes the source week,
and ``breakdown`` preserves the raw stat fields.  These rows intentionally do
not populate ``weekly_player_scores``: that table is reserved for the app's
EPA attribution pipeline, which is separate finish-line work.

Run from the repository root with the data-pipeline environment:

    $env:DATABASE_URL = "sqlite:///./user_run.db"
    .venv/Scripts/python.exe scripts/ingest_nfl_rosters_and_stats.py --season 2024 --week 1

The operation is idempotent. Existing NFL teams, players, draftable assets,
defense groups, and the selected stat snapshot are updated in place.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./user_run.db")

import nfl_data_py as nfl
import pandas as pd

from fantasy_sports.platform.db.models.core import (
    DefenseGroup,
    DraftableAsset,
    Player,
    RealTeam,
    Universe,
    Week,
)
from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal
from fantasy_sports.sports.football.models import PlayerSeasonPoints
from seed_nfl_universe import seed_nfl_reference_data


TEAM_NAMES = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "PK"}
ACTIVE_STATUSES = {"ACT", "DEV", "INA", "RES"}
DEFENSE_GROUPS = ("DL", "LB", "DB")
STAT_FIELDS = (
    "completions",
    "attempts",
    "passing_yards",
    "passing_tds",
    "interceptions",
    "sacks",
    "sack_yards",
    "passing_air_yards",
    "passing_yards_after_catch",
    "passing_first_downs",
    "passing_epa",
    "passing_2pt_conversions",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "rushing_first_downs",
    "rushing_epa",
    "rushing_2pt_conversions",
    "receptions",
    "targets",
    "receiving_yards",
    "receiving_tds",
    "receiving_air_yards",
    "receiving_yards_after_catch",
    "receiving_first_downs",
    "receiving_epa",
    "receiving_2pt_conversions",
    "special_teams_tds",
    "fantasy_points",
    "fantasy_points_ppr",
)


def _plain(value):
    """Convert pandas/numpy scalar values into JSON-safe Python values."""
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value


def _source_data(season: int, week: int):
    rosters = nfl.import_seasonal_rosters([season])
    weekly = nfl.import_weekly_data([season], downcast=False)
    weekly = weekly[
        (weekly["season"] == season)
        & (weekly["week"] == week)
        & (weekly["season_type"] == "REG")
    ].copy()
    if rosters.empty:
        raise RuntimeError(f"nflverse returned no {season} roster rows")
    if weekly.empty:
        raise RuntimeError(f"nflverse returned no {season} regular-season week {week} stats")
    if rosters["player_id"].duplicated().any():
        raise RuntimeError(f"nflverse {season} seasonal roster contains duplicate player ids")
    if weekly["player_id"].duplicated().any():
        raise RuntimeError(f"nflverse {season} week {week} stats contain duplicate player ids")
    unknown_teams = set(rosters["team"].dropna()) - set(TEAM_NAMES)
    if unknown_teams:
        raise RuntimeError(f"Unrecognized nflverse team abbreviation(s): {sorted(unknown_teams)}")
    return rosters, weekly


def _sync_teams(db, universe: Universe, rosters) -> tuple[dict[str, RealTeam], int]:
    existing = {
        team.external_ref: team
        for team in db.query(RealTeam).filter_by(universe_id=universe.id)
        if team.external_ref
    }
    teams: dict[str, RealTeam] = {}
    created = 0
    for code in sorted(set(rosters["team"].dropna())):
        team = existing.get(code)
        if team is None:
            team = RealTeam(universe_id=universe.id, external_ref=code)
            db.add(team)
            created += 1
        team.name = TEAM_NAMES[code]
        team.classification = "nfl"
        team.conference = "AFC" if code in {
            "BAL", "BUF", "CIN", "CLE", "DEN", "HOU", "IND", "JAX",
            "KC", "LAC", "LV", "MIA", "NE", "NYJ", "PIT", "TEN",
        } else "NFC"
        teams[code] = team
    db.flush()
    return teams, created


def _sync_players_and_assets(db, universe: Universe, teams, rosters) -> dict[str, int]:
    players = {
        player.external_ref: player
        for player in db.query(Player).filter_by(universe_id=universe.id)
        if player.external_ref
    }
    player_asset_ids = {
        asset.player_id
        for asset in db.query(DraftableAsset).filter_by(universe_id=universe.id)
        if asset.player_id is not None
    }
    for player in players.values():
        player.active = False

    created = assets_created = active = 0
    for row in rosters.itertuples(index=False):
        ref = str(row.player_id)
        full_name = str(row.player_name).strip()
        position = str(row.position).strip().upper()
        team_id = teams[str(row.team)].id
        is_active = str(row.status).upper() in ACTIVE_STATUSES
        player = players.get(ref)
        if player is None:
            player = Player(
                universe_id=universe.id,
                external_ref=ref,
                full_name=full_name,
                position=position,
                real_team_id=team_id,
                active=is_active,
            )
            db.add(player)
            db.flush()
            players[ref] = player
            created += 1
        else:
            player.full_name = full_name
            player.position = position
            player.real_team_id = team_id
            player.active = is_active
        active += int(player.active)
        if player.position in FANTASY_POSITIONS and player.id not in player_asset_ids:
            db.add(DraftableAsset(
                universe_id=universe.id,
                asset_type="player",
                player_id=player.id,
            ))
            player_asset_ids.add(player.id)
            assets_created += 1
    db.flush()
    return {
        "players": len(rosters),
        "players_created": created,
        "players_active": active,
        "player_assets_created": assets_created,
    }


def _sync_defense_groups(db, universe: Universe, teams) -> int:
    groups = {
        (group.real_team_id, group.position_group): group
        for group in db.query(DefenseGroup).join(RealTeam).filter(RealTeam.universe_id == universe.id)
    }
    group_asset_ids = {
        asset.defense_group_id
        for asset in db.query(DraftableAsset).filter_by(universe_id=universe.id)
        if asset.defense_group_id is not None
    }
    created = 0
    for team in teams.values():
        for position in DEFENSE_GROUPS:
            group = groups.get((team.id, position))
            if group is None:
                group = DefenseGroup(real_team_id=team.id, position_group=position)
                db.add(group)
                db.flush()
            if group.id not in group_asset_ids:
                db.add(DraftableAsset(
                    universe_id=universe.id,
                    asset_type="defense_group",
                    defense_group_id=group.id,
                ))
                group_asset_ids.add(group.id)
                created += 1
    return created


def _sync_weekly_stats(db, universe: Universe, season: int, week: int, weekly) -> tuple[int, int]:
    week_row = db.query(Week).filter_by(
        universe_id=universe.id,
        season_year=season,
        week_number=week,
    ).one_or_none()
    if week_row is None:
        db.add(Week(
            universe_id=universe.id,
            season_year=season,
            week_number=week,
            is_playoffs=False,
        ))

    players = {
        player.external_ref: player
        for player in db.query(Player).filter_by(universe_id=universe.id)
        if player.external_ref
    }
    scoring_system = f"nflverse_weekly_w{week:02d}"
    existing = {
        row.player_id: row
        for row in db.query(PlayerSeasonPoints).filter_by(
            season=season,
            scoring_system=scoring_system,
        )
        if row.player_id in {player.id for player in players.values()}
    }
    seen: set[int] = set()
    matched = created = 0
    for source in weekly.itertuples(index=False):
        player = players.get(str(source.player_id))
        if player is None:
            continue
        breakdown = {
            "source": "nflverse player_stats",
            "week": week,
            "season_type": str(source.season_type),
            "opponent_team": _plain(source.opponent_team),
            "recent_team": _plain(source.recent_team),
            "position": _plain(source.position),
        }
        breakdown.update({field: _plain(getattr(source, field)) for field in STAT_FIELDS})
        points = _plain(source.fantasy_points)
        stat = existing.get(player.id)
        if stat is None:
            stat = PlayerSeasonPoints(
                player_id=player.id,
                season=season,
                scoring_system=scoring_system,
            )
            db.add(stat)
            created += 1
        stat.points = float(points or 0.0)
        stat.breakdown = breakdown
        seen.add(player.id)
        matched += 1
    for player_id, stale in existing.items():
        if player_id not in seen:
            db.delete(stale)
    return matched, created


def ingest(season: int, week: int) -> dict[str, int]:
    rosters, weekly = _source_data(season, week)
    with SessionLocal() as db:
        universe, slots_created = seed_nfl_reference_data(db)
        teams, teams_created = _sync_teams(db, universe, rosters)
        result = _sync_players_and_assets(db, universe, teams, rosters)
        result["defense_assets_created"] = _sync_defense_groups(db, universe, teams)
        stats, stats_created = _sync_weekly_stats(db, universe, season, week, weekly)
        db.commit()
        result.update({
            "slots_created": slots_created,
            "teams": len(teams),
            "teams_created": teams_created,
            "weekly_source_rows": len(weekly),
            "weekly_stats": stats,
            "weekly_stats_created": stats_created,
        })
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=2024)
    parser.add_argument("--week", type=int, default=1)
    args = parser.parse_args()
    if args.week < 1:
        parser.error("--week must be positive")

    print(f"Database: {DATABASE_URL}")
    result = ingest(args.season, args.week)
    print(
        f"NFL {args.season} rosters: {result['teams']} teams "
        f"({result['teams_created']} new), {result['players']} players "
        f"({result['players_created']} new, {result['players_active']} active), "
        f"{result['player_assets_created']} new player assets, "
        f"{result['defense_assets_created']} new defense-group assets"
    )
    print(
        f"NFL {args.season} week {args.week}: {result['weekly_stats']} of "
        f"{result['weekly_source_rows']} nflverse stat rows matched roster players "
        f"({result['weekly_stats_created']} new snapshot rows)"
    )


if __name__ == "__main__":
    main()
