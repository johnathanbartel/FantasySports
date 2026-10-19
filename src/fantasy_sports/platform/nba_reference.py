"""NBA reference data used by league creation and the seeding script.

Additive and idempotent, like hockey_reference.  An NBA team carries 15 players: five starters (PG, SG, SF, PF, C)
and ten on the bench, who may be any position.  Existing definitions are only rewritten when no saved lineup
points at them.  Real teams, players and schedules are a separate ingestion concern.
"""

from fantasy_sports.platform.db.models.core import LineupEntry, RosterSlotDefinition, Universe


ROSTER_SLOTS = [
    ("PG", ["PG", "G"], 1, True),
    ("SG", ["SG", "G"], 1, True),
    ("SF", ["SF", "F"], 1, True),
    ("PF", ["PF", "F"], 1, True),
    ("C", ["C"], 1, True),
    ("BENCH", ["PG", "SG", "SF", "PF", "C", "G", "F"], 10, False),   # 5 starters + 10 bench = the 15-player roster
]

ROSTER_SIZE = sum(count for _, _, count, _ in ROSTER_SLOTS)


def seed_nba_reference_data(db) -> tuple[Universe, int]:
    nba = db.query(Universe).filter(Universe.code == "nba").one_or_none()
    if nba is None:
        nba = Universe(sport="basketball", level="pro", code="nba", display_name="NBA")
        db.add(nba)
        db.flush()
    elif (nba.sport, nba.level) != ("basketball", "pro"):
        raise RuntimeError(
            "Existing 'nba' universe has incompatible sport/level: "
            f"{nba.sport!r}/{nba.level!r}"
        )

    expected_by_code = {row[0]: row[1:] for row in ROSTER_SLOTS}
    for row in db.query(RosterSlotDefinition).filter_by(universe_id=nba.id).all():
        if row.defense_mode is not None or row.slot_code not in expected_by_code:
            if db.query(LineupEntry).filter_by(roster_slot_id=row.id).first() is not None:
                raise RuntimeError(
                    f"Legacy NBA roster slot {row.slot_code!r} is referenced by lineups; "
                    "migrate those lineups first"
                )
            db.delete(row)

    inserted = 0
    for slot_code, eligible, count, is_starter in ROSTER_SLOTS:
        rows = db.query(RosterSlotDefinition).filter_by(
            universe_id=nba.id, slot_code=slot_code, defense_mode=None
        ).all()
        if len(rows) > 1:
            raise RuntimeError(f"NBA roster slot {slot_code!r} is duplicated")
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
                        f"NBA roster slot {slot_code!r} conflicts with the current spec and "
                        "is referenced by lineups"
                    )
                row.eligible_positions = eligible
                row.slot_count = count
                row.is_starter = is_starter
            continue
        db.add(RosterSlotDefinition(
            universe_id=nba.id,
            slot_code=slot_code,
            eligible_positions=eligible,
            slot_count=count,
            is_starter=is_starter,
        ))
        inserted += 1

    db.commit()
    return nba, inserted
