"""season_champion

league_seasons.champion_team_id: the champion named at rollover, who picks last in the next draft.

Revision ID: e3c8a1d5b9f2
Revises: d2b7f4a9c6e1
Create Date: 2026-10-15 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3c8a1d5b9f2"
down_revision: Union[str, Sequence[str], None] = "d2b7f4a9c6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("league_seasons") as batch:
        batch.add_column(sa.Column("champion_team_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_league_seasons_champion_team_id", "teams", ["champion_team_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("league_seasons") as batch:
        batch.drop_constraint("fk_league_seasons_champion_team_id", type_="foreignkey")
        batch.drop_column("champion_team_id")
