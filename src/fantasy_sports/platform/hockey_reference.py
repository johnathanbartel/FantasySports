"""NHL reference data used by league creation and the seeding script.

The public league wizard can call this on a fresh database.  It is additive
and idempotent, and it keeps the NHL starting lineup aligned with the product
brief: C, W, W, D, D, G.  Existing roster definitions are only rewritten when
no saved lineup points at them.
"""

from fantasy_sports.platform.db.models.core import LineupEntry, RosterSlotDefinition, Universe


ROSTER_SLOTS = [
    ("C", ["C"], 1, True),
    ("W", ["W", "LW", "RW"], 2, True),
    ("D", ["D"], 2, True),
    ("G", ["G"], 1, True),
    ("BENCH", ["C", "W", "LW", "RW", "D", "G"], 12, False),   # 6 starters + 12 bench = the 18-player roster (docs/hockey_spec.md 4.2)
]


def seed_hockey_reference_data(db) -> tuple[Universe, int]:
    nhl = db.query(Universe).filter(Universe.code == "nhl").one_or_none()
    if nhl is None:
        nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
        db.add(nhl)
        db.flush()
    elif (nhl.sport, nhl.level) != ("hockey", "pro"):
        raise RuntimeError(
            "Existing 'nhl' universe has incompatible sport/level: "
            f"{nhl.sport!r}/{nhl.level!r}"
        )

    expected_by_code = {row[0]: row[1:] for row in ROSTER_SLOTS}
    existing = db.query(RosterSlotDefinition).filter_by(universe_id=nhl.id).all()
    for row in existing:
        if row.defense_mode is not None or row.slot_code not in expected_by_code:
            if db.query(LineupEntry).filter_by(roster_slot_id=row.id).first() is not None:
                raise RuntimeError(
                    f"Legacy NHL roster slot {row.slot_code!r} is referenced by lineups; "
                    "migrate those lineups first"
                )
            db.delete(row)

    inserted = 0
    for slot_code, eligible, count, is_starter in ROSTER_SLOTS:
        rows = db.query(RosterSlotDefinition).filter_by(
            universe_id=nhl.id, slot_code=slot_code, defense_mode=None
        ).all()
        if len(rows) > 1:
            raise RuntimeError(f"NHL roster slot {slot_code!r} is duplicated")
        if rows:
            row = rows[0]
            actual = (row.eligible_positions, row.slot_count, row.is_starter)
            expected = (eligible, count, is_starter)
            if actual != expected:
                if actual[0] == expected[0] and actual[2] == expected[2]:
                    row.slot_count = count      # only the count changed: saved lineups stay valid
                    continue
                if db.query(LineupEntry).filter_by(roster_slot_id=row.id).first() is not None:
                    raise RuntimeError(
                        f"NHL roster slot {slot_code!r} conflicts with the current spec and "
                        "is referenced by lineups"
                    )
                row.eligible_positions = eligible
                row.slot_count = count
                row.is_starter = is_starter
            continue
        db.add(RosterSlotDefinition(
            universe_id=nhl.id,
            slot_code=slot_code,
            eligible_positions=eligible,
            slot_count=count,
            is_starter=is_starter,
        ))
        inserted += 1

    db.commit()
    return nhl, inserted
