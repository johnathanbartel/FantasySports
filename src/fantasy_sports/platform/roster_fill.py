"""Which lineup slot each rostered player fills, for showing a team's lines and for the AI's draft needs.

Slots come from lineups.league_slots (specific position slots first, then flex slots, bench last). Players are placed
in the order they were drafted, so the same roster always shows - and is judged - the same way.
"""

from __future__ import annotations

from fantasy_sports.platform.db.models.core import RosterSlotDefinition

RosterItem = tuple[int, str | None]   # (draftable asset id, position)


def fill_slots(
    slots: list[RosterSlotDefinition], roster: list[RosterItem]
) -> tuple[list[tuple[RosterSlotDefinition, list[RosterItem]]], list[RosterItem]]:
    """[(slot, players in it)] in slot order, plus the players that fit no slot (a roster longer than the slots)."""
    remaining = list(roster)
    out = []
    for slot in slots:
        chosen: list[RosterItem] = []
        for item in ([] if slot.slot_code == "IR" else list(remaining)):
            if len(chosen) == slot.slot_count:
                break
            if item[1] in slot.eligible_positions:
                chosen.append(item)
                remaining.remove(item)
        out.append((slot, chosen))
    return out, remaining
