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
    for row in db.query(RosterSlotDefinition).filter_by(universe_id=nba.id, league_id=None).all():
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
            universe_id=nba.id, league_id=None, slot_code=slot_code, defense_mode=None
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


# --- Per-league layouts (FS-037) ------------------------------------------------------------------------------------
# The universe rows above are the default (5 starters + 10 bench).  A league may instead carry its own rows
# (RosterSlotDefinition.league_id), which lineups.league_slots then uses in place of the universe's.

ALL_POSITIONS = ["PG", "SG", "SF", "PF", "C", "G", "F"]
FIVE = ("PG", "SG", "SF", "PF", "C")


def layout_slots(*, g: int = 0, f: int = 0, util: int = 0, bench: int = 10, ir: int = 0) -> list[tuple]:
    """(slot_code, eligible positions, count, is_starter) for the five position slots plus the optional G, F and UTIL
    starters, the bench and injured reserve.  layout_slots() is the default 5 + 10 layout."""
    for name, n in (("g", g), ("f", f), ("util", util), ("bench", bench), ("ir", ir)):
        if n < 0:
            raise ValueError(f"{name} can't be negative")
    rows = [row for row in ROSTER_SLOTS if row[0] != "BENCH"]
    if g:
        rows.append(("G", ["PG", "SG", "G"], g, True))
    if f:
        rows.append(("F", ["SF", "PF", "F"], f, True))
    if util:
        rows.append(("UTIL", list(ALL_POSITIONS), util, True))
    if bench:
        rows.append(("BENCH", list(ALL_POSITIONS), bench, False))
    if ir:
        rows.append(("IR", list(ALL_POSITIONS), ir, False))      # not a roster spot; never auto-filled
    return rows


def apply_league_layout(db, league, **counts) -> list[RosterSlotDefinition]:
    """Gives `league` its own NBA slot layout (keywords of layout_slots).  Replaces an earlier custom layout, but refuses
    once any lineup points at it.  Sets draft_rounds to the roster size (every slot except IR)."""
    old = db.query(RosterSlotDefinition).filter_by(league_id=league.id).all()
    if any(db.query(LineupEntry).filter_by(roster_slot_id=r.id).first() is not None for r in old):
        raise RuntimeError("League layout is referenced by saved lineups; it can't be replaced")
    universe = db.query(Universe).filter_by(code="nba").one()
    spec = layout_slots(**counts)
    for r in old:
        db.delete(r)
    db.flush()
    rows = [RosterSlotDefinition(universe_id=universe.id, league_id=league.id, slot_code=code, eligible_positions=eligible,
                                 slot_count=count, is_starter=starter) for code, eligible, count, starter in spec]
    db.add_all(rows)
    league.draft_rounds = sum(count for code, _, count, _ in spec if code != "IR")
    db.flush()
    return rows
