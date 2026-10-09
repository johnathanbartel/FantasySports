"""shot_player_ids

Adds the shooter and goalie draftable assets to hockey shots so xG credit
can be attributed to players (FS-014).

Revision ID: a9b4d2e7c5f1
Revises: f1a9c4d7e2b6
Create Date: 2026-10-09 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9b4d2e7c5f1'
down_revision: Union[str, Sequence[str], None] = 'f1a9c4d7e2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("shots") as batch_op:
        batch_op.add_column(sa.Column('shooter_asset_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('goalie_asset_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_shots_shooter_asset_id', 'draftable_assets', ['shooter_asset_id'], ['id'])
        batch_op.create_foreign_key('fk_shots_goalie_asset_id', 'draftable_assets', ['goalie_asset_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("shots") as batch_op:
        batch_op.drop_constraint('fk_shots_goalie_asset_id', type_='foreignkey')
        batch_op.drop_constraint('fk_shots_shooter_asset_id', type_='foreignkey')
        batch_op.drop_column('goalie_asset_id')
        batch_op.drop_column('shooter_asset_id')
