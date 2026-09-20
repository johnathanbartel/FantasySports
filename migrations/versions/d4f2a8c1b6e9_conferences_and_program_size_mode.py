"""conferences_and_program_size_mode

Revision ID: d4f2a8c1b6e9
Revises: c9a1e6f2d7b3
Create Date: 2026-09-18 03:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f2a8c1b6e9'
down_revision: Union[str, Sequence[str], None] = 'c9a1e6f2d7b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # "team_choice" (teams pick their own program_size) or "dynamic"
    # (stubbed - see platform/db/models/core.py::League). Existing leagues
    # backfill to "team_choice", the only mode actually usable today.
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.add_column(
            sa.Column("program_size_mode", sa.String(length=20), nullable=False, server_default="team_choice")
        )
        batch_op.create_check_constraint(
            "ck_league_program_size_mode", "program_size_mode IN ('team_choice', 'dynamic')"
        )

    op.create_table(
        "conferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("program_size", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.CheckConstraint("program_size IN ('large', 'moderate', 'small')", name="ck_conference_program_size"),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Nullable: existing teams (all inserted directly via the ORM, not the
    # new POST /teams endpoint) predate conferences and never get one.
    with op.batch_alter_table("teams") as batch_op:
        batch_op.add_column(sa.Column("conference_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_teams_conference_id", "conferences", ["conference_id"], ["id"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_constraint("fk_teams_conference_id", type_="foreignkey")
        batch_op.drop_column("conference_id")

    op.drop_table("conferences")

    with op.batch_alter_table("leagues") as batch_op:
        batch_op.drop_constraint("ck_league_program_size_mode", type_="check")
        batch_op.drop_column("program_size_mode")
