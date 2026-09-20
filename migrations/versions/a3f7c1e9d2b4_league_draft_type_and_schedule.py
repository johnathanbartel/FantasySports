"""league_draft_type_and_schedule

Revision ID: a3f7c1e9d2b4
Revises: d4f2a8c1b6e9
Create Date: 2026-09-17 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f7c1e9d2b4'
down_revision: Union[str, Sequence[str], None] = 'd4f2a8c1b6e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # "recruit" (timed sealed-bid auction, shown to teams as a "recruiting
    # period") or "snake" (a traditional snake draft run at a scheduled
    # time). Existing leagues backfill to "snake", since every draft built
    # so far (drafts/draft_picks) is snake-shaped.
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.add_column(
            sa.Column("draft_type", sa.String(length=20), nullable=False, server_default="snake")
        )
        batch_op.add_column(sa.Column("draft_scheduled_at", sa.DateTime(), nullable=True))
        batch_op.create_check_constraint(
            "ck_league_draft_type", "draft_type IN ('recruit', 'snake')"
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.drop_constraint("ck_league_draft_type", type_="check")
        batch_op.drop_column("draft_scheduled_at")
        batch_op.drop_column("draft_type")
