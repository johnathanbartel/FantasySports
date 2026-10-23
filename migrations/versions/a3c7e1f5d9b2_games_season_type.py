"""games_season_type

Adds games.season_type ('regular' or 'playoff') and backfills it for NHL games
from the NHL game id kept in game_events.external_event_id
("<season>:<10-digit NHL game id>:<event>"; digits 5-6 of the id are the game
type, 02 regular and 03 playoff). Other sports' games stay NULL.

Revision ID: a3c7e1f5d9b2
Revises: e5a1c7d3b9f2
Create Date: 2026-10-23 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3c7e1f5d9b2"
down_revision: Union[str, Sequence[str], None] = "e5a1c7d3b9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TYPES = {"02": "regular", "03": "playoff"}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "season_type" not in {c["name"] for c in inspector.get_columns("games")}:
        with op.batch_alter_table("games") as batch_op:
            batch_op.add_column(sa.Column("season_type", sa.String(length=10), nullable=True))

    if "game_events" not in set(inspector.get_table_names()):
        return
    rows = bind.execute(sa.text(
        "SELECT e.game_id, MIN(e.external_event_id) FROM game_events e "
        "JOIN games g ON g.id = e.game_id JOIN universes u ON u.id = g.universe_id "
        "WHERE u.code = 'nhl' AND e.source = 'nhl_api' GROUP BY e.game_id"
    )).fetchall()
    for game_id, ref in rows:
        parts = (ref or "").split(":")
        nhl_id = parts[1] if len(parts) > 2 else ""
        season_type = _TYPES.get(nhl_id[4:6]) if len(nhl_id) == 10 and nhl_id.isdigit() else None
        if season_type:
            bind.execute(
                sa.text("UPDATE games SET season_type = :t WHERE id = :g"),
                {"t": season_type, "g": game_id},
            )


def downgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.drop_column("season_type")
