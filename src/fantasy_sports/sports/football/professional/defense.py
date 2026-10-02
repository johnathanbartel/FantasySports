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


# --- Individual defender attribution ---------------------------------------
#
# For leagues that draft individual defenders. Every defender nflfastR names
# on a play shares that play's -epa in proportion to a FITTED weight for what
# they did (sack, QB hit, tackle for loss, pass defense, interception, forced
# fumble, solo tackle, assist), so the shares always sum to the play value
# (non-duplicative). Weights come from data/defense_event_weights.json,
# produced by scripts/fit_defense_weights.py; they are never hand-set.
#
# Plays with no named defender have no individual to credit; their value goes
# to the team remainder (see PipelineResult.defense_team_remainder) so team
# defense still reconciles to -epa allowed.
#
# Credits are keyed by the defender's gsis_id, not by team: a fantasy team that
# drafts a player keeps him after a trade, and the play's own `defteam` only
# records where he played that snap. Team and group assets, by contrast, are
# keyed by team and unit and ignore who was on the field.

import json
from collections import defaultdict
from pathlib import Path

_WEIGHTS_PATH = Path(__file__).parent / "data" / "defense_event_weights.json"

# role -> [(id column, name column, count per mention)]
EVENT_COLUMNS = {
    "sack": [("sack_player_id", "sack_player_name", 1.0),
             ("half_sack_1_player_id", "half_sack_1_player_name", 0.5),
             ("half_sack_2_player_id", "half_sack_2_player_name", 0.5)],
    "qb_hit": [("qb_hit_1_player_id", "qb_hit_1_player_name", 1.0),
               ("qb_hit_2_player_id", "qb_hit_2_player_name", 1.0)],
    "tackle_for_loss": [("tackle_for_loss_1_player_id", "tackle_for_loss_1_player_name", 1.0),
                        ("tackle_for_loss_2_player_id", "tackle_for_loss_2_player_name", 1.0)],
    "pass_defense": [("pass_defense_1_player_id", "pass_defense_1_player_name", 1.0),
                     ("pass_defense_2_player_id", "pass_defense_2_player_name", 1.0)],
    "interception": [("interception_player_id", "interception_player_name", 1.0)],
    "forced_fumble": [("forced_fumble_player_1_player_id", "forced_fumble_player_1_player_name", 1.0),
                      ("forced_fumble_player_2_player_id", "forced_fumble_player_2_player_name", 1.0)],
    "solo_tackle": [("solo_tackle_1_player_id", "solo_tackle_1_player_name", 1.0),
                    ("solo_tackle_2_player_id", "solo_tackle_2_player_name", 1.0)],
    "assist_tackle": [(f"assist_tackle_{i}_player_id", f"assist_tackle_{i}_player_name", 1.0)
                      for i in range(1, 5)],
}
EVENT_ROLES = tuple(EVENT_COLUMNS)


def play_events(play) -> dict[str, list[tuple[str, str | None, float]]]:
    """role -> [(gsis_id, name, count)] for every defender named on the play."""
    out: dict[str, list] = {}
    for role, cols in EVENT_COLUMNS.items():
        for id_col, name_col, count in cols:
            gsis = getattr(play, id_col, None)
            if gsis is None or pd.isna(gsis):
                continue
            name = getattr(play, name_col, None)
            out.setdefault(role, []).append((str(gsis), None if pd.isna(name) else name, count))
    return out


class EventWeights:
    """{"pass": {role: weight}, "run": {role: weight}} from the fitted file."""

    def __init__(self, table: dict[str, dict[str, float]], source: str = "inline"):
        self.table = table
        self.source = source

    @classmethod
    def load(cls, path: Path = _WEIGHTS_PATH) -> "EventWeights":
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found - run professional/scripts/fit_defense_weights.py first "
                "(individual defense weights are fitted from data, never hand-set)")
        data = json.loads(path.read_text())
        return cls(data["weights"], data.get("source", str(path)))

    def weight(self, play_type: str, role: str) -> float:
        return self.table.get(play_type, {}).get(role, 0.0)


@dataclass(frozen=True)
class IndividualCredit:
    gsis_id: str
    name: str | None
    points: float
    roles: tuple[str, ...]


def attribute_individuals(play, weights: EventWeights) -> tuple[list[IndividualCredit], float]:
    """(per-defender credits, remainder) for one scrimmage play. Credits plus
    remainder always equal -epa."""
    value = -play.epa
    shares: dict[str, float] = defaultdict(float)
    names: dict[str, str | None] = {}
    roles: dict[str, list[str]] = defaultdict(list)
    for role, mentions in play_events(play).items():
        w = weights.weight(play.play_type, role)
        for gsis, name, count in mentions:
            shares[gsis] += w * count
            names[gsis] = name
            if role not in roles[gsis]:
                roles[gsis].append(role)
    total = sum(shares.values())
    if total <= 0:
        return [], value
    credits = [IndividualCredit(g, names[g], value * s / total, tuple(roles[g]))
               for g, s in shares.items() if s > 0]
    return credits, 0.0
