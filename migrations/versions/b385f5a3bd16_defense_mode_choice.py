"""defense_mode_choice

Revision ID: b385f5a3bd16
Revises: 674793c5a374
Create Date: 2026-09-17 18:30:25.363409

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b385f5a3bd16'
down_revision: Union[str, Sequence[str], None] = '674793c5a374'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # SQLite can't ALTER a table to add a CHECK constraint directly - batch
    # mode does it via copy-and-move. server_default is required on the two
    # NOT NULL columns for the same reason (SQLite needs a value to backfill
    # existing rows into the rebuilt table).
    with op.batch_alter_table("league_universes") as batch_op:
        batch_op.add_column(sa.Column("defense_mode", sa.String(length=20), nullable=False, server_default="group"))
        batch_op.create_check_constraint("ck_league_universe_defense_mode", "defense_mode IN ('group', 'individual')")

    with op.batch_alter_table("roster_slot_definitions") as batch_op:
        batch_op.add_column(sa.Column("defense_mode", sa.String(length=20), nullable=True))
        batch_op.create_check_constraint(
            "ck_roster_slot_definition_defense_mode",
            "defense_mode IS NULL OR defense_mode IN ('group', 'individual')",
        )

    with op.batch_alter_table("universes") as batch_op:
        batch_op.add_column(sa.Column("supports_individual_defense", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("universes") as batch_op:
        batch_op.drop_column("supports_individual_defense")

    with op.batch_alter_table("roster_slot_definitions") as batch_op:
        batch_op.drop_constraint("ck_roster_slot_definition_defense_mode", type_="check")
        batch_op.drop_column("defense_mode")

    with op.batch_alter_table("league_universes") as batch_op:
        batch_op.drop_constraint("ck_league_universe_defense_mode", type_="check")
        batch_op.drop_column("defense_mode")
