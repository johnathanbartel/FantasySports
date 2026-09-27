"""hockey_shots_and_shot_credits

Revision ID: c3d8e1a5f9b2
Revises: a7d5f1c9e3b4
Create Date: 2026-09-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d8e1a5f9b2'
down_revision: Union[str, Sequence[str], None] = 'a7d5f1c9e3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('shots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('game_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('external_shot_id', sa.String(length=60), nullable=False),
    sa.Column('period', sa.Integer(), nullable=True),
    sa.Column('clock', sa.String(length=10), nullable=True),
    sa.Column('strength_state', sa.String(length=10), nullable=True),
    sa.Column('shot_type', sa.String(length=30), nullable=True),
    sa.Column('shooting_team_id', sa.Integer(), nullable=True),
    sa.Column('goalie_team_id', sa.Integer(), nullable=True),
    sa.Column('pre_shot_xg', sa.Float(), nullable=True),
    sa.Column('is_goal', sa.Boolean(), nullable=False),
    sa.Column('is_empty_net', sa.Boolean(), nullable=False),
    sa.Column('ingested_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['game_id'], ['games.id'], ),
    sa.ForeignKeyConstraint(['goalie_team_id'], ['real_teams.id'], ),
    sa.ForeignKeyConstraint(['shooting_team_id'], ['real_teams.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'external_shot_id')
    )
    op.create_table('shot_credits',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shot_id', sa.Integer(), nullable=False),
    sa.Column('draftable_asset_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('credit_points', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['draftable_asset_id'], ['draftable_assets.id'], ),
    sa.ForeignKeyConstraint(['shot_id'], ['shots.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('shot_credits')
    op.drop_table('shots')
