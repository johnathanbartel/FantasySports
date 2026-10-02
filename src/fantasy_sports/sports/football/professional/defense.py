"""
NFL defense attribution from nflfastR's named-defender columns. One set of
play-level credits feeds all three ways a league can draft defense:

  individual  each named defender shares the play's value (-epa) in proportion
              to FITTED weights for what they did (sack, QB hit, tackle for
              loss, pass defense, interception, forced fumble, solo tackle,
              assist; data/defense_event_weights.json, from
              scripts/fit_defense_weights.py). Keyed by gsis_id, never by team,
              so a drafted player keeps scoring after a trade.
  group       a team's DL / LB / DB unit = the sum of its resolved players'
              credits, plus the value nobody is named on (and players whose
              position doesn't resolve) by a provisional rule: pass plays to DB
              (coverage), runs to LB. Keyed by team and unit.
  team        a team's whole defense = every credit on the play = -epa.

Credits on a play always sum to -epa (non-duplicative; docs/nfl_spec.md).
Optional baselines (what an average defended play earns each group / the team
/ an average involved defender of a position) are subtracted to center credits
so positions share a scale; the validation tie-out runs uncentered.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from fantasy_sports.platform.defense_modes import POSITION_TO_GROUP

GROUPS = ("DL", "LB", "DB")


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
    group: str | None = None  # DL/LB/DB from the roster; None if unresolved


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


_BASELINE_PATH = Path(__file__).parent / "data" / "defense_baseline.json"


@dataclass
class DefenseBaseline:
    """Credit an average defended play / involved defender earns, subtracted so
    DL, LB, DB, team and individual credits sit on comparable scales."""
    group: dict[str, float] = field(default_factory=lambda: {g: 0.0 for g in GROUPS})
    individual: dict[str, float] = field(default_factory=lambda: {g: 0.0 for g in GROUPS})

    @property
    def team(self) -> float:
        return sum(self.group.values())

    @classmethod
    def load(cls, path: Path = _BASELINE_PATH) -> "DefenseBaseline":
        if not path.exists():
            raise FileNotFoundError(f"{path} not found - run professional/scripts/fit_defense_weights.py")
        d = json.loads(path.read_text())
        return cls(group=d["group"], individual=d["individual"])


@dataclass
class PlayDefense:
    individuals: list[IndividualCredit]
    groups: dict[str, float]  # DL/LB/DB -> credit (after baseline)
    team: float
    named: bool  # at least one resolved defender was named on the play


def fallback_group(play) -> str:
    return "DB" if play.play_type == "pass" else "LB"


def attribute_play(play, weights: EventWeights, resolver: PositionResolver,
                   baseline: DefenseBaseline | None = None) -> PlayDefense:
    """All three views of one scrimmage play with a usable epa."""
    base = baseline or DefenseBaseline()
    credits, remainder = attribute_individuals(play, weights)
    groups = {g: 0.0 for g in GROUPS}
    individuals = []
    unassigned = remainder
    named = False
    for c in credits:
        group = resolver.resolve(c.gsis_id)
        if group:
            groups[group] += c.points
            named = True
        else:
            unassigned += c.points
        individuals.append(IndividualCredit(
            c.gsis_id, c.name, c.points - (base.individual[group] if group else 0.0), c.roles, group))
    groups[fallback_group(play)] += unassigned
    for g in GROUPS:
        groups[g] -= base.group[g]
    return PlayDefense(individuals, groups, -play.epa - base.team, named)
