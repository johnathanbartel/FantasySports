from fantasy_sports.platform.db.models.core import DraftableAsset
from fantasy_sports.platform.defense_modes import defender_group


def draftable_asset_display_name(asset: DraftableAsset) -> str:
    if asset.asset_type == "player":
        return asset.player.full_name
    real_team_name = asset.defense_group.real_team.name if asset.defense_group.real_team else "?"
    return f"{real_team_name} {asset.defense_group.position_group}"


def draftable_asset_position(asset: DraftableAsset) -> str | None:
    if asset.asset_type == "player":
        # an individual defender (CB, DE, OLB...) fills the DL/LB/DB slots by his unit
        return defender_group(asset.player.position) or asset.player.position
    return asset.defense_group.position_group
