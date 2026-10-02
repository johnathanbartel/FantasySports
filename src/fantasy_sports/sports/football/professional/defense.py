"""
NFL defense attribution: splitting a defended scrimmage play's value among
the defense's DL / LB / DB units, from nflfastR's named-defender columns.

A play's value to the defense is -epa. Unlike the college pipeline
(shared/defense.py), nflfastR names the defender who made the play on most
snaps (tackler, sacker, interceptor, pass defender, ...) via *_player_id
columns, so the group is usually read directly off the play instead of
inferred from measured shares:

  1. Named defender -> that defender's group (via a gsis_id -> position
     resolver built from the season roster) takes the WHOLE play value. Which
     named defender wins is a fixed priority (sack > interception > forced
     fumble > pass defense > tackle for loss > solo tackle > assist tackle >
     QB hit), so exactly one group is credited per play.
  2. No resolvable defender (TDs, incompletions with no pass defender/hit,
     unmapped positions) -> provisional play-type rule: pass plays to DB
     (coverage), runs to LB. Reported as "fallback" so the caller can see how
     much credit rests on it. Not a validated model.

Because exactly one group gets each play, the group credits sum to -epa
(the non-duplicative invariant in docs/nfl_spec.md). An optional per-group
baseline (credit an average group earns per defended play) centers the
credits the way college does; with no baseline credits are uncentered. No NFL
baseline is derived yet - that needs a league-wide sample of real plays.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

GROUPS = ("DL", "LB", "DB")

POSITION_TO_GROUP = {
    "DL": "DL", "DE": "DL", "DT": "DL", "NT": "DL", "EDGE": "DL",
    "LB": "LB", "ILB": "LB", "MLB": "LB", "OLB": "LB",
    "DB": "DB", "CB": "DB", "S": "DB", "SS": "DB", "FS": "DB", "SAF": "DB",
}

# (role, nflfastR player-id column) in priority order - first resolvable wins.
NAMED_DEFENDER_COLUMNS = (
    ("sack", "sack_player_id"),
    ("sack", "half_sack_1_player_id"),
    ("sack", "half_sack_2_player_id"),
    ("interception", "interception_player_id"),
    ("forced_fumble", "forced_fumble_player_1_player_id"),
    ("pass_defense", "pass_defense_1_player_id"),
    ("pass_defense", "pass_defense_2_player_id"),
    ("tackle_for_loss", "tackle_for_loss_1_player_id"),
    ("tackle_for_loss", "tackle_for_loss_2_player_id"),
    ("tackle", "solo_tackle_1_player_id"),
    ("tackle", "assist_tackle_1_player_id"),
    ("tackle", "assist_tackle_2_player_id"),
    ("tackle", "assist_tackle_3_player_id"),
    ("tackle", "assist_tackle_4_player_id"),
    ("qb_hit", "qb_hit_1_player_id"),
    ("qb_hit", "qb_hit_2_player_id"),
)


class PositionResolver:
    """gsis_id -> DL/LB/DB, from roster rows ({gsis_id, position}). A player
    whose position isn't a defensive-group position resolves to None."""

    def __init__(self, roster_rows: list[dict] | None = None):
        self._by_id: dict[str, str] = {}
        for r in roster_rows or []:
            group = POSITION_TO_GROUP.get(str(r.get("position") or "").upper())
            gsis = r.get("gsis_id")
            if group and gsis and not pd.isna(gsis):
                self._by_id[str(gsis)] = group

    @classmethod
    def from_roster_frame(cls, roster: pd.DataFrame) -> "PositionResolver":
        # nfl_data_py rosters carry the gsis id as `player_id`
        id_col = "gsis_id" if "gsis_id" in roster.columns else "player_id"
        rows = roster[[id_col, "position"]].dropna().rename(columns={id_col: "gsis_id"})
        return cls(rows.to_dict("records"))

    def resolve(self, gsis_id) -> str | None:
        if gsis_id is None or pd.isna(gsis_id):
            return None
        return self._by_id.get(str(gsis_id))


@dataclass(frozen=True)
class DefenseCredit:
    group: str
    points: float
    named: bool  # True when a named defender resolved to this group


def _fallback_group(play) -> str:
    return "DB" if play.play_type == "pass" else "LB"


def credited_group(play, resolver: PositionResolver) -> tuple[str, bool]:
    """(group, named) for one scrimmage play."""
    for _role, column in NAMED_DEFENDER_COLUMNS:
        group = resolver.resolve(getattr(play, column, None))
        if group:
            return group, True
    return _fallback_group(play), False


def attribute_defense(
    play, resolver: PositionResolver, baseline: dict[str, float] | None = None
) -> list[DefenseCredit]:
    """Credits for one scrimmage play with a usable epa. The credited group
    gets -epa; with a baseline every group also has its baseline subtracted
    (centering), so a group can carry a row on a play it had no part in."""
    group, named = credited_group(play, resolver)
    value = -play.epa
    out = []
    for g in GROUPS:
        points = (value if g == group else 0.0) - (baseline or {}).get(g, 0.0)
        if points != 0.0:
            out.append(DefenseCredit(g, points, named and g == group))
    return out
