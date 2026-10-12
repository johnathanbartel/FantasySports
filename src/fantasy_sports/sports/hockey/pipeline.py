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


# ---------------------------------------------------------------------------------------------------------------
# Fantasy windows (docs/hockey_spec.md section 5). Pure functions: no database, no clock, no network.
#
# A fantasy week runs Sunday to Saturday and has three windows - 1: Sun-Mon-Tue, 2: Wed-Thu, 3: Fri-Sat - each a
# standalone game. A player contributes AT MOST ONE real game's score to a window:
#   * one real game in the window: that game;
#   * several: the "Best Game", the highest fantasy score (ties: the earlier game, then the lower game id);
#   * a manager may move a SURPLUS game (a window with two or more of the player's games) to a LATER window (2 or 3)
#     in which the player has no real game of his own; the other game stays and still counts where it was.
# A game's score is the sum of the credit lines it earned the player; a goalie's can be negative, and Best Game is
# still the maximum (the least bad), never skipped.
# ---------------------------------------------------------------------------------------------------------------
import datetime  # noqa: E402

WINDOWS = (1, 2, 3)
# Python weekday(): Monday = 0 ... Sunday = 6
WINDOW_WEEKDAYS = {1: (6, 0, 1), 2: (2, 3), 3: (4, 5)}
FLEX_TARGETS = (2, 3)


class WindowError(ValueError):
    """A window assignment that breaks the rules (the message is meant for the manager)."""


def week_start(day: datetime.date) -> datetime.date:
    """The Sunday that begins the fantasy week containing `day`."""
    return day - datetime.timedelta(days=(day.weekday() + 1) % 7)


def window_of(day: datetime.date) -> tuple[datetime.date, int]:
    """(week start, window 1-3) for a calendar date."""
    weekday = day.weekday()
    for index, weekdays in WINDOW_WEEKDAYS.items():
        if weekday in weekdays:
            return week_start(day), index
    raise AssertionError("every weekday belongs to a window")


def window_dates(start: datetime.date, window: int) -> list[datetime.date]:
    """The calendar dates of a window, earliest first (`start` is the week's Sunday)."""
    if window not in WINDOW_WEEKDAYS:
        raise WindowError(f"Window must be 1, 2 or 3, got {window}")
    days = [start + datetime.timedelta(days=i) for i in range(7)]
    return [d for d in days if d.weekday() in WINDOW_WEEKDAYS[window]]


@dataclass(frozen=True)
class PlayerGame:
    """One player's score in one real game."""
    asset_id: int
    game_id: int
    game_date: datetime.date
    points: float
    # True for a game his team played in which he earned no credit at all. It still OCCUPIES its window (a surplus game
    # cannot be moved onto it) but is never the Best Game while a game with credit competes: not playing is not a
    # way to dodge a bad night.
    scheduled_only: bool = False

    @property
    def window(self) -> tuple[datetime.date, int]:
        return window_of(self.game_date)


@dataclass(frozen=True)
class WindowAssignment:
    """A manager's choice to count a surplus game in a later window of the same week."""
    asset_id: int
    game_id: int
    target_window: int


@dataclass(frozen=True)
class WindowScore:
    """What one player contributes to one window."""
    asset_id: int
    week_start: datetime.date
    window: int
    game_id: int | None          # the game that counts (None = the player has no game in this window)
    points: float                # 0.0 when no game counts
    source: str                  # none / single / best_game / reassigned
    candidates: tuple[int, ...]  # the games that competed for this window
    dropped: tuple[int, ...]     # candidates that lost to the Best Game


def player_game_points(credits, scheduled=()) -> list[PlayerGame]:
    """Sums credit lines into per-game scores. `credits` yields (asset_id, game_id, game_date, points). `scheduled`
    yields (asset_id, game_id, game_date) for every game a player's team played; one with no credit line becomes a
    scheduled-only game (0.0). Exact (math.fsum) and sorted, so the same credits always give the same games."""
    parts: dict[tuple[int, int, datetime.date], list[float]] = defaultdict(list)
    for asset_id, game_id, game_date, points in credits:
        parts[(asset_id, game_id, game_date)].append(points)
    games = [PlayerGame(a, g, d, math.fsum(p)) for (a, g, d), p in parts.items()]
    have = {(g.asset_id, g.game_id) for g in games}
    games += [PlayerGame(a, g, d, 0.0, scheduled_only=True) for a, g, d in set(scheduled) if (a, g) not in have]
    return sorted(games, key=lambda g: (g.asset_id, g.game_date, g.game_id))


def _best(games: list[PlayerGame]) -> PlayerGame:
    played = [g for g in games if not g.scheduled_only] or games      # a game he earned credit in beats one he did not
    return max(played, key=lambda g: (g.points, -g.game_date.toordinal(), -g.game_id))


def resolve_player_week(games, assignments=()) -> tuple[WindowScore, WindowScore, WindowScore]:
    """One player's three windows for one week, from all his real games that week."""
    games = sorted(games, key=lambda g: (g.game_date, g.game_id))
    if not games:
        raise WindowError("No games to resolve")
    asset_id = games[0].asset_id
    start = games[0].window[0]
    if any(g.asset_id != asset_id or g.window[0] != start for g in games):
        raise WindowError("resolve_player_week takes one player's games from one fantasy week")
    if len({g.game_id for g in games}) != len(games):
        raise WindowError("A game appears twice for the same player")
    native = {w: [g for g in games if g.window[1] == w] for w in WINDOWS}
    by_id = {g.game_id: g for g in games}

    moved: dict[int, int] = {}            # game id -> target window
    target_taken: dict[int, int] = {}     # target window -> game id
    for a in assignments:
        if a.asset_id != asset_id:
            continue
        game = by_id.get(a.game_id)
        if game is None:
            raise WindowError(f"Game {a.game_id} is not one of this player's games this week")
        if a.target_window not in FLEX_TARGETS:
            raise WindowError("A surplus game can only be moved to Window 2 or Window 3")
        source = game.window[1]
        if a.target_window <= source:
            raise WindowError(f"A game from Window {source} can only move to a later window, not Window {a.target_window}")
        if len(native[source]) < 2:
            raise WindowError(f"Window {source} has only one game for this player: there is no surplus game to move")
        if native[a.target_window]:
            raise WindowError(f"The player already has a game in Window {a.target_window}: a surplus game can only go to an open window")
        if a.game_id in moved:
            raise WindowError("That game is already moved")
        if a.target_window in target_taken:
            raise WindowError(f"Window {a.target_window} already has a moved game: one game per window")
        moved[a.game_id] = a.target_window
        target_taken[a.target_window] = a.game_id
    for w in WINDOWS:
        if native[w] and all(g.game_id in moved for g in native[w]):
            raise WindowError(f"At least one game must stay in Window {w}")

    out = []
    for w in WINDOWS:
        if w in target_taken:
            game = by_id[target_taken[w]]
            out.append(WindowScore(asset_id, start, w, game.game_id, game.points, "reassigned", (game.game_id,), ()))
            continue
        pool = [g for g in native[w] if g.game_id not in moved]
        if not pool:
            out.append(WindowScore(asset_id, start, w, None, 0.0, "none", (), ()))
            continue
        best = _best(pool)
        out.append(WindowScore(
            asset_id, start, w, best.game_id, best.points, "single" if len(pool) == 1 else "best_game",
            tuple(g.game_id for g in pool), tuple(g.game_id for g in pool if g.game_id != best.game_id)))
    assert_one_game_per_window(out)
    return tuple(out)  # type: ignore[return-value]


def resolve_windows(player_games, assignments=()) -> dict[tuple[int, datetime.date], tuple[WindowScore, ...]]:
    """Resolves every player-week in `player_games`. Keys are (asset_id, week_start)."""
    grouped: dict[tuple[int, datetime.date], list[PlayerGame]] = defaultdict(list)
    for g in player_games:
        grouped[(g.asset_id, g.window[0])].append(g)
    assignments = tuple(assignments)
    for a in assignments:
        if not any(k[0] == a.asset_id and any(g.game_id == a.game_id for g in v) for k, v in grouped.items()):
            raise WindowError(f"Player {a.asset_id} has no game {a.game_id} to move")
    return {key: resolve_player_week(games, [a for a in assignments if a.asset_id == key[0]])
            for key, games in sorted(grouped.items())}


def assert_one_game_per_window(scores) -> None:
    """The invariant: a window holds at most one game per player, and no game counts in two windows."""
    counted = [s.game_id for s in scores if s.game_id is not None]
    if len(counted) != len(set(counted)):
        raise WindowError("A game counts in more than one window")
    if len({s.window for s in scores}) != len(scores):
        raise WindowError("A window is listed twice")


def window_totals(resolved) -> dict[tuple[datetime.date, int], dict[int, float]]:
    """(week start, window) -> {asset_id: points that count}: the numbers a team's window score is built from."""
    out: dict[tuple[datetime.date, int], dict[int, float]] = defaultdict(dict)
    for (asset_id, start), scores in resolved.items():
        for s in scores:
            if s.game_id is not None:
                out[(start, s.window)][asset_id] = s.points
    return dict(out)
