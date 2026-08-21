"""
Shared per-play processing for the NCAAF pipeline: turns a list of CFBD
Play objects into player/defense/kicker credit. Used by both the
single-game script (run_game_pipeline.py) and the multi-game validation
script (run_week_validation.py) so the two never drift out of sync.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from fantasy_sports.sports.football.shared.attribution import (
    YacEpaLookup,
    split_passing_epa_incomplete,
    attribute_defense,
    FieldGoalEpLookup,
    attribute_kicker,
)

RUSH_RE = re.compile(r"^(?P<runner>.+?) run (?:for|to)\b")
# CFBD sometimes uses a "recap" text style for touchdowns instead of the
# standard play-by-play phrasing, e.g. "Savion Red 1 Yd Run (Kicker Kick)"
# rather than "Savion Red run for 1 yd ... for a TD (Kicker KICK)".
RUSH_TD_ALT_RE = re.compile(r"^(?P<runner>.+?) \d+ Yd Run\b")
# A third text style, seen on some smaller-conference games, e.g.
# "M.Bailey rushed for 7 yards. TOUCHDOWN." / "B.Olson rushed for -3 yards."
RUSH_ALT2_RE = re.compile(r"^(?P<runner>.+?) rushed for -?\d+ yards?\b")
# Clock-killing kneel-downs: not a meaningful skill-position play, and the
# text format varies too much to reliably pull a player name out of anyway
# (e.g. "[NH] Kneel down by JMU at JMU18 (team loss of 2)", "N. Glantz
# takes a knee", "Kneel down by STONE, Preston at NEV40").
KNEEL_RE = re.compile(r"[Kk]neel down|takes a knee")

PASS_COMPLETE_RE = re.compile(r"^(?P<passer>.+?) pass complete to (?P<receiver>.+?) for\b")
PASS_COMPLETE_FALLBACK_RE = re.compile(r"^(?P<passer>.+?) pass complete to (?P<receiver>.+)$")
# Recap style for passing TDs: "Receiver N Yd pass from Passer (Kicker Kick)"
PASS_TD_ALT_RE = re.compile(r"^(?P<receiver>.+?) \d+ Yd pass from (?P<passer>.+?)\b")
# Third style: "J.Labas pass complete. Catch made by E.Boyd for 47 yards. TOUCHDOWN."
PASS_COMPLETE_ALT2_RE = re.compile(r"^(?P<passer>.+?) pass complete\. Catch made by (?P<receiver>.+?) for\b")

# Dropped the trailing '$' so suffixes like ", broken up by X" don't block a
# match - we only need the passer's name, not the rest of the sentence.
PASS_INCOMPLETE_RE = re.compile(r"^(?P<passer>.+?) pass incomplete\b(?: to (?P<receiver>[^,.]+))?")

SACK_RE = re.compile(r"^(?P<passer>.+?) sacked by\b")
SACK_ALT_RE = re.compile(r"^(?P<passer>.+?) sacked for\b")  # no defender named
# Third style: "G.Booty steps back to pass. Sacked at JST 39 for -8 yards (E.Guthrie)."
SACK_ALT2_RE = re.compile(r"^(?P<passer>.+?) steps back to pass\. Sacked\b")

# IGNORECASE covers the third style's "pass INTERCEPTED" alongside the
# standard "pass intercepted".
INTERCEPTION_RE = re.compile(r"^(?P<passer>.+?) pass intercepted\b", re.IGNORECASE)

FIELD_GOAL_RE = re.compile(r"^(?P<kicker>.+?)\s+(?P<distance>\d+)\s*[Yy]d\.?\s*(?:Field Goal|FG)\b")

# Play types that carry a real ppa but involve turnover-return yardage or
# fumble mechanics this pipeline doesn't attribute yet (who forced/returned
# a fumble, who scored on a defensive/special-teams return). Explicitly
# skipped rather than treated as a parser failure - a real future-work item,
# not a bug.
SKIPPED_PLAY_TYPES = {
    "Fumble Recovery (Own)",
    "Fumble Recovery (Opponent)",
    "Fumble Return Touchdown",
    "Interception Return Touchdown",
    "Blocked Punt Touchdown",
    "Punt Return Touchdown",
    "Safety",
    "End Period",
}


@dataclass
class PlayLevelCredit:
    """One player/defense-group's credit on one specific play - the detail
    behind the aggregate `credits`/`defense_credits` dicts below. Exists
    for consumers (e.g. the DB seed script's `plays`/`play_credits`
    tables) that need per-play breakdown, not just a running total."""
    play: object
    role: str  # "passer"/"receiver"/"rusher"/"kicker"/"dl"/"lb"/"db"
    points: float
    player_name: str | None = None   # offense/kicker credits
    team: str | None = None          # offense team (player credits) or defense team (group credits)
    position_group: str | None = None  # "DL"/"LB"/"DB" for defense-group credits


@dataclass
class PipelineResult:
    credits: dict = field(default_factory=lambda: defaultdict(float))  # (player, team) -> pts
    defense_credits: dict = field(default_factory=lambda: defaultdict(float))  # (team, group) -> pts
    scrimmage_ppa_by_team: dict = field(default_factory=lambda: defaultdict(float))
    fg_credit_by_team: dict = field(default_factory=lambda: defaultdict(float))
    play_level_credits: list = field(default_factory=list)  # list[PlayLevelCredit]
    unattributed: list = field(default_factory=list)
    unattributed_fg: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    def model_total(self, team: str) -> float:
        return self.scrimmage_ppa_by_team.get(team, 0.0) + self.fg_credit_by_team.get(team, 0.0)

    def _credit(self, play, role: str, points: float, player_name: str, team: str):
        self.credits[(player_name, team)] += points
        self.play_level_credits.append(PlayLevelCredit(
            play=play, role=role, points=points, player_name=player_name, team=team,
        ))


def _attribute_offense(play, lookup: YacEpaLookup, result: PipelineResult):
    ppa = play.ppa
    play_type = play.play_type
    text = play.play_text or ""

    if play_type in SKIPPED_PLAY_TYPES:
        result.skipped.append(play)
        return

    if play_type in ("Rush", "Rushing Touchdown"):
        if KNEEL_RE.search(text):
            result.skipped.append(play)
            return
        m = RUSH_RE.match(text) or RUSH_TD_ALT_RE.match(text) or RUSH_ALT2_RE.match(text)
        if not m:
            result.unattributed.append(play)
            return
        result._credit(play, "rusher", ppa, m["runner"], play.offense)

    elif play_type in ("Pass Reception", "Passing Touchdown"):
        m = (
            PASS_COMPLETE_RE.match(text)
            or PASS_TD_ALT_RE.match(text)
            or PASS_COMPLETE_ALT2_RE.match(text)
            or PASS_COMPLETE_FALLBACK_RE.match(text)
        )
        if not m:
            result.unattributed.append(play)
            return
        split = lookup.split(ppa, play.yards_gained)
        result._credit(play, "passer", split.qb_credit, m["passer"], play.offense)
        result._credit(play, "receiver", split.receiver_credit, m["receiver"], play.offense)

    elif play_type == "Pass Incompletion":
        m = PASS_INCOMPLETE_RE.match(text)
        if not m:
            result.unattributed.append(play)
            return
        receiver = m["receiver"]
        split = split_passing_epa_incomplete(ppa, receiver_identified=bool(receiver))
        result._credit(play, "passer", split.qb_credit, m["passer"], play.offense)
        if receiver:
            result._credit(play, "receiver", split.receiver_credit, receiver, play.offense)

    elif play_type == "Sack":
        m = SACK_RE.match(text) or SACK_ALT_RE.match(text) or SACK_ALT2_RE.match(text)
        if not m:
            result.unattributed.append(play)
            return
        # v1: full (negative) credit to the QB. The sacking defender's
        # positive mirror credit is part of defensive group attribution.
        result._credit(play, "passer", ppa, m["passer"], play.offense)

    elif play_type in ("Pass Interception Return", "Interception"):
        m = INTERCEPTION_RE.match(text)
        if not m:
            result.unattributed.append(play)
            return
        # v1: full (negative) credit to the QB, same caveat as sacks.
        result._credit(play, "passer", ppa, m["passer"], play.offense)

    else:
        result.unattributed.append(play)


def process_plays(
    plays,
    lookup: YacEpaLookup | None = None,
    fg_lookup: FieldGoalEpLookup | None = None,
) -> PipelineResult:
    """Runs the full attribution chain (offense split, defense split,
    kicker marginal-EP) over a list of CFBD Play objects, which may span
    one or many games."""
    lookup = lookup or YacEpaLookup()
    fg_lookup = fg_lookup or FieldGoalEpLookup()
    result = PipelineResult()

    for play in plays:
        if play.play_type in ("Field Goal Good", "Field Goal Missed"):
            m = FIELD_GOAL_RE.match(play.play_text or "")
            if not m:
                result.unattributed_fg.append(play)
                continue
            distance = int(m["distance"])
            made = play.play_type == "Field Goal Good"
            kicker_credit = attribute_kicker(distance, made, fg_lookup)
            result._credit(play, "kicker", kicker_credit, m["kicker"], play.offense)
            result.fg_credit_by_team[play.offense] += kicker_credit
            continue

        if play.ppa is None:
            continue
        result.scrimmage_ppa_by_team[play.offense] += play.ppa
        _attribute_offense(play, lookup, result)

        d_split = attribute_defense(play.play_type, play.yards_gained, play.ppa)
        if d_split is not None:
            for group in ("dl", "lb", "db"):
                value = getattr(d_split, group)
                if value:
                    result.defense_credits[(play.defense, group.upper())] += value
                    result.play_level_credits.append(PlayLevelCredit(
                        play=play, role=group, points=value,
                        team=play.defense, position_group=group.upper(),
                    ))

    return result
