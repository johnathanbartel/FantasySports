"""Offline NFL defense-attribution smoke test (synthetic nflfastR-shaped plays).

Checks the invariants from docs/nfl_spec.md without any external data: exactly
one DL/LB/DB group is credited per defended play, group credits sum to -epa
allowed, named defenders map to their roster group, the fallback rule applies
otherwise, and baseline centering shifts every group. The real-data check is
src/fantasy_sports/sports/football/professional/scripts/run_week_validation.py.

Run from the repository root with the data-pipeline environment (pandas):

    .venv/Scripts/python.exe scripts/seed_nfl_defense_smoke_test.py
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.sports.football.professional.defense import (
    EventWeights,
    PositionResolver,
    attribute_individuals,
)
from fantasy_sports.sports.football.professional.pipeline import process_plays

ROSTER = [
    {"gsis_id": "dl1", "position": "DE"},
    {"gsis_id": "lb1", "position": "OLB"},
    {"gsis_id": "db1", "position": "CB"},
    {"gsis_id": "db2", "position": "FS"},
    {"gsis_id": "wr1", "position": "WR"},
]

COLUMNS = [
    "play_type", "posteam", "defteam", "epa", "yac_epa", "sack", "interception",
    "complete_pass", "rusher_player_name", "passer_player_name", "receiver_player_name",
    "kicker_player_name", "kick_distance", "field_goal_result",
    "sack_player_id", "interception_player_id", "pass_defense_1_player_id",
    "solo_tackle_1_player_id", "assist_tackle_1_player_id", "qb_hit_1_player_id",
]


def play(**kw):
    row = {c: None for c in COLUMNS}
    row.update(posteam="AAA", defteam="BBB", sack=0, interception=0, complete_pass=0)
    row.update(kw)
    return row


def main():
    plays = pd.DataFrame([
        # sack by DL (named) - passer takes the offense side, DL the defense side
        play(play_type="pass", epa=-2.0, passer_player_name="QB", sack=1, sack_player_id="dl1"),
        # run tackled by a LB
        play(play_type="run", epa=0.5, rusher_player_name="RB", solo_tackle_1_player_id="lb1"),
        # interception: named INT (DB) wins over a tackle by a LB
        play(play_type="pass", epa=-4.0, passer_player_name="QB", interception=1,
             interception_player_id="db1", solo_tackle_1_player_id="lb1"),
        # completion, only tackler is an offensive-position id -> unresolved -> fallback DB
        play(play_type="pass", epa=1.5, yac_epa=0.5, complete_pass=1, passer_player_name="QB",
             receiver_player_name="WR", solo_tackle_1_player_id="wr1"),
        # run, nobody named -> fallback LB
        play(play_type="run", epa=-0.25, rusher_player_name="RB"),
    ])

    result = process_plays(plays, resolver=PositionResolver(ROSTER))
    dc = result.defense_credits
    expected = {("BBB", "DL"): 2.0, ("BBB", "LB"): -0.5 + 0.25, ("BBB", "DB"): 4.0 - 1.5}
    for key, want in expected.items():
        assert abs(dc[key] - want) < 1e-9, (key, dc[key], want)

    assert abs(result.defense_group_total("BBB") + result.epa_allowed_by_team["BBB"]) < 1e-9
    assert abs(result.epa_allowed_by_team["BBB"] - (-2.0 + 0.5 - 4.0 + 1.5 - 0.25)) < 1e-9
    assert (result.defense_named_plays, result.defense_fallback_plays) == (3, 2)

    defense_rows = [c for c in result.play_level_credits if c.position_group]
    assert len(defense_rows) == 5, len(defense_rows)  # one group credited per play, no duplicates
    assert all(c.team == "BBB" and c.role == c.position_group.lower() for c in defense_rows)

    # no resolver: everything falls back, still one group per play, still ties out
    bare = process_plays(plays)
    assert bare.defense_named_plays == 0 and bare.defense_fallback_plays == 5
    assert abs(bare.defense_group_total("BBB") + bare.epa_allowed_by_team["BBB"]) < 1e-9

    # baseline centering: every group has the baseline subtracted on every play
    base = {"DL": 0.1, "LB": 0.2, "DB": 0.3}
    centered = process_plays(plays, resolver=PositionResolver(ROSTER), defense_baseline=base)
    for g, b in base.items():
        assert abs(centered.defense_credits[("BBB", g)] - (expected[("BBB", g)] - 5 * b)) < 1e-9, g

    # offense unchanged by the defense wiring
    assert abs(result.model_total("AAA") - (-2.0 + 0.5 - 4.0 + 1.5 - 0.25)) < 1e-9

    # individual defenders: shares follow the weights, credits + remainder == -epa,
    # credits are keyed by player (not team), unnamed plays go to the team remainder
    w = EventWeights({"pass": {"sack": 3.0, "qb_hit": 1.0, "solo_tackle": 0.5, "pass_defense": 2.0},
                      "run": {"solo_tackle": 1.0, "assist_tackle": 0.5}})
    ind_plays = pd.DataFrame([
        {**play(play_type="pass", epa=-2.0, passer_player_name="QB", sack=1), "sack_player_id": "dl1",
         "sack_player_name": "D.Line", "qb_hit_1_player_id": "dl2", "qb_hit_1_player_name": "E.Dge"},
        {**play(play_type="run", epa=0.6), "solo_tackle_1_player_id": "lb1", "solo_tackle_1_player_name": "L.Backer",
         "assist_tackle_1_player_id": "db1", "assist_tackle_1_player_name": "D.Back"},
        play(play_type="pass", epa=1.0, complete_pass=1, passer_player_name="QB", yac_epa=0.2),
    ])
    first = next(ind_plays.itertuples())
    credits, rem = attribute_individuals(first, w)
    by_id = {c.gsis_id: c.points for c in credits}
    assert abs(by_id["dl1"] - 2.0 * 3 / 4) < 1e-9 and abs(by_id["dl2"] - 2.0 / 4) < 1e-9 and rem == 0.0

    res = process_plays(ind_plays, resolver=PositionResolver(ROSTER), individual_weights=w)
    assert abs(res.individual_credits["lb1"] - (-0.6 * 1.0 / 1.5)) < 1e-9
    assert abs(res.individual_credits["db1"] - (-0.6 * 0.5 / 1.5)) < 1e-9
    assert abs(res.defense_team_remainder["BBB"] - (-1.0)) < 1e-9
    total = sum(res.individual_credits.values()) + res.defense_team_remainder["BBB"]
    assert abs(total + res.epa_allowed_by_team["BBB"]) < 1e-9
    ind_rows = [c for c in res.play_level_credits if c.role == "defender"]
    assert {c.player_id for c in ind_rows} == {"dl1", "dl2", "lb1", "db1"}
    # same player credited under a different defteam keeps one running total
    traded = ind_plays.iloc[[1]].assign(defteam="CCC")
    both = process_plays(pd.concat([ind_plays.iloc[[1]], traded]), resolver=PositionResolver(ROSTER),
                         individual_weights=w)
    assert abs(both.individual_credits["lb1"] - 2 * (-0.6 / 1.5)) < 1e-9
    try:
        EventWeights.load(Path("/nonexistent/weights.json"))
        raise AssertionError("missing weights file must fail loudly")
    except FileNotFoundError:
        pass

    print("NFL defense attribution smoke test: OK")


if __name__ == "__main__":
    main()
