"""
Credit attribution: splitting a single play's EPA/PPA among the players
involved, so it's never counted in full for more than one player.

Passing plays split between QB and receiver. Rushing plays go entirely to
the ball carrier (no split needed - O-line scoring is out of scope for v1).
"""

import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

_LOOKUP_PATH = Path(__file__).parent / "data" / "yac_epa_lookup.json"
_FG_EP_LOOKUP_PATH = Path(__file__).parent.parent / "college" / "data" / "fg_ep_lookup.json"
_DROP_RATE_PATH = Path(__file__).parent / "data" / "drop_rate.json"


@dataclass(frozen=True)
class PassSplit:
    qb_credit: float
    receiver_credit: float


def split_passing_epa_exact(epa: float, yac_epa: float) -> PassSplit:
    """NFL case: nflfastR gives us the real yac_epa for this specific play."""
    receiver_credit = yac_epa
    qb_credit = epa - yac_epa
    return PassSplit(qb_credit=qb_credit, receiver_credit=receiver_credit)


class YacEpaLookup:
    """NCAAF case: no air_yards/yac data, so we key off yardsGained against
    an empirical mean_yac_epa table derived from NFL play-by-play (see
    Football/common/scripts/derive_yac_split.py). The receiver gets that
    typical after-catch value; the QB gets the remainder of the play's
    actual ppa - so the split always sums exactly to the real ppa, even
    though the *proportions* are an NFL-derived approximation.
    """

    def __init__(self, path: Path = _LOOKUP_PATH):
        data = json.loads(path.read_text())
        self.source = data["source"]
        self.years = data["years"]
        self._buckets = data["buckets"]
        # upper edges, in order, for bisecting a yards_gained value to a bucket
        self._upper_edges = [b["yards_gained_max_inclusive"] for b in self._buckets]

    def mean_yac_epa(self, yards_gained: float) -> float:
        idx = bisect_right(self._upper_edges, yards_gained)
        idx = min(idx, len(self._buckets) - 1)
        return self._buckets[idx]["mean_yac_epa"]

    def split(self, ppa: float, yards_gained: float) -> PassSplit:
        receiver_credit = self.mean_yac_epa(yards_gained)
        qb_credit = ppa - receiver_credit
        return PassSplit(qb_credit=qb_credit, receiver_credit=receiver_credit)


def _load_drop_rate(path: Path = _DROP_RATE_PATH) -> float:
    return json.loads(path.read_text())["drop_rate"]


# Population-average share of an incompletion's EPA attributed to drops,
# from Pro Football Reference's charted weekly drop counts vs. total NFL
# incompletions 2022-2024 (see derive_drop_rate.py) - ~14%. Neither CFBD
# nor nflfastR's play-by-play tags individual drops, so this can't be
# play-specific or depth-conditioned like the YAC split; it's the best
# available correction against the alternative of always blaming the QB.
DROP_RATE = _load_drop_rate()


def split_passing_epa_incomplete(epa: float, receiver_identified: bool = True) -> PassSplit:
    """Splits an incompletion's EPA between QB and receiver using
    DROP_RATE, when a receiver is identifiable from the play text. If the
    play text doesn't name a target (CFBD sometimes omits it entirely),
    there's no one to attribute drop-share to, so it falls back to the old
    all-QB behavior rather than crediting/debiting an unknown player.
    """
    if not receiver_identified:
        return PassSplit(qb_credit=epa, receiver_credit=0.0)
    receiver_credit = DROP_RATE * epa
    qb_credit = epa - receiver_credit
    return PassSplit(qb_credit=qb_credit, receiver_credit=receiver_credit)


# --- Defensive group attribution (DL / LB / DB) ---------------------------
#
# CFBD gives us no tackler identity and no run-gap/coverage-responsibility
# data (confirmed: 0/154 plays in a sample game mention a tackler in
# playText), so there's no way to know which individual defender - let
# alone which position group - actually made a given stop. This is a
# situational heuristic keyed on play type and yards gained, not a
# validated attribution model. It is the "needs a defined rule, likely
# based on which position group's assigned gap/zone the stop occurred in,
# to the extent that's inferable from available data" gap the spec called
# out explicitly - this is v1's answer to that, and should be revisited if
# a richer data source (e.g. charted participation data) becomes available.
#
# Rule of thumb: a defensive stop close to the line of scrimmage is a DL
# win (they beat their block before anyone else got involved); a short
# gain that still gets stopped is the LB reading and reacting; anything
# that gets into the second/third level before being stopped, or any pass
# defended, is credited to the DB group as the last line of run support /
# the group responsible for coverage.
#
# Design choice: defensive credit mirrors the offense's ppa on *every*
# scrimmage play, not just negative-EPA ones (defense_credit = -ppa
# always) - a defense that allows a 40-yard gain should lose fantasy
# points for it, symmetrically with how it gains points for a stop. This
# is broader than the spec's own illustrative example (which only
# describes the negative-play case), so flag it for review if a
# stops-only model is preferred instead.

RUN_STUFF_MAX_YARDS = 0   # <= this many yards -> DL stop at/behind the line
RUN_SHORT_MAX_YARDS = 3   # <= this many yards -> LB stop, short gain
PASS_UNDERNEATH_MAX_YARDS = 3  # <= this many yards -> LB, short/underneath coverage

DEFENSE_GROUPS = ("DL", "LB", "DB")


@dataclass(frozen=True)
class DefenseSplit:
    dl: float = 0.0
    lb: float = 0.0
    db: float = 0.0


def attribute_defense(play_type: str, yards_gained: float | None, ppa: float) -> DefenseSplit | None:
    """Returns None for play types this heuristic doesn't cover (field
    goals, punts, kickoffs, penalties, etc. - those already carry no ppa
    from CFBD, so callers should skip them before reaching here anyway)."""
    defense_value = -ppa

    if play_type == "Sack":
        group = "DL"
    elif play_type in ("Rush", "Rushing Touchdown"):
        if yards_gained is None:
            return None
        if yards_gained <= RUN_STUFF_MAX_YARDS:
            group = "DL"
        elif yards_gained <= RUN_SHORT_MAX_YARDS:
            group = "LB"
        else:
            group = "DB"
    elif play_type in ("Pass Reception", "Passing Touchdown"):
        if yards_gained is None:
            return None
        group = "LB" if yards_gained <= PASS_UNDERNEATH_MAX_YARDS else "DB"
    elif play_type in ("Pass Incompletion", "Pass Interception Return"):
        group = "DB"
    else:
        return None

    return DefenseSplit(**{group.lower(): defense_value})


# --- Kicker marginal-EP attribution ----------------------------------------
#
# Flat-crediting a made FG at 3 points overvalues kickers, exactly like flat
# TD bonuses overvalue receivers: a 20-yard chip shot and a 55-yard boot are
# both "3 points" in standard fantasy despite being wildly different in
# difficulty. CFBD's field-goal expected-points curve (cached by
# Football/College/scripts/fetch_fg_ep_table.py) gives the average point
# value of an attempt from a given distance, make probability included - so
# a kicker's marginal contribution is the gap between what actually happened
# and what an average attempt from that distance is worth.


class FieldGoalEpLookup:
    def __init__(self, path: Path = _FG_EP_LOOKUP_PATH):
        data = json.loads(path.read_text())
        self.source = data["source"]
        self._by_distance = {row["distance"]: row["expected_points"] for row in data["rows"]}
        self._distances = sorted(self._by_distance)

    def expected_points(self, distance: int) -> float:
        if distance in self._by_distance:
            return self._by_distance[distance]
        nearest = min(self._distances, key=lambda d: abs(d - distance))
        return self._by_distance[nearest]


def attribute_kicker(distance: int, made: bool, lookup: FieldGoalEpLookup) -> float:
    actual_points = 3.0 if made else 0.0
    return actual_points - lookup.expected_points(distance)
