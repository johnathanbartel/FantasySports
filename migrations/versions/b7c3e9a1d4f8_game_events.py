"""game_events

NHL play-by-play events (assists, hits, blocks, giveaways, takeaways,
faceoffs) for the FS-015 credit pipeline.

Revision ID: b7c3e9a1d4f8
Revises: a9b4d2e7c5f1
Create Date: 2026-10-09 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c3e9a1d4f8'
down_revision: Union[str, Sequence[str], None] = 'a9b4d2e7c5f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('game_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('game_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('external_event_id', sa.String(length=60), nullable=False),
    sa.Column('event_type', sa.String(length=20), nullable=False),
    sa.Column('period', sa.Integer(), nullable=True),
    sa.Column('game_seconds', sa.Integer(), nullable=True),
    sa.Column('team_id', sa.Integer(), nullable=True),
    sa.Column('actor_ref', sa.String(length=20), nullable=True),
    sa.Column('secondary_ref', sa.String(length=20), nullable=True),
    sa.Column('tertiary_ref', sa.String(length=20), nullable=True),
    sa.Column('actor_asset_id', sa.Integer(), nullable=True),
    sa.Column('secondary_asset_id', sa.Integer(), nullable=True),
    sa.Column('tertiary_asset_id', sa.Integer(), nullable=True),
    sa.Column('x_coord', sa.Float(), nullable=True),
    sa.Column('y_coord', sa.Float(), nullable=True),
    sa.Column('zone_code', sa.String(length=2), nullable=True),
    sa.Column('shot_type', sa.String(length=30), nullable=True),
    sa.ForeignKeyConstraint(['game_id'], ['games.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['real_teams.id'], ),
    sa.ForeignKeyConstraint(['actor_asset_id'], ['draftable_assets.id'], ),
    sa.ForeignKeyConstraint(['secondary_asset_id'], ['draftable_assets.id'], ),
    sa.ForeignKeyConstraint(['tertiary_asset_id'], ['draftable_assets.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'external_event_id')
    )
    op.create_index(op.f('ix_game_events_game_id'), 'game_events', ['game_id'], unique=False)
    op.create_table('event_credits',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('draftable_asset_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('credit_points', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['draftable_asset_id'], ['draftable_assets.id'], ),
    sa.ForeignKeyConstraint(['event_id'], ['game_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_event_credits_event_id'), 'event_credits', ['event_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_event_credits_event_id'), table_name='event_credits')
    op.drop_table('event_credits')
    op.drop_index(op.f('ix_game_events_game_id'), table_name='game_events')
    op.drop_table('game_events')
