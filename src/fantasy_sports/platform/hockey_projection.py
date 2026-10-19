"""
NHL projected points: age-adjusted production per game x projected games played.

    projected points = projected points per game x projected games played

POINTS PER GAME. Each of the player's last HISTORY_SEASONS completed seasons gives a points-per-game rate (his stored xG
credit divided by the games he played). Each rate is taken to its peak-age equivalent by dividing out the aging curve at the
age he was that season, the seasons are averaged with Marcel-style weights (SEASON_WEIGHTS, newest first, times games played,
so a full season counts more than a cup of coffee) after adding RATE_BALLAST weighted games of his position's pool average
(regression to the mean - a hot half season is mostly luck), and the result is moved to the age he is this season by
multiplying the aging curve back in.

GAMES PLAYED (the playing time). The same weights over his games played in those seasons, regressed toward his position's
typical games with GP_BALLAST weight units, capped at TEAM_GAMES. A game counts as played when the player has ice time in the
stored boxscore or earned any credit in it, so a defenseman who skated 18 quiet minutes is not skipped. Role (line, power
play) is not modelled separately: it is already inside his per-game production.

AGING CURVES. AGE_CURVES gives each age's output as a share of the player's peak, per position group (F = C and wingers,
D, G). The SHAPE follows the published NHL delta-method findings (skaters peak in their mid-20s and decline a few percent a
year after about 27, defensemen peak slightly later and decline slower, goalies peak late and fall off after 32), but the
numbers are hand-set priors, NOT fitted to this platform's data. fit_aging_curves() is the place to replace them once
enough seasons are ingested. An unknown birth date means no adjustment (factor 1.0).

NO HISTORY. A player with no stored games in those seasons (a rookie, a player back from overseas) is projected from the pool:
NO_HISTORY_RATE_FACTOR of his position's average rate and NO_HISTORY_GP_FACTOR of its typical games, source "prior". With
no data at all in the pool nobody is projected (None), never a made-up number.

Only COMPLETED seasons are read (the current, part-played season is left out), so a projection does not move while the season
is played and a draft grade made from it stays put. Injuries and trades are not modelled.

Pure helpers first; the database loader at the bottom. Nothing here scores or attributes a game.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from fantasy_sports.platform.draft_stats import nhl_season_year
from fantasy_sports.platform.pecking_order import GROUP_BY_POSITION
from fantasy_sports.platform.player_age import age_on

TEAM_GAMES = 82
HISTORY_SEASONS = 3
SEASON_WEIGHTS = (5.0, 4.0, 3.0)       # newest season first
RATE_BALLAST = 120.0                    # weighted games of pool average mixed into every rate
GP_BALLAST = 2.0                        # weight units of pool-typical games mixed into every games projection
MIN_POOL_GAMES = 20                     # a season with fewer games doesn't help define a position's typical games
NO_HISTORY_RATE_FACTOR = 0.7
NO_HISTORY_GP_FACTOR = 0.6

CURVE_GROUPS = ("F", "D", "G")

# age -> output as a share of the player's peak (1.0). Hand-set priors; see the module docstring.
AGE_CURVES: dict[str, dict[int, float]] = {
    "F": {18: 0.72, 19: 0.80, 20: 0.87, 21: 0.92, 22: 0.95, 23: 0.975, 24: 0.99, 25: 1.0, 26: 1.0, 27: 0.99, 28: 0.97,
          29: 0.95, 30: 0.925, 31: 0.90, 32: 0.87, 33: 0.83, 34: 0.79, 35: 0.75, 36: 0.70, 37: 0.65, 38: 0.60, 39: 0.55, 40: 0.50},
    "D": {18: 0.62, 19: 0.70, 20: 0.78, 21: 0.85, 22: 0.91, 23: 0.95, 24: 0.98, 25: 1.0, 26: 1.0, 27: 1.0, 28: 0.985,
          29: 0.965, 30: 0.94, 31: 0.915, 32: 0.89, 33: 0.855, 34: 0.82, 35: 0.78, 36: 0.73, 37: 0.68, 38: 0.63, 39: 0.58, 40: 0.53},
    "G": {19: 0.62, 20: 0.70, 21: 0.76, 22: 0.82, 23: 0.88, 24: 0.93, 25: 0.96, 26: 0.98, 27: 0.99, 28: 1.0, 29: 1.0, 30: 1.0,
          31: 0.99, 32: 0.975, 33: 0.95, 34: 0.92, 35: 0.88, 36: 0.84, 37: 0.79, 38: 0.74, 39: 0.69, 40: 0.64},
}


def curve_group(position: str | None) -> str | None:
    """F / D / G for a player's position; None when he isn't an NHL skater or goalie."""
    group = GROUP_BY_POSITION.get((position or "").upper())
    return None if group is None else ("F" if group in ("C", "W") else group)


def aging_factor(curve_grp: str, age: int | None) -> float:
    """Output at `age` as a share of peak. Ages beyond the table take its nearest end; unknown age is 1.0."""
    if age is None:
        return 1.0
    curve = AGE_CURVES[curve_grp]
    return curve[min(max(age, min(curve)), max(curve))]


def age_in_season(birth_date: datetime.date | None, season: int) -> int | None:
    """His age when the season that began in the fall of `season` started (October 1)."""
    return age_on(birth_date, datetime.date(season, 10, 1))


@dataclass
class SeasonLine:
    season: int
    games: int
    points: float


@dataclass
class PoolPriors:
    """Per curve group: the pool's peak-equivalent points per game and typical games played."""
    rate: dict[str, float] = field(default_factory=dict)
    gp: dict[str, float] = field(default_factory=dict)


@dataclass
class Projection:
    asset_id: int
    group: str                  # pecking-order group: C / W / D / G
    age: int | None
    games: float                # projected games played
    rate: float                 # projected points per game
    points: float               # games x rate
    source: str                 # "history" or "prior"
    seasons_used: int


def pool_priors(players: list[tuple[str, datetime.date | None, list[SeasonLine]]], target: int) -> PoolPriors:
    """Position averages from the pool: games-weighted peak-equivalent points per game, and the mean games of seasons with at
    least MIN_POOL_GAMES. `players` holds (curve group, birth date, season lines)."""
    rate_num: dict[str, float] = defaultdict(float)
    rate_den: dict[str, float] = defaultdict(float)
    gp_sum: dict[str, float] = defaultdict(float)
    gp_n: dict[str, int] = defaultdict(int)
    for grp, born, lines in players:
        for ln in lines:
            if not 1 <= target - ln.season <= HISTORY_SEASONS or ln.games <= 0:
                continue
            peak = (ln.points / ln.games) / aging_factor(grp, age_in_season(born, ln.season))
            rate_num[grp] += peak * ln.games
            rate_den[grp] += ln.games
            if ln.games >= MIN_POOL_GAMES:
                gp_sum[grp] += ln.games
                gp_n[grp] += 1
    priors = PoolPriors()
    for grp in CURVE_GROUPS:
        if rate_den[grp] > 0:
            priors.rate[grp] = rate_num[grp] / rate_den[grp]
        if gp_n[grp]:
            priors.gp[grp] = gp_sum[grp] / gp_n[grp]
    return priors


def project_player(asset_id: int, position: str | None, birth_date: datetime.date | None, lines: list[SeasonLine],
                   priors: PoolPriors, target: int) -> Projection | None:
    """One player's projection for the season that began in the fall of `target`, or None when he has no position group or
    the pool has no prior for it."""
    grp = curve_group(position)
    if grp is None or grp not in priors.rate or grp not in priors.gp:
        return None
    pos_group = GROUP_BY_POSITION[(position or "").upper()]
    age = age_in_season(birth_date, target)
    used = [ln for ln in lines if 1 <= target - ln.season <= HISTORY_SEASONS and ln.games > 0]
    if not used:
        games = priors.gp[grp] * NO_HISTORY_GP_FACTOR
        rate = priors.rate[grp] * NO_HISTORY_RATE_FACTOR * aging_factor(grp, age)
        return Projection(asset_id, pos_group, age, games, rate, games * rate, "prior", 0)
    num = den = gp_num = gp_den = 0.0
    for ln in used:
        w = SEASON_WEIGHTS[target - ln.season - 1]
        peak = (ln.points / ln.games) / aging_factor(grp, age_in_season(birth_date, ln.season))
        num += w * ln.games * peak
        den += w * ln.games
        gp_num += w * ln.games
        gp_den += w
    rate_peak = (num + RATE_BALLAST * priors.rate[grp]) / (den + RATE_BALLAST)
    games = min(float(TEAM_GAMES), (gp_num + GP_BALLAST * priors.gp[grp]) / (gp_den + GP_BALLAST))
    rate = rate_peak * aging_factor(grp, age)
    return Projection(asset_id, pos_group, age, games, rate, games * rate, "history", len(used))


def fit_aging_curves(*_args, **_kwargs):
    """Placeholder for fitting AGE_CURVES from ingested seasons (the delta method: the average change in per-game output
    between ages a and a+1 for players who played both seasons, chained from the peak). Not built: it needs more than the
    three seasons the platform has, and the hand-set priors stand until then."""
    raise NotImplementedError("aging curves are hand-set priors; see the module docstring")


# ---- lineup value of a set of projected players (shared by draft grades and power rankings) ------------------------------

STARTERS = {"C": 1, "W": 2, "D": 2, "G": 1}


@dataclass
class LineupProjection:
    total: float                        # starters + depth cover
    starters_points: float
    depth_points: float
    starters: list[Projection]


def lineup_projection(projections: list[Projection]) -> LineupProjection:
    """The best lineup the players can field (the pecking order's 1 C, 2 W, 2 D, 1 G) over a full season. A starter's own
    points already leave out the games he is projected to miss; the next players in his group cover those games at their own
    per-game rate, each only for as many games as he is projected to play."""
    by_group: dict[str, list[Projection]] = defaultdict(list)
    for p in projections:
        by_group[p.group].append(p)
    starters: list[Projection] = []
    starters_points = depth_points = 0.0
    for grp, need in STARTERS.items():
        ranked = sorted(by_group.get(grp, []), key=lambda p: p.points, reverse=True)
        chosen, bench = ranked[:need], ranked[need:]
        starters += chosen
        starters_points += sum(p.points for p in chosen)
        missed = sum(TEAM_GAMES - p.games for p in chosen) + (need - len(chosen)) * TEAM_GAMES     # an empty slot misses all
        for b in sorted(bench, key=lambda p: p.rate, reverse=True):
            if missed <= 0:
                break
            take = min(missed, b.games)
            depth_points += take * b.rate
            missed -= take
    return LineupProjection(starters_points + depth_points, starters_points, depth_points, starters)


# ---- database loader ------------------------------------------------------------------------------------------------

def _has(db: Session, model) -> bool:
    return inspect(db.get_bind()).has_table(model.__tablename__)


def load_history(db: Session, target: int | None = None) -> dict[int, list[SeasonLine]]:
    """asset id -> a SeasonLine per completed season in the history window, read from stored hockey credits. Assets with
    nothing stored are absent. Tables a deployment lacks are skipped."""
    from fantasy_sports.platform.db.models.core import Game, Week
    from fantasy_sports.sports.hockey.models import EventCredit, GameEvent, PlayerGameToi, Shot, ShotCredit

    target = target if target is not None else nhl_season_year()
    seasons = list(range(target - HISTORY_SEASONS, target))
    points: dict[tuple[int, int], float] = defaultdict(float)
    games: dict[tuple[int, int], set[int]] = defaultdict(set)
    if _has(db, ShotCredit) and _has(db, Shot):
        for a, season, g, pts in (db.query(ShotCredit.draftable_asset_id, Week.season_year, Shot.game_id, func.sum(ShotCredit.credit_points))
                                  .join(Shot, Shot.id == ShotCredit.shot_id).join(Game, Game.id == Shot.game_id)
                                  .join(Week, Week.id == Game.week_id).filter(Week.season_year.in_(seasons))
                                  .group_by(ShotCredit.draftable_asset_id, Week.season_year, Shot.game_id)):
            points[a, season] += float(pts or 0)
            games[a, season].add(g)
    if _has(db, EventCredit) and _has(db, GameEvent):
        for a, season, g, pts in (db.query(EventCredit.draftable_asset_id, Week.season_year, GameEvent.game_id, func.sum(EventCredit.credit_points))
                                  .join(GameEvent, GameEvent.id == EventCredit.event_id).join(Game, Game.id == GameEvent.game_id)
                                  .join(Week, Week.id == Game.week_id)
                                  .filter(EventCredit.role == "blocker", Week.season_year.in_(seasons))
                                  .group_by(EventCredit.draftable_asset_id, Week.season_year, GameEvent.game_id)):
            points[a, season] += float(pts or 0)
            games[a, season].add(g)
    if _has(db, PlayerGameToi):
        for a, season, g in (db.query(PlayerGameToi.draftable_asset_id, Week.season_year, PlayerGameToi.game_id)
                             .join(Game, Game.id == PlayerGameToi.game_id).join(Week, Week.id == Game.week_id)
                             .filter(PlayerGameToi.toi_seconds > 0, Week.season_year.in_(seasons))):
            games[a, season].add(g)
    out: dict[int, list[SeasonLine]] = defaultdict(list)
    for (a, season), gs in games.items():
        out[a].append(SeasonLine(season, len(gs), points.get((a, season), 0.0)))
    return out


def project_pool(db: Session, target: int | None = None) -> dict[int, Projection]:
    """asset id -> Projection for every active NHL skater and goalie. Empty when the pool has no stored history."""
    from fantasy_sports.platform.db.models.core import DraftableAsset, Player, Universe

    target = target if target is not None else nhl_season_year()
    history = load_history(db, target)
    rows = [(a.id, p.position, p.birth_date)
            for a, p in (db.query(DraftableAsset, Player).join(Player, Player.id == DraftableAsset.player_id)
                         .join(Universe, Universe.id == Player.universe_id)
                         .filter(Universe.code == "nhl", Player.active.is_(True)))]
    pool = [(grp, born, history.get(aid, [])) for aid, pos, born in rows if (grp := curve_group(pos)) is not None]
    priors = pool_priors(pool, target)
    out: dict[int, Projection] = {}
    for aid, pos, born in rows:
        proj = project_player(aid, pos, born, history.get(aid, []), priors, target)
        if proj is not None:
            out[aid] = proj
    return out
