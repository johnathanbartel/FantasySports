"""team_defense_mode

Revision ID: b8e2f4a6c1d7
Revises: c3d8e1a5f9b2
Create Date: 2026-10-02 12:00:00.000000

Adds the whole-team defense option: defense_mode may now be 'team', and
defense_groups.position_group may be 'DEF' (the entire defense, alongside the
DL/LB/DB units). Revises the hockey head c3d8e1a5f9b2; the separate
a3f7c1e9d2b4 head is still unmerged (see AGENTS.md, known doc drift).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8e2f4a6c1d7'
down_revision: Union[str, Sequence[str], None] = 'c3d8e1a5f9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_MODES = "defense_mode IN ('group', 'individual')"
NEW_MODES = "defense_mode IN ('group', 'individual', 'team')"
OLD_SLOT_MODES = "defense_mode IS NULL OR defense_mode IN ('group', 'individual')"
NEW_SLOT_MODES = "defense_mode IS NULL OR defense_mode IN ('group', 'individual', 'team')"
OLD_GROUPS = "position_group IN ('DL','LB','DB')"
NEW_GROUPS = "position_group IN ('DL','LB','DB','DEF')"


def _swap(table: str, name: str, new_check: str, widen_position_group: bool = False) -> None:
    with op.batch_alter_table(table) as batch_op:
        batch_op.drop_constraint(name, type_="check")
        if widen_position_group:
            batch_op.alter_column("position_group", existing_type=sa.String(length=2),
                                  type_=sa.String(length=3), existing_nullable=False)
        batch_op.create_check_constraint(name, new_check)


def upgrade() -> None:
    _swap("league_universes", "ck_league_universe_defense_mode", NEW_MODES)
    _swap("roster_slot_definitions", "ck_roster_slot_definition_defense_mode", NEW_SLOT_MODES)
    _swap("defense_groups", "ck_defense_group_position", NEW_GROUPS, widen_position_group=True)


def downgrade() -> None:
    op.execute("DELETE FROM defense_groups WHERE position_group = 'DEF'")
    op.execute("DELETE FROM roster_slot_definitions WHERE defense_mode = 'team'")
    op.execute("UPDATE league_universes SET defense_mode = 'group' WHERE defense_mode = 'team'")
    _swap("defense_groups", "ck_defense_group_position", OLD_GROUPS)
    _swap("roster_slot_definitions", "ck_roster_slot_definition_defense_mode", OLD_SLOT_MODES)
    _swap("league_universes", "ck_league_universe_defense_mode", OLD_MODES)
