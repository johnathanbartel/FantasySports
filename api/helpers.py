from db.models.core import DraftableAsset


def draftable_asset_display_name(asset: DraftableAsset) -> str:
    if asset.asset_type == "player":
        return asset.player.full_name
    real_team_name = asset.defense_group.real_team.name if asset.defense_group.real_team else "?"
    return f"{real_team_name} {asset.defense_group.position_group}"


def draftable_asset_position(asset: DraftableAsset) -> str | None:
    if asset.asset_type == "player":
        return asset.player.position
    return asset.defense_group.position_group
