"""
NFL-specific per-play processing for the professional pipeline: turns
nflfastR play-by-play rows into player credit.

Unlike sports/football/college/pipeline.py, this needs no text parsing:
CFBD's play objects carry no structured player-identity fields, so the
college pipeline has to regex passer/receiver/rusher names out of free-text
playText. nflfastR already provides those as plain columns
(passer_player_name, receiver_player_name, rusher_player_name,
sack_player_name, interception_player_name, kicker_player_name), plus a
real per-play yac_epa - so the passer/receiver split is exact
(split_passing_epa_exact), not the yards-gained-bucket approximation the
college pipeline needs (YacEpaLookup).

Defense (DL/LB/DB) attribution lives in professional/defense.py and is wired
into process_plays below; see its docstring for the rules.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from fantasy_sports.sports.football.professional.defense import (
    DefenseBaseline,
    EventWeights,
    PositionResolver,
    attribute_play,
)
from fantasy_sports.sports.football.shared.attribution import (
    FieldGoalEpLookup,
    attribute_kicker,
    split_passing_epa_exact,
    split_passing_epa_incomplete,
)


@dataclass
class PlayLevelCredit:
    """One player's credit on one specific play - mirrors
    college/pipeline.py's PlayLevelCredit shape so both sports feed the
    same play_credits DB table without a translation layer."""
    play: object  # the nflfastR pbp row (a pandas Series)
    role: str  # "passer"/"receiver"/"rusher"/"kicker", or "dl"/"lb"/"db"/"def_team"/"defender" for defense
    points: float
    player_name: str | None = None
    team: str | None = None  # offense team, or the defense team for group credits
    position_group: str | None = None  # "DL"/"LB"/"DB" for defense credits
    player_id: str | None = None  # gsis_id; set on individual-defender credits


@dataclass
class PipelineResult:
    credits: dict = field(default_factory=lambda: defaultdict(float))  # (player, team) -> pts
    scrimmage_epa_by_team: dict = field(default_factory=lambda: defaultdict(float))
    fg_credit_by_team: dict = field(default_factory=lambda: defaultdict(float))
    defense_credits: dict = field(default_factory=lambda: defaultdict(float))  # (team, group) -> pts
    epa_allowed_by_team: dict = field(default_factory=lambda: defaultdict(float))  # defense team -> opp scrimmage epa
    fg_credit_allowed_by_team: dict = field(default_factory=lambda: defaultdict(float))  # defense team -> opp kicker credit
    team_defense_credits: dict = field(default_factory=lambda: defaultdict(float))  # defteam -> whole-defense credit
    individual_credits: dict = field(default_factory=lambda: defaultdict(float))  # gsis_id -> pts (follows the player across teams)
    individual_names: dict = field(default_factory=dict)  # gsis_id -> name
    defense_team_remainder: dict = field(default_factory=lambda: defaultdict(float))  # defteam -> -epa on plays with no named defender
    defense_named_plays: int = 0
    defense_fallback_plays: int = 0
    play_level_credits: list = field(default_factory=list)  # list[PlayLevelCredit]
    unattributed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    def model_points_allowed(self, team: str) -> float:
        """Model estimate of the points `team`'s defense allowed (mirror of
        model_total: opponent scrimmage epa + opponent kicker credit)."""
        return self.epa_allowed_by_team.get(team, 0.0) + self.fg_credit_allowed_by_team.get(team, 0.0)

    def defense_group_total(self, team: str) -> float:
        """Sum of the team's DL/LB/DB credits (uncentered: equals -epa allowed)."""
        return sum(self.defense_credits.get((team, g), 0.0) for g in ("DL", "LB", "DB"))

    def model_total(self, team: str) -> float:
        return self.scrimmage_epa_by_team.get(team, 0.0) + self.fg_credit_by_team.get(team, 0.0)

    def _credit(self, play, role: str, points: float, player_name: str, team: str):
        self.credits[(player_name, team)] += points
        self.play_level_credits.append(PlayLevelCredit(
            play=play, role=role, points=points, player_name=player_name, team=team,
        ))


# play_type values this pipeline attributes. Everything else (kickoff, punt,
# no_play, qb_kneel, qb_spike, extra_point) is skipped for v1 - same scope
# cut as the college pipeline (punts/kickoffs/return scores excluded there
# too, for the same reason: no per-play EPA worth attributing, or no skill-
# position credit to assign).
SCRIMMAGE_PLAY_TYPES = {"run", "pass"}


def _attribute_scrimmage_play(play, result: PipelineResult):
    epa = play.epa
    team = play.posteam

    if play.play_type == "run":
        if pd.isna(play.rusher_player_name):
            result.unattributed.append(play)
            return
        result._credit(play, "rusher", epa, play.rusher_player_name, team)
        return

    # play_type == "pass"
    if pd.isna(play.passer_player_name):
        result.unattributed.append(play)
        return

    if play.sack == 1 or play.interception == 1:
        # v1: full (negative) credit to the QB on sacks/INTs, matching the
        # college pipeline's same v1 scope cut - the sacking defender/
        # intercepting DB's positive mirror credit is a defense-attribution
        # concern, out of scope here.
        result._credit(play, "passer", epa, play.passer_player_name, team)
        return

    if play.complete_pass == 1:
        yac_epa = 0.0 if pd.isna(play.yac_epa) else play.yac_epa
        split = split_passing_epa_exact(epa, yac_epa)
        result._credit(play, "passer", split.qb_credit, play.passer_player_name, team)
        result._credit(play, "receiver", split.receiver_credit, play.receiver_player_name, team)
        return

    # Incompletion. nflfastR names the targeted receiver on ~90% of these
    # (vs. CFBD's ~9.5%) - reuse the same population drop-rate split as the
    # college pipeline for the ones that do (real PFR-derived DROP_RATE,
    # not NFL-specific here but the same population it was measured on).
    receiver_identified = not pd.isna(play.receiver_player_name)
    split = split_passing_epa_incomplete(epa, receiver_identified=receiver_identified)
    result._credit(play, "passer", split.qb_credit, play.passer_player_name, team)
    if receiver_identified:
        result._credit(play, "receiver", split.receiver_credit, play.receiver_player_name, team)


def _attribute_defense_play(play, weights: EventWeights, resolver: PositionResolver,
                            baseline: DefenseBaseline | None, result: PipelineResult):
    team = play.defteam
    result.epa_allowed_by_team[team] += play.epa
    d = attribute_play(play, weights, resolver, baseline)
    if d.named:
        result.defense_named_plays += 1
    else:
        result.defense_fallback_plays += 1
    if not d.individuals:
        result.defense_team_remainder[team] += -play.epa

    result.team_defense_credits[team] += d.team
    result.play_level_credits.append(PlayLevelCredit(
        play=play, role="def_team", points=d.team, team=team, position_group="TEAM",
    ))
    for g, points in d.groups.items():
        result.defense_credits[(team, g)] += points
        result.play_level_credits.append(PlayLevelCredit(
            play=play, role=g.lower(), points=points, team=team, position_group=g,
        ))
    for c in d.individuals:
        result.individual_credits[c.gsis_id] += c.points
        result.individual_names[c.gsis_id] = c.name
        result.play_level_credits.append(PlayLevelCredit(
            play=play, role="defender", points=c.points, player_name=c.name,
            team=team, position_group=c.group, player_id=c.gsis_id,
        ))


def process_plays(
    pbp,
    fg_lookup: FieldGoalEpLookup | None = None,
    resolver: PositionResolver | None = None,
    defense_baseline: DefenseBaseline | None = None,
    individual_weights: EventWeights | None = None,
) -> PipelineResult:
    """Runs offense attribution, defense attribution (individual, DL/LB/DB group
    and whole-team credit - see defense.py) and kicker marginal-EP over nflfastR
    play-by-play rows (a DataFrame, or any iterable of row objects/Series
    exposing the same columns) for one or many games.
    `resolver` (season roster -> DL/LB/DB) maps defenders to their position
    group; `individual_weights` are the fitted event weights (default: the saved
    file, which must exist); `defense_baseline` centers defense credits (omit for
    uncentered, which is what the validation tie-out uses)."""
    fg_lookup = fg_lookup or FieldGoalEpLookup()
    resolver = resolver or PositionResolver()
    individual_weights = individual_weights or EventWeights.load()
    result = PipelineResult()

    rows = pbp.itertuples() if isinstance(pbp, pd.DataFrame) else pbp

    for play in rows:
        if play.play_type == "field_goal":
            if pd.isna(play.kicker_player_name) or pd.isna(play.kick_distance):
                result.unattributed.append(play)
                continue
            made = play.field_goal_result == "made"
            kicker_credit = attribute_kicker(int(play.kick_distance), made, fg_lookup)
            result._credit(play, "kicker", kicker_credit, play.kicker_player_name, play.posteam)
            result.fg_credit_by_team[play.posteam] += kicker_credit
            result.fg_credit_allowed_by_team[play.defteam] += kicker_credit
            continue

        if play.play_type not in SCRIMMAGE_PLAY_TYPES or pd.isna(play.epa):
            result.skipped.append(play)
            continue

        result.scrimmage_epa_by_team[play.posteam] += play.epa
        _attribute_scrimmage_play(play, result)
        _attribute_defense_play(play, individual_weights, resolver, defense_baseline, result)

    return result
