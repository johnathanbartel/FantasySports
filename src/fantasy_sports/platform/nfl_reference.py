"""NFL reference data: the `nfl` universe and its v1 roster slot definitions.

Idempotent and additive-only. Used by scripts/seed_nfl_universe.py and by league
creation, so a fresh database can host an NFL league with no manual seeding.
"""

from fantasy_sports.platform.db.models.core import LineupEntry, RosterSlotDefinition, Universe


ROSTER_SLOTS = [
    ("QB", ["QB"], 1, True, None),
    ("RB", ["RB"], 2, True, None),
    ("WR", ["WR"], 2, True, None),
    ("TE", ["TE"], 1, True, None),
    ("FLEX", ["RB", "WR", "TE"], 1, True, None),
    ("K", ["K", "PK"], 1, True, None),
    # Defense depends on how the league drafts it (league_universes.defense_mode):
    # group - the three DL/LB/DB units; team - one whole-defense asset;
    # individual - 11 named defenders (3 DL, 3 LB, 4 DB, 1 defensive flex).
    ("DL", ["DL"], 1, True, "group"),
    ("LB", ["LB"], 1, True, "group"),
    ("DB", ["DB"], 1, True, "group"),
    ("DEF", ["DEF"], 1, True, "team"),
    ("DL", ["DL"], 3, True, "individual"),
    ("LB", ["LB"], 3, True, "individual"),
    ("DB", ["DB"], 4, True, "individual"),
    ("DFLEX", ["DL", "LB", "DB"], 1, True, "individual"),
]

# Bench is 70% of the starter count (rounded to the nearest asset), per defense mode, so
# a league's roster - and its draft_rounds - is starters + bench: group 11+8 = 19,
# team 9+6 = 15, individual 19+13 = 32.
BENCH_FRACTION = 0.7
_OFFENSE = ["QB", "RB", "WR", "TE", "K", "PK"]
_BENCH_ELIGIBLE = {
    "group": _OFFENSE + ["DL", "LB", "DB"],
    "team": _OFFENSE + ["DEF"],
    "individual": _OFFENSE + ["DL", "LB", "DB"],
}
for _mode, _eligible in _BENCH_ELIGIBLE.items():
    _starters = sum(
        count for _code, _pos, count, is_starter, mode in ROSTER_SLOTS
        if is_starter and mode in (None, _mode)
    )
    ROSTER_SLOTS.append(("BENCH", _eligible, round(BENCH_FRACTION * _starters), False, _mode))


def seed_nfl_reference_data(db) -> tuple[Universe, int]:
    nfl = db.query(Universe).filter(Universe.code == "nfl").one_or_none()
    if nfl is None:
        nfl = Universe(
            sport="football",
            level="pro",
            code="nfl",
            display_name="NFL",
            supports_individual_defense=True,
        )
        db.add(nfl)
        db.flush()
    elif (nfl.sport, nfl.level) != ("football", "pro"):
        raise RuntimeError(
            "Existing 'nfl' universe has incompatible sport/level: "
            f"{nfl.sport!r}/{nfl.level!r}"
        )
    else:
        nfl.supports_individual_defense = True

    # an earlier version seeded one mode-less BENCH row (9 slots); bench size is now per mode
    legacy = db.query(RosterSlotDefinition).filter_by(universe_id=nfl.id, slot_code="BENCH", defense_mode=None).all()
    for row in legacy:
        if db.query(LineupEntry).filter_by(roster_slot_id=row.id).first() is not None:
            raise RuntimeError("Legacy NFL BENCH slot is referenced by lineups; migrate those lineups first")
        db.delete(row)

    inserted = 0
    for slot_code, eligible, count, is_starter, defense_mode in ROSTER_SLOTS:
        rows = db.query(RosterSlotDefinition).filter_by(
            universe_id=nfl.id,
            slot_code=slot_code,
            defense_mode=defense_mode,
        ).all()
        if len(rows) > 1:
            raise RuntimeError(
                f"NFL roster slot {slot_code!r} ({defense_mode or 'all modes'}) is duplicated"
            )
        if rows:
            row = rows[0]
            actual = (row.eligible_positions, row.slot_count, row.is_starter)
            expected = (eligible, count, is_starter)
            if (
                actual != expected
                and (actual[1:], set(actual[0]) <= set(eligible)) == (expected[1:], True)
            ):
                # only the eligible positions grew (e.g. BENCH gained DEF): additive, safe to widen
                row.eligible_positions = eligible
            elif actual != expected:
                raise RuntimeError(
                    f"Existing NFL roster slot {slot_code!r} conflicts with the v1 spec: "
                    f"found {actual!r}, expected {expected!r}"
                )
            continue
        db.add(RosterSlotDefinition(
            universe_id=nfl.id,
            slot_code=slot_code,
            eligible_positions=eligible,
            slot_count=count,
            is_starter=is_starter,
            defense_mode=defense_mode,
        ))
        inserted += 1

    db.commit()
    return nfl, inserted
