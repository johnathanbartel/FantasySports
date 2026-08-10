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


def split_passing_epa_incomplete(epa: float) -> PassSplit:
    """No catch happened, so there's no after-catch value to attribute."""
    return PassSplit(qb_credit=epa, receiver_credit=0.0)
