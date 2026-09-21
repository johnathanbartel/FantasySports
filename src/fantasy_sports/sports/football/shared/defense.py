"""
Defense attribution: splitting a defensive play's value among DL / LB / DB.

A play's value to the defense is -ppa, and each group's credit is CENTERED:

    credit(group) = share(group) * (-ppa)  -  baseline(group)

where baseline(group) is what that group earns on an average defended play. An
average DL, LB or DB group therefore nets ~0 per game and the groups are on one
scale. (The earlier version was uncentered: passing is positive-EPA on average,
so it charged DBs about -26 EPA a game and paid DL for the same defense.)
Centering is per group, not per play type, so real differences - allowing
completions instead of incompletions - are preserved.

Who gets the value, in order of evidence:

  1. A defender named in the play text - the sacker, the interceptor, the
     forcer of a fumble, the pass breakup, a safety's scorer - is mapped to a
     group through the season roster (PositionResolver), and that group gets the
     whole play. This is real attribution, not inference.
  2. Otherwise the value is shared across groups by MEASURED shares
     (data/defense_shares.json, derived by scripts/derive_defense_shares.py from
     plays where the position is known): e.g. runs of 10+ yards are tackled by
     DBs ~70% of the time, runs of <=2 yards by DL ~45%. This replaces the older
     rule that handed 100% of a play to one group by yardage alone, which put the
     wrong group on ~40% of sacks and ~24% of interceptions.

  3. The measured shares are then TILTED per team (TeamDefenseProfile) by that
     team's own season stats: a defense whose DBs make an unusually large share
     of the tackles gets a DB-heavier split of its unnamed plays. Multipliers are
     each group's share of the team's stat (tackles, sacks, deflections+hurries,
     interceptions) over the league's share, shrunk toward 1 so a small sample
     can't swing a split far. Forced fumbles have no season stat and stay
     league-wide. Season stats include the games being replayed, which is right
     for a replay of a finished season; for a season in progress they are
     season-to-date.

Limits worth knowing: on ordinary runs and completions no defender is named
(~2% of plays name a tackler), so a team's split there comes from its season
stat profile, not from the play itself. Fumbles the offense recovers, and special-teams plays, give
the defense nothing here.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

_SHARES_PATH = Path(__file__).parent.parent / "college" / "data" / "defense_shares.json"
_PROFILE_PATH = Path(__file__).parent.parent / "college" / "data" / "team_defense_profile.json"

GROUPS = ("DL", "LB", "DB")
POSITION_TO_GROUP = {
    "DL": "DL", "DE": "DL", "DT": "DL", "EDGE": "DL", "NT": "DL",
    "LB": "LB",
    "DB": "DB", "S": "DB", "CB": "DB",
}

RUN_TYPES = {"Rush", "Rushing Touchdown"}
PASS_TYPES = {"Pass Reception", "Passing Touchdown", "Pass Incompletion", "Sack",
              "Interception", "Pass Interception Return", "Interception Return Touchdown"}
FUMBLE_TYPES = {"Fumble Recovery (Opponent)", "Fumble Return Touchdown"}
SAFETY_TYPES = {"Safety"}
# Plays that carry defensive value. Fumbles the offense recovers itself
# ("Fumble Recovery (Own)"), penalties, and special teams are deliberately not here.
DEFENSIVE_TYPES = RUN_TYPES | PASS_TYPES | FUMBLE_TYPES | SAFETY_TYPES

SACK_BY_RE = re.compile(r"sacked by (?P<n>.+?)(?: for| and|,|$)")
INTERCEPTED_RE = re.compile(r"pass intercepted (?P<n>.+?) return", re.IGNORECASE)
INTERCEPTED_BY_RE = re.compile(r"Intercepted by (?P<n>[A-Za-z.' -]+?) at")
FORCED_BY_RE = re.compile(r"forced by (?P<n>[^,.;]+?)(?:,| recovered|\.|$)")
BROKEN_UP_RE = re.compile(r"broken up by (?P<n>[^,.;]+)")
SAFETY_RE = re.compile(r"^(?P<n>.+?) Safety$")
# CFBD's 2026 text names defenders as "#7 C.Kennedy" (jersey number + first initial + last name):
#   "... sacked for loss of 15 yards to the NAU10 (#7 C.Kennedy)"
#   "... pass intercepted by #35 Q.Olson at NAU32"     "... broken up by #7 A.Jackson"
SACK_PAREN_RE = re.compile(r"sacked for (?:a )?loss of \d+ yards? to the \S+ \((?P<n>#\d+ [^);]+)")
INTERCEPTED_BY_S26_RE = re.compile(r"pass intercepted by (?P<n>#\d+ \S+)", re.IGNORECASE)
FORCED_BY_S26_RE = re.compile(r"forced by (?P<n>#\d+ \S+)")
BROKEN_UP_S26_RE = re.compile(r"broken up by (?P<n>#\d+ \S+)")
_JERSEY_PREFIX_RE = re.compile(r"^#\d+\s+")
_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv)$")


def _norm(name: str) -> str:
    name = _JERSEY_PREFIX_RE.sub("", name.strip())
    return _SUFFIX_RE.sub("", name.lower().replace(".", " ").replace("'", "").replace("-", " ").strip()).strip()


def _short_key(name: str) -> str | None:
    """'A.Thomas' / 'Anthony Thomas' -> 'athomas' (first initial + last name)."""
    parts = _norm(name).split()
    if len(parts) < 2:
        return None
    return parts[0][0] + "".join(parts[1:])


class PositionResolver:
    """Maps a defender's name in play text to DL/LB/DB via the season roster.
    A name that matches more than one position group on the team is unresolved."""

    def __init__(self, roster_rows: list[dict] | None = None):
        self._full: dict[str, dict[str, set[str]]] = {}
        self._short: dict[str, dict[str, set[str]]] = {}
        for r in roster_rows or []:
            group = POSITION_TO_GROUP.get(r.get("position"))
            if not group or not r.get("first") or not r.get("last"):
                continue
            full = _norm(f'{r["first"]} {r["last"]}')
            short = _short_key(f'{r["first"]} {r["last"]}')
            self._full.setdefault(r["team"], {}).setdefault(full, set()).add(group)
            if short:
                self._short.setdefault(r["team"], {}).setdefault(short, set()).add(group)

    def resolve(self, team: str, name: str | None) -> str | None:
        if not name:
            return None
        for index, key in ((self._full, _norm(name)), (self._short, _short_key(name))):
            groups = index.get(team, {}).get(key) if key else None
            if groups and len(groups) == 1:
                return next(iter(groups))
        return None


class DefenseShares:
    def __init__(self, path: Path = _SHARES_PATH):
        data = json.loads(path.read_text())
        self.source = data["source"]
        self.group_baseline_per_play: dict[str, float] = data["group_baseline_per_play"]
        self._shares: dict[str, dict[str, float]] = data["shares"]

    @classmethod
    def uncentered(cls, shares: dict[str, dict[str, float]]) -> "DefenseShares":
        """Shares with no baseline - used only while deriving the baseline."""
        self = cls.__new__(cls)
        self.source = "uncentered"
        self.group_baseline_per_play = {g: 0.0 for g in GROUPS}
        self._shares = shares
        return self

    def shares(self, key: str) -> dict[str, float]:
        return self._shares[key]


# Which season stat tilts which share key.
STAT_FOR_SHARE_KEY = {
    "run_short": "tackles", "run_mid": "tackles", "run_long": "tackles",
    "catch_short": "tackles", "catch_mid": "tackles", "catch_long": "tackles",
    "sack": "sack", "interception": "interception", "incompletion": "incompletion",
}
# (category, stat_type) rows that make up each profile stat
PROFILE_STATS = {
    "tackles": (("defensive", "TOT"),),
    "sack": (("defensive", "SACKS"),),
    "interception": (("interceptions", "INT"),),
    "incompletion": (("defensive", "PD"), ("defensive", "QB HUR")),
}
DEFAULT_PRIOR_FRAC = 0.05  # shrinkage weight, as a fraction of a median team's stat total (tuned on held-out named tackles)


class TeamDefenseProfile:
    """team -> profile stat -> {group: multiplier}. Empty = no tilt (league shares)."""

    def __init__(self, table: dict | None = None):
        self._t = table or {}

    def multipliers(self, team: str, share_key: str) -> dict[str, float] | None:
        stat = STAT_FOR_SHARE_KEY.get(share_key)
        return self._t.get(team, {}).get(stat) if stat else None

    @property
    def table(self) -> dict:
        return self._t

    @classmethod
    def for_season(cls, season: int, path: Path = _PROFILE_PATH) -> "TeamDefenseProfile":
        """The saved profile for one season (empty - no tilt - if none is saved)."""
        if not path.exists():
            return cls()
        return cls(json.loads(path.read_text())["seasons"].get(str(season)))

    @classmethod
    def build(cls, stat_rows: list[dict], teams: set[str] | None = None,
              prior_frac: float = DEFAULT_PRIOR_FRAC) -> "TeamDefenseProfile":
        """stat_rows: {team, position, category, stat_type, stat} per player (CFBD
        player season stats). `teams` restricts the league to the teams being scored."""
        table: dict[str, dict[str, dict[str, float]]] = {}
        for stat_name, sources in PROFILE_STATS.items():
            by_team: dict[str, dict[str, float]] = {}
            for r in stat_rows:
                if (r["category"], r["stat_type"]) not in sources:
                    continue
                group = POSITION_TO_GROUP.get(r.get("position"))
                if not group or (teams is not None and r["team"] not in teams):
                    continue
                try:
                    v = float(r["stat"])
                except (TypeError, ValueError):
                    continue
                by_team.setdefault(r["team"], {g: 0.0 for g in GROUPS})[group] += v
            if not by_team:
                continue
            league = {g: sum(t[g] for t in by_team.values()) for g in GROUPS}
            total = sum(league.values())
            if total <= 0:
                continue
            L = {g: league[g] / total for g in GROUPS}
            totals = sorted(sum(t.values()) for t in by_team.values())
            K = prior_frac * totals[len(totals) // 2]
            for team, t in by_team.items():
                T = sum(t.values())
                if T <= 0 or min(L.values()) <= 0:
                    continue
                table.setdefault(team, {})[stat_name] = {
                    g: round(((t[g] + K * L[g]) / (T + K)) / L[g], 4) for g in GROUPS}
        return cls(table)


def yards_band(yards: float | None) -> str:
    if yards is None or yards <= 2:
        return "short"
    return "mid" if yards <= 9 else "long"


@dataclass(frozen=True)
class DefenseCredit:
    group: str
    points: float
    named: bool  # True when a defender in the play text was resolved to this group


def _named_group(play, resolver: PositionResolver) -> str | None:
    text = play.play_text or ""
    t = play.play_type
    patterns = ()
    if t == "Sack":
        patterns = (SACK_BY_RE, SACK_PAREN_RE)
    elif t in ("Pass Interception Return", "Interception", "Interception Return Touchdown"):
        patterns = (INTERCEPTED_RE, INTERCEPTED_BY_RE, INTERCEPTED_BY_S26_RE)
    elif t in FUMBLE_TYPES:
        patterns = (FORCED_BY_RE, FORCED_BY_S26_RE)
    elif t == "Pass Incompletion":
        patterns = (BROKEN_UP_RE, BROKEN_UP_S26_RE)
    elif t in SAFETY_TYPES:
        patterns = (SAFETY_RE,)
    for rx in patterns:
        m = rx.search(text)
        if m:
            group = resolver.resolve(play.defense, m["n"])
            if group:
                return group
    return None


def _share_key(play) -> str:
    t = play.play_type
    if t == "Sack" or t in SAFETY_TYPES:
        return "sack"
    if t in ("Pass Interception Return", "Interception", "Interception Return Touchdown"):
        return "interception"
    if t in FUMBLE_TYPES:
        return "forced_fumble"
    if t == "Pass Incompletion":
        return "incompletion"
    band = yards_band(play.yards_gained)
    return f"run_{band}" if t in RUN_TYPES else f"catch_{band}"


def raw_credits(play, resolver: PositionResolver, shares: DefenseShares,
                profile: TeamDefenseProfile | None = None) -> dict[str, tuple[float, bool]] | None:
    """{group: (uncentered credit, named)} for one play, or None if it carries
    no defensive value."""
    if play.play_type not in DEFENSIVE_TYPES or play.ppa is None:
        return None
    value = -play.ppa
    group = _named_group(play, resolver)
    if group:
        return {g: (value if g == group else 0.0, True) for g in GROUPS}
    key = _share_key(play)
    split = dict(shares.shares(key))
    mult = profile.multipliers(play.defense, key) if profile else None
    if mult:
        tilted = {g: split[g] * mult[g] for g in GROUPS}
        norm = sum(tilted.values())
        if norm > 0:
            split = {g: w / norm for g, w in tilted.items()}
    return {g: (value * split[g], False) for g in GROUPS}


def attribute_defense(play, resolver: PositionResolver, shares: DefenseShares,
                      profile: TeamDefenseProfile | None = None) -> list[DefenseCredit] | None:
    """Centered credit for each group on one play, or None if the play carries
    none. Every group gets a row (a group's baseline applies to every defended
    play, including ones it had no part in)."""
    raw = raw_credits(play, resolver, shares, profile)
    if raw is None:
        return None
    return [DefenseCredit(g, c - shares.group_baseline_per_play[g], named) for g, (c, named) in raw.items()]
