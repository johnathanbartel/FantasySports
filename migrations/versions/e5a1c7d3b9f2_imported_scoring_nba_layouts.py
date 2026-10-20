"""imported scoring and per-league NBA layouts

leagues.scoring_system ('realistic' default, or 'imported'), the imported_scoring_items table (a frozen stat formula),
and roster_slot_definitions.league_id (a league's own slot layout; NULL = the universe's default).

Revision ID: e5a1c7d3b9f2
Revises: d2f6a8c4e1b7
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5a1c7d3b9f2"
down_revision: Union[str, Sequence[str], None] = "d2f6a8c4e1b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("scoring_system", sa.String(length=10), nullable=False, server_default="realistic"))
        batch.create_check_constraint("ck_league_scoring_system", "scoring_system IN ('realistic', 'imported')")
    op.create_table(
        "imported_scoring_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("source_platform", sa.String(length=20), nullable=False),
        sa.Column("source_stat_id", sa.Integer(), nullable=False),
        sa.Column("stat_code", sa.String(length=10), nullable=False),
        sa.Column("points", sa.Numeric(8, 3), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("league_id", "stat_code", name="uq_imported_scoring_league_stat"),
    )
    op.create_index("ix_imported_scoring_items_league_id", "imported_scoring_items", ["league_id"])
    with op.batch_alter_table("roster_slot_definitions") as batch:
        batch.add_column(sa.Column("league_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_roster_slot_definitions_league_id", "leagues", ["league_id"], ["id"])
        batch.create_index("ix_roster_slot_definitions_league_id", ["league_id"])


def downgrade() -> None:
    with op.batch_alter_table("roster_slot_definitions") as batch:
        batch.drop_index("ix_roster_slot_definitions_league_id")
        batch.drop_constraint("fk_roster_slot_definitions_league_id", type_="foreignkey")
        batch.drop_column("league_id")
    op.drop_index("ix_imported_scoring_items_league_id", table_name="imported_scoring_items")
    op.drop_table("imported_scoring_items")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_scoring_system", type_="check")
        batch.drop_column("scoring_system")
