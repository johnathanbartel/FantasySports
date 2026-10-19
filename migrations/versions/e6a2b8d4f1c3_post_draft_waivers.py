"""post-draft waivers

leagues.post_draft_waivers: undrafted players go on waivers for 24 hours after the draft ends (default off).

Revision ID: e6a2b8d4f1c3
Revises: d5f1a7c3e9b2
Create Date: 2026-10-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6a2b8d4f1c3"
down_revision: Union[str, Sequence[str], None] = "d5f1a7c3e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("post_draft_waivers", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("post_draft_waivers")
