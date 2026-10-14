"""divisions_system

Adds divisions table and teams.division_id for hierarchical conferences and divisions.

Revision ID: f9c2d5b8a1e4
Revises: e8b1c4a7f2d3
Create Date: 2026-10-13 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9c2d5b8a1e4"
down_revision: Union[str, Sequence[str], None] = "e8b1c4a7f2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "divisions" not in tables:
        op.create_table(
            "divisions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("league_id", sa.Integer(), nullable=False),
            sa.Column("conference_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
            sa.ForeignKeyConstraint(["conference_id"], ["conferences.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("divisions") as batch_op:
            batch_op.create_index("ix_divisions_league_id", ["league_id"])
            batch_op.create_index("ix_divisions_conference_id", ["conference_id"])

    team_cols = {c["name"] for c in inspector.get_columns("teams")}
    if "division_id" not in team_cols:
        with op.batch_alter_table("teams") as batch_op:
            batch_op.add_column(sa.Column("division_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key("fk_teams_division_id_divisions", "divisions", ["division_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_constraint("fk_teams_division_id_divisions", type_="foreignkey")
        batch_op.drop_column("division_id")

    with op.batch_alter_table("divisions") as batch_op:
        batch_op.drop_index("ix_divisions_conference_id")
        batch_op.drop_index("ix_divisions_league_id")
    op.drop_table("divisions")
