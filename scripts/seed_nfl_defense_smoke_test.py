"""Offline NFL defense-attribution smoke test (synthetic nflfastR-shaped plays).

Checks the invariants from docs/nfl_spec.md without any external data. For one
set of plays: individual credits follow the weights; group credits are the sum
of their players' credits plus the unnamed remainder (pass->DB, run->LB); team
credit is the whole play; individuals + group + team all reconcile to -epa; a
player's credit follows him across teams; baselines center credits. The
real-data check is
src/fantasy_sports/sports/football/professional/scripts/run_week_validation.py.

Run from the repository root with the data-pipeline environment (pandas):

    .venv/Scripts/python.exe scripts/seed_nfl_defense_smoke_test.py
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.sports.football.professional.defense import (
    DefenseBaseline,
    EventWeights,
    PositionResolver,
    attribute_individuals,
)
from fantasy_sports.sports.football.professional.pipeline import process_plays

ROSTER = [
    {"gsis_id": "dl1", "position": "DE"},
    {"gsis_id": "dl2", "position": "DT"},
    {"gsis_id": "lb1", "position": "OLB"},
    {"gsis_id": "db1", "position": "CB"},
    {"gsis_id": "wr1", "position": "WR"},
]
WEIGHTS = EventWeights({
    "pass": {"sack": 3.0, "qb_hit": 1.0, "solo_tackle": 0.5, "pass_defense": 2.0, "interception": 4.0},
    "run": {"solo_tackle": 1.0, "assist_tackle": 0.5},
})

ID_COLS = [
    "sack_player_id", "half_sack_1_player_id", "interception_player_id", "pass_defense_1_player_id",
    "solo_tackle_1_player_id", "assist_tackle_1_player_id", "qb_hit_1_player_id",
]
COLUMNS = [
    "play_type", "posteam", "defteam", "epa", "yac_epa", "sack", "interception", "complete_pass",
    "rusher_player_name", "passer_player_name", "receiver_player_name", "kicker_player_name",
    "kick_distance", "field_goal_result",
] + ID_COLS + [c.replace("_id", "_name") for c in ID_COLS]


def play(**kw):
    row = {c: None for c in COLUMNS}
    row.update(posteam="AAA", defteam="BBB", sack=0, interception=0, complete_pass=0)
    row.update(kw)
    return row


def close(a, b):
    return abs(a - b) < 1e-9


def main():
    plays = pd.DataFrame([
        # sack: DL1 (sacker) + DL2 (hit)           value +2.0 -> DL1 1.5, DL2 0.5
        play(play_type="pass", epa=-2.0, passer_player_name="QB", sack=1,
             sack_player_id="dl1", sack_player_name="D.One", qb_hit_1_player_id="dl2", qb_hit_1_player_name="D.Two"),
        # run: LB1 solo tackle; WR1 (unresolved position) assist   value -0.6 -> LB1 -0.4, WR1 -0.2 (to LB fallback)
        play(play_type="run", epa=0.6, rusher_player_name="RB",
             solo_tackle_1_player_id="lb1", solo_tackle_1_player_name="L.One",
             assist_tackle_1_player_id="wr1", assist_tackle_1_player_name="W.One"),
        # completion, nobody named: -1.0 all to remainder -> DB fallback
        play(play_type="pass", epa=1.0, complete_pass=1, passer_player_name="QB", yac_epa=0.2),
        # interception: CB1                         value +4.0 -> DB1 4.0
        play(play_type="pass", epa=-4.0, passer_player_name="QB", interception=1,
             interception_player_id="db1", interception_player_name="C.One"),
    ])
    resolver = PositionResolver(ROSTER)
    res = process_plays(plays, resolver=resolver, individual_weights=WEIGHTS)

    ind = res.individual_credits
    assert close(ind["dl1"], 1.5) and close(ind["dl2"], 0.5)
    assert close(ind["lb1"], -0.4) and close(ind["wr1"], -0.2) and close(ind["db1"], 4.0)

    dc = res.defense_credits
    assert close(dc[("BBB", "DL")], 2.0)
    assert close(dc[("BBB", "LB")], -0.4 + -0.2)        # unresolved WR1 share -> run fallback LB
    assert close(dc[("BBB", "DB")], 4.0 + -1.0)         # INT + unnamed completion -> pass fallback DB

    epa_allowed = -2.0 + 0.6 + 1.0 - 4.0
    assert close(res.epa_allowed_by_team["BBB"], epa_allowed)
    assert close(res.team_defense_credits["BBB"], -epa_allowed)
    assert close(res.defense_group_total("BBB"), -epa_allowed)           # groups reconcile to the play value
    assert close(sum(ind.values()) + res.defense_team_remainder["BBB"], -epa_allowed)
    assert (res.defense_named_plays, res.defense_fallback_plays) == (3, 1)

    by_role = {}
    for c in res.play_level_credits:
        if c.position_group:
            by_role.setdefault(c.role, []).append(c)
    assert set(by_role) == {"dl", "lb", "db", "def_team", "defender"}
    assert len(by_role["def_team"]) == 4
    assert all(c.player_id for c in by_role["defender"])

    first = next(plays.itertuples())
    credits, rem = attribute_individuals(first, WEIGHTS)
    assert rem == 0.0 and close(sum(c.points for c in credits), 2.0)

    # a player's credit follows him: same id under two defenses is one running total
    moved = pd.concat([plays.iloc[[1]], plays.iloc[[1]].assign(defteam="CCC")])
    both = process_plays(moved, resolver=resolver, individual_weights=WEIGHTS)
    assert close(both.individual_credits["lb1"], -0.8)
    assert close(both.defense_credits[("BBB", "LB")], both.defense_credits[("CCC", "LB")])

    # baselines subtract from every defended play (groups, team) and per involvement (individuals)
    base = DefenseBaseline(group={"DL": 0.1, "LB": 0.2, "DB": 0.3}, individual={"DL": 0.05, "LB": 0.1, "DB": 0.15})
    centered = process_plays(plays, resolver=resolver, individual_weights=WEIGHTS, defense_baseline=base)
    assert close(centered.defense_credits[("BBB", "DB")], dc[("BBB", "DB")] - 4 * 0.3)
    assert close(centered.team_defense_credits["BBB"], res.team_defense_credits["BBB"] - 4 * 0.6)
    assert close(centered.individual_credits["lb1"], ind["lb1"] - 0.1)
    assert close(centered.individual_credits["wr1"], ind["wr1"])         # unresolved position: no baseline

    try:
        EventWeights.load(Path("/nonexistent/weights.json"))
        raise AssertionError("missing weights file must fail loudly")
    except FileNotFoundError:
        pass

    print("NFL defense attribution smoke test: OK")


if __name__ == "__main__":
    main()
