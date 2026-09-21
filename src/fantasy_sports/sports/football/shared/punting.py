"""
Punter attribution: what the punter did, independent of anyone else.

A punter controls one thing - where the ball comes down. The return (returner
skill, coverage unit) and what happens next are other people's plays. So a
punt is priced at its LANDING spot, before any return:

    landing value = -EP(receiving team's 1st-and-10 at that spot)

and the punter's credit is that value minus what an average punt from the same
starting field position yields (the baseline), so a 40-yard punt from your own
end zone isn't punished for starting deep and a 35-yard punt from the opponent's
38 isn't rewarded for starting close. A punter is therefore credited only for
distance/placement beyond what the situation typically produces.

Excluded on purpose: blocked punts (a protection failure, not the punter) and
any punt whose text can't be parsed. Touchbacks count as a landing in the end
zone, spotted at the receiving team's 20 (the NCAA punt-touchback spot).
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_EP_CURVE_PATH = Path(__file__).parent.parent / "college" / "data" / "ep_curve.json"
_BASELINE_PATH = Path(__file__).parent.parent / "college" / "data" / "punt_ep_baseline.json"

PUNT_RE = re.compile(r"^(?P<punter>.+?)\s+punt for (?P<yards>-?\d+)\s*y(?:ar)?ds?", re.IGNORECASE)
TOUCHBACK_SPOT_YTG = 80  # receiving team's yards-to-goal after a punt touchback
BUCKET = 5  # yards of starting field position per baseline bucket


@dataclass(frozen=True)
class ParsedPunt:
    punter: str
    landing_ytg: int  # receiving team's yards-to-goal where the ball came down


def parse_punt(play_text: str | None, yards_to_goal: int | None) -> ParsedPunt | None:
    m = PUNT_RE.match(play_text or "")
    if not m or yards_to_goal is None:
        return None
    remaining = yards_to_goal - int(m["yards"])  # kicking team's distance to the goal line
    if "touchback" in play_text.lower() or remaining <= 0:
        return ParsedPunt(m["punter"], TOUCHBACK_SPOT_YTG)
    return ParsedPunt(m["punter"], 100 - remaining)


class EpCurve:
    """EP of a 1st-and-10 (CFBD get_predicted_points). The curve is indexed by
    yard line = yards from the offense's OWN goal, so callers pass yards-to-goal
    and this converts."""

    def __init__(self, path: Path = _EP_CURVE_PATH):
        rows = json.loads(path.read_text())["rows"]
        self._ep = {r["yard_line"]: r["predicted_points"] for r in rows}
        self._known = sorted(self._ep)

    def ep(self, yards_to_goal: int) -> float:
        yard_line = 100 - yards_to_goal
        if yard_line in self._ep:
            return self._ep[yard_line]
        return self._ep[min(self._known, key=lambda y: abs(y - yard_line))]


def landing_value(parsed: ParsedPunt, curve: EpCurve) -> float:
    return -curve.ep(parsed.landing_ytg)


def _bucket(yards_to_goal: int) -> int:
    return yards_to_goal // BUCKET


class PuntBaseline:
    """Mean landing value of a punt from each starting-field-position bucket."""

    def __init__(self, means: dict[int, float] | None = None, path: Path = _BASELINE_PATH):
        if means is None:
            data = json.loads(path.read_text())
            means = {int(k): v for k, v in data["mean_landing_value_by_bucket"].items()}
        self._means = means
        self._known = sorted(means)

    def mean(self, yards_to_goal: int) -> float:
        b = _bucket(yards_to_goal)
        if b in self._means:
            return self._means[b]
        return self._means[min(self._known, key=lambda k: abs(k - b))]

    @classmethod
    def derive(cls, punts: list[tuple[int, float]]) -> "PuntBaseline":
        """punts: (starting yards_to_goal, landing value) per punt."""
        by_bucket: dict[int, list[float]] = defaultdict(list)
        for ytg, value in punts:
            by_bucket[_bucket(ytg)].append(value)
        return cls({b: sum(v) / len(v) for b, v in by_bucket.items() if len(v) >= 20})

    def save(self, source: str, n_punts: int, path: Path = _BASELINE_PATH) -> None:
        path.write_text(json.dumps({
            "source": source, "n_punts": n_punts, "bucket_yards": BUCKET,
            "mean_landing_value_by_bucket": {str(k): round(v, 4) for k, v in sorted(self._means.items())},
        }, indent=1))


def attribute_punter(play, curve: EpCurve, baseline: PuntBaseline) -> tuple[str, float] | None:
    """(punter name, credit) for a Punt play; None if blocked/unparseable.
    Positive credit = the punt left the receiving team worse off than an
    average punt from that spot."""
    if play.play_type != "Punt":
        return None
    parsed = parse_punt(play.play_text, play.yards_to_goal)
    if parsed is None:
        return None
    return parsed.punter, landing_value(parsed, curve) - baseline.mean(play.yards_to_goal)
