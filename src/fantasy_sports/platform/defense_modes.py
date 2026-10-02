"""How a league drafts defense, and which assets belong in its pool.

A universe can offer up to three modes (league_universes.defense_mode):

  team        one whole-defense asset per team (DefenseGroup position_group "DEF")
  group       a team's DL / LB / DB units (DefenseGroup "DL"/"LB"/"DB")
  individual  named defenders (ordinary `players` rows with a defensive position;
              only where universe.supports_individual_defense)

All three draw from one asset table, so every pool query (snake board, auction
pool, free agency) must drop the assets that belong to the league's other
modes - otherwise a group league would also be offered "DEF" assets and
individual defenders. Offense, kickers and anything in a universe that doesn't
offer individual defense are never filtered.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import DraftableAsset, LeagueUniverse

DEFENSE_MODES = ("group", "individual", "team")

# Roster positions (nflverse/CFBD) -> the DL/LB/DB unit they play in.
POSITION_TO_GROUP = {
    "DL": "DL", "DE": "DL", "DT": "DL", "NT": "DL", "EDGE": "DL",
    "LB": "LB", "ILB": "LB", "MLB": "LB", "OLB": "LB",
    "DB": "DB", "CB": "DB", "S": "DB", "SS": "DB", "FS": "DB", "SAF": "DB",
}


def defender_group(position: str | None) -> str | None:
    return POSITION_TO_GROUP.get((position or "").upper())


def asset_defense_mode(asset: DraftableAsset) -> str | None:
    """The defense mode an asset belongs to, or None for offense/kickers (and
    for defensive players in a universe without individual defense)."""
    if asset.asset_type == "defense_group":
        return "team" if asset.defense_group.position_group == "DEF" else "group"
    if asset.player is not None and defender_group(asset.player.position):
        return "individual" if asset.universe.supports_individual_defense else None
    return None


def league_modes(db: Session, league_id: int) -> dict[int, str]:
    """universe_id -> that league's defense mode."""
    return {lu.universe_id: lu.defense_mode for lu in db.query(LeagueUniverse).filter_by(league_id=league_id)}


def in_pool(asset: DraftableAsset, modes: dict[int, str]) -> bool:
    kind = asset_defense_mode(asset)
    return kind is None or kind == modes.get(asset.universe_id, "group")


def league_pool(db: Session, league_id: int) -> list[DraftableAsset]:
    """Every draftable asset of the league's universes that matches its defense mode."""
    modes = league_modes(db, league_id)
    assets = db.query(DraftableAsset).filter(DraftableAsset.universe_id.in_(list(modes) or [0])).all()
    return [a for a in assets if in_pool(a, modes)]
