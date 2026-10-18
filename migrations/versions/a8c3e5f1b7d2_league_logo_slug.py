"""league_logo_slug

Custom league URL and logo: leagues.slug (unique, lowercase), leagues.logo_data / logo_content_type / logo_updated_at.
Existing leagues have neither.

Revision ID: a8c3e5f1b7d2
Revises: a4e7c2b9d1f6
Create Date: 2026-10-18 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8c3e5f1b7d2"
down_revision: Union[str, Sequence[str], None] = "a4e7c2b9d1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("slug", sa.String(40), nullable=True))
        batch.add_column(sa.Column("logo_data", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("logo_content_type", sa.String(20), nullable=True))
        batch.add_column(sa.Column("logo_updated_at", sa.DateTime(), nullable=True))
        batch.create_unique_constraint("uq_leagues_slug", ["slug"])


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("uq_leagues_slug", type_="unique")
        batch.drop_column("logo_updated_at")
        batch.drop_column("logo_content_type")
        batch.drop_column("logo_data")
        batch.drop_column("slug")
