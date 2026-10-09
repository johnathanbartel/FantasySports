"""NHL xG credit attribution (hockey README, "Credit attribution").

Pure functions over shot and event records - no database access - so the same
code scores a game for validation and for persistence.

Rules:

- Goal, no assist data (v1 / games without events): shooter gets 1.0.
- Goal with NHL play-by-play assists: the goal's 1.0 is split, never added
  to. Shooter 1 - xG (finishing share); the xG share (the value of the chance)
  goes to the assists: one assist takes all of it, two assists split it 2:1
  primary:secondary. A goal's credits always sum to exactly 1.0.
- A non-goal shot earns the shooter nothing, so a team's shooter+assist total
  equals the goals it scored in regulation and overtime by construction.
- Goalie: Goals Saved Above Expected, xG - goal, on every shot faced.
  Empty-net shots are excluded (the team's own decision, not a goalie
  failure).
- Blocked shots (play-by-play only; MoneyPuck has no blocked attempts): the
  blocker gets the estimated xG of the attempt he blocked. This is extra
  credit on top of the validated goal total and is reported on its own line.
- Hits: a small fitted constant per hit (HIT_CREDIT), also reported
  separately.
- Shootout goals are not attributed (the shot data has none).

Possession-chain credit (takeaway/faceoff before a shot) and rebound-creation
credit are deliberately not included: the NHL publishes no pass events and
the owner has not chosen slice sizes (see FS-015). Counts of takeaways and
giveaways are reported as advanced metrics only.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

# Blocked-shot events record where the BLOCK happened (zone "D" from the
# blocker's side; mean 26 ft from the net vs 34 ft for unblocked attempts), not
# where the attempt was taken. Blocked attempts therefore come from farther out
# than the recorded point; the estimator adds this assumed gap. It is an
# assumption (the source has no shooter location), checked only for
# plausibility in the validation output.
BLOCK_ATTEMPT_OFFSET_FT = 18.0

# Goals-equivalent credit per hit, fitted in scripts/fit_event_weights.py
# (63,017 hits: net xG swing -0.0031 goals, t = -14.5, so hits earn nothing).
HIT_CREDIT = 0.0

# Multiplier on block credit. 1.0 = the estimated xG the block prevented.
BLOCK_CREDIT_SCALE = 1.0

ASSIST_SPLIT = (2 / 3, 1 / 3)  # primary, secondary share of the xG portion
GOAL_TIME_TOLERANCE = 2  # seconds between MoneyPuck and NHL clocks


@dataclass(frozen=True)
class ShotRecord:
    shot_id: int
    shooting_team_id: int
    goalie_team_id: int
    shooter_asset_id: int | None
    goalie_asset_id: int | None
    pre_shot_xg: float
    is_goal: bool
    is_empty_net: bool
    game_seconds: int | None = None


@dataclass(frozen=True)
class EventRecord:
    event_id: int
    event_type: str
    team_id: int | None
    game_seconds: int | None
    actor_ref: str | None = None
    secondary_ref: str | None = None
    tertiary_ref: str | None = None
    actor_asset_id: int | None = None
    secondary_asset_id: int | None = None
    tertiary_asset_id: int | None = None
    x: float | None = None
    y: float | None = None


@dataclass(frozen=True)
class ShotCreditRow:
    shot_id: int
    asset_id: int
    role: str  # shooter / primary_assist / secondary_assist / goalie
    points: float


@dataclass(frozen=True)
class EventCreditRow:
    event_id: int
    asset_id: int
    role: str  # blocker / hitter
    points: float


def attempt_distance(x: float, y: float) -> float:
    """Feet from the attempt to the goal it was aimed at (net at |x| = 89)."""
    return math.hypot(89 - abs(x), y)


class BlockXgEstimator:
    """Estimated xG of a blocked attempt: the league's goal rate on unblocked
    attempts (goals / (goals + shots on goal + misses)) from the attempt's
    distance band. Fitted from play-by-play events, so it needs no extra
    source. The attempt distance is the block's distance from the net plus
    BLOCK_ATTEMPT_OFFSET_FT, because the event records the blocker's spot."""

    BAND = 5.0
    MAX_BAND = 14  # 70 ft and beyond share one band

    def __init__(self, rates: dict[int, float], attempts: dict[int, int]):
        self.rates = rates
        self.attempts = attempts

    @classmethod
    def fit(cls, events) -> "BlockXgEstimator":
        goals: dict[int, int] = defaultdict(int)
        attempts: dict[int, int] = defaultdict(int)
        for e in events:
            if e.event_type not in ("goal", "shot-on-goal", "missed-shot") or e.x is None or e.y is None:
                continue
            band = cls._band(attempt_distance(e.x, e.y))
            attempts[band] += 1
            if e.event_type == "goal":
                goals[band] += 1
        rates = {band: goals[band] / n for band, n in attempts.items() if n}
        return cls(rates, dict(attempts))

    @classmethod
    def _band(cls, distance: float) -> int:
        return min(int(distance // cls.BAND), cls.MAX_BAND)

    def xg(self, x: float | None, y: float | None) -> float:
        if x is None or y is None or not self.rates:
            return 0.0
        band = self._band(attempt_distance(x, y) + BLOCK_ATTEMPT_OFFSET_FT)
        if band in self.rates:
            return self.rates[band]
        return self.rates[max(b for b in self.rates if b <= band)] if any(b <= band for b in self.rates) \
            else self.rates[min(self.rates)]


@dataclass
class GameResult:
    goals_by_team: dict = field(default_factory=lambda: defaultdict(float))  # model total (shooter + assist credit)
    xg_faced_by_team: dict = field(default_factory=lambda: defaultdict(float))  # non-empty-net xG against
    gsax_by_team: dict = field(default_factory=lambda: defaultdict(float))  # team goalie credit incl. unnamed goalies
    empty_net_goals_by_team: dict = field(default_factory=lambda: defaultdict(int))  # goals into this team's empty net
    credits: list = field(default_factory=list)  # ShotCreditRow, named players only
    event_credits: list = field(default_factory=list)  # EventCreditRow (blocks, hits)
    shooter_credit_by_asset: dict = field(default_factory=lambda: defaultdict(float))
    assist_credit_by_asset: dict = field(default_factory=lambda: defaultdict(float))
    gsax_by_asset: dict = field(default_factory=lambda: defaultdict(float))
    block_credit_by_asset: dict = field(default_factory=lambda: defaultdict(float))
    hit_credit_by_asset: dict = field(default_factory=lambda: defaultdict(float))
    ixg_by_asset: dict = field(default_factory=lambda: defaultdict(float))  # xG of the player's own unblocked attempts
    unnamed_shooter_points: float = 0.0
    unnamed_assist_points: float = 0.0
    unnamed_goalie_points: float = 0.0
    goals_with_assists: int = 0
    assist_slots_credited: int = 0  # primary + secondary assists given credit rows or unnamed credit
    goals_unmatched: int = 0  # goals with events present but no matching goal event
    goals_total: int = 0
    block_credit_by_team: dict = field(default_factory=lambda: defaultdict(float))
    blocks_by_team: dict = field(default_factory=lambda: defaultdict(int))
    hits_by_team: dict = field(default_factory=lambda: defaultdict(int))
    takeaways_by_asset: dict = field(default_factory=lambda: defaultdict(int))
    giveaways_by_asset: dict = field(default_factory=lambda: defaultdict(int))

    def model_total(self, team_id: int) -> float:
        return self.goals_by_team.get(team_id, 0.0)

    def model_goals_allowed(self, team_id: int) -> float:
        """Goals the team's goalies allowed, rebuilt from the credits alone:
        xG faced minus goalie GSAx (empty net excluded)."""
        return self.xg_faced_by_team.get(team_id, 0.0) - self.gsax_by_team.get(team_id, 0.0)


def _match_goal_events(shots, events):
    """Pair each goal shot with its NHL goal event (same team, same clock
    within tolerance, same scorer when both are known)."""
    goal_events = [e for e in events if e.event_type == "goal"]
    used: set[int] = set()
    pairing: dict[int, EventRecord] = {}
    for shot in sorted((s for s in shots if s.is_goal), key=lambda s: (s.game_seconds is None, s.game_seconds or 0)):
        best = None
        for e in goal_events:
            if e.event_id in used or e.team_id != shot.shooting_team_id:
                continue
            if shot.game_seconds is None or e.game_seconds is None:
                continue
            if abs(e.game_seconds - shot.game_seconds) > GOAL_TIME_TOLERANCE:
                continue
            same_scorer = (
                shot.shooter_asset_id is not None and e.actor_asset_id == shot.shooter_asset_id
            )
            key = (not same_scorer, abs(e.game_seconds - shot.game_seconds))
            if best is None or key < best[0]:
                best = (key, e)
        if best is not None:
            used.add(best[1].event_id)
            pairing[shot.shot_id] = best[1]
    return pairing


def process_game(shots, events=None, block_estimator: BlockXgEstimator | None = None,
                 teams: tuple[int, int] | None = None) -> GameResult:
    result = GameResult()
    shots = list(shots)
    events = list(events) if events else []
    pairing = _match_goal_events(shots, events) if events else {}

    for shot in shots:
        goal = 1.0 if shot.is_goal else 0.0
        result.goals_by_team.setdefault(shot.shooting_team_id, 0.0)
        if shot.shooter_asset_id is not None and not shot.is_empty_net:
            result.ixg_by_asset[shot.shooter_asset_id] += shot.pre_shot_xg
        if goal:
            result.goals_total += 1
            result.goals_by_team[shot.shooting_team_id] += 1.0
            _credit_goal(result, shot, pairing.get(shot.shot_id), have_events=bool(events))

        if shot.is_empty_net:
            if goal:
                result.empty_net_goals_by_team[shot.goalie_team_id] += 1
            continue
        gsax = shot.pre_shot_xg - goal
        result.xg_faced_by_team[shot.goalie_team_id] += shot.pre_shot_xg
        result.gsax_by_team[shot.goalie_team_id] += gsax
        if shot.goalie_asset_id is None:
            result.unnamed_goalie_points += gsax
        else:
            result.credits.append(ShotCreditRow(shot.shot_id, shot.goalie_asset_id, "goalie", gsax))
            result.gsax_by_asset[shot.goalie_asset_id] += gsax

    for e in events:
        if e.event_type == "blocked-shot":
            # actor = blocker, secondary = shooter; the event is owned by the
            # shooting team, so the blocker's team is the other one.
            xg = (block_estimator.xg(e.x, e.y) if block_estimator else 0.0) * BLOCK_CREDIT_SCALE
            blocker_team = next((t for t in (teams or ()) if t != e.team_id), None)
            result.blocks_by_team[blocker_team] += 1
            result.block_credit_by_team[blocker_team] += xg
            if e.actor_asset_id is not None and xg:
                result.event_credits.append(EventCreditRow(e.event_id, e.actor_asset_id, "blocker", xg))
                result.block_credit_by_asset[e.actor_asset_id] += xg
        elif e.event_type == "hit":
            result.hits_by_team[e.team_id] += 1
            if e.actor_asset_id is not None and HIT_CREDIT:
                result.event_credits.append(EventCreditRow(e.event_id, e.actor_asset_id, "hitter", HIT_CREDIT))
                result.hit_credit_by_asset[e.actor_asset_id] += HIT_CREDIT
        elif e.event_type == "takeaway" and e.actor_asset_id is not None:
            result.takeaways_by_asset[e.actor_asset_id] += 1
        elif e.event_type == "giveaway" and e.actor_asset_id is not None:
            result.giveaways_by_asset[e.actor_asset_id] += 1
    return result


def _credit_goal(result: GameResult, shot: ShotRecord, event: EventRecord | None, have_events: bool) -> None:
    if have_events and event is None:
        result.goals_unmatched += 1
    assists = []  # (ref present, asset id)
    if event is not None:
        if event.secondary_ref:
            assists.append(event.secondary_asset_id)
        if event.tertiary_ref:
            assists.append(event.tertiary_asset_id)
    if not assists:
        _give(result, shot, shot.shooter_asset_id, "shooter", 1.0)
        return
    result.goals_with_assists += 1
    result.assist_slots_credited += len(assists)
    xg = min(max(shot.pre_shot_xg, 0.0), 1.0)
    _give(result, shot, shot.shooter_asset_id, "shooter", 1.0 - xg)
    shares = (xg,) if len(assists) == 1 else (xg * ASSIST_SPLIT[0], xg * ASSIST_SPLIT[1])
    for rank, (asset_id, share) in enumerate(zip(assists, shares)):
        _give(result, shot, asset_id, ("primary_assist", "secondary_assist")[rank], share)


def _give(result: GameResult, shot: ShotRecord, asset_id: int | None, role: str, points: float) -> None:
    if asset_id is None:
        if role == "shooter":
            result.unnamed_shooter_points += points
        else:
            result.unnamed_assist_points += points
        return
    result.credits.append(ShotCreditRow(shot.shot_id, asset_id, role, points))
    if role == "shooter":
        result.shooter_credit_by_asset[asset_id] += points
    else:
        result.assist_credit_by_asset[asset_id] += points


# FS-013 entry point, kept for callers that have shots only.
def process_shots(shots) -> GameResult:
    return process_game(shots)
