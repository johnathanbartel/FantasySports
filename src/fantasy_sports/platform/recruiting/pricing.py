"""
Hidden per-player prices ("thresholds") for a recruiting draft, and the player
"worth" the AI bids by.

THE RULES (commissioner):
  * A price is a player's cost for the WHOLE recruitment: an average drafted player
    costs $10 a week of recruiting ($200 over 20 weeks) and an average elite player
    $15 a week ($300). Budgets ($300 / $200 / $100 a week) are sized to that: the
    league's total money is exactly (number of roster spots) x $200.
  * "Average" means the players who actually get drafted: the best players at each
    position in the numbers a league's rosters use (DRAFT_DEMAND_PER_TEAM), not the whole
    pool of thousands.
  * Every league draws its own prices: each player has an EXPECTED price, and a
    league's price is a random draw around it, so a player costs different amounts
    in different leagues but averages out to his expected price. The draws have mean
    exactly 1, with occasional cheap elite players and expensive role players.
  * Position-fair: worth is value OVER REPLACEMENT, so a top tight end is worth about
    what a top WR is, though a QB scores far more raw points.
  * Players with no stat line for the last completed season are valued from their
    recruiting rating (see PlayerRecruitRating): what a young player of that rating
    and position typically produced stands in for the missing stats.
  * Defense groups (DL/LB/DB) have no fantasy points to value them by yet, so each is
    priced like the average drafted player until the EPA scoring model can value them.

HOW A PRICE IS BUILT
  points   last season's ESPN-standard points, or the recruiting-rating prior if none
  worth    max(points - replacement, 0) + BELOW_REPLACEMENT_WEIGHT * reference * min(points/replacement, 1)
           (replacement = the points of the last starter-level player at his position,
           counting FLEX; reference = the average replacement level across positions. Below
           replacement a player keeps a little worth in proportion to how close to replacement
           he is, on one scale for every position, so depth isn't free and QBs' bigger raw
           point totals don't make backup QBs look valuable)
  expected worth ** gamma, scaled so the drafted players average $200 and the top
           ELITE_SHARE of them average $300 (gamma is solved to hit that ratio). Below the
           drafted pool the price tapers away in proportion to worth (a walk-on isn't
           priced like a starter), floor $10
  price    expected x a mean-1 random draw
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from fantasy_sports.platform.calendar import last_points_season
from fantasy_sports.platform.db.models.core import DraftableAsset, Player
from fantasy_sports.sports.football.college.season_scoring import SCORING_SYSTEM
from fantasy_sports.sports.football.models import PlayerRecruitRating, PlayerSeasonPoints, WeeklyPlayerScore

ELITE_MEAN_PER_WEEK = 15      # x number of weeks = the average elite player's price
ROLE_MEAN_PER_WEEK = 10       # x number of weeks = the average drafted player's price
ELITE_SHARE = 0.20            # the top 20% of drafted-caliber players are "elite"
MIN_PRICE = 10
ELITE_BARGAIN_CHANCE = 0.08   # elite players who cost very little
ROLE_OVERPRICE_CHANCE = 0.06  # non-elite players who cost a ton
NOISE_SIGMA = 0.35
NOISE_CLIP = 2.5              # the ordinary price draw is limited to +/- this many sigmas (no $1,100 stars)
BELOW_REPLACEMENT_WEIGHT = 0.10
# How many players at each position the drafted pool holds per team, for a 20-man roster (scaled by
# roster size): the rest of a roster is the team's DL/LB/DB groups. This defines "the players who
# actually get drafted" that the $200 / $300 averages are measured over.
DRAFT_DEMAND_PER_TEAM = {"QB": 1.5, "RB": 4.5, "WR": 6.0, "TE": 3.0, "PK": 1.5}
TAPER_EXPONENT = 1.0          # below the drafted pool, price falls off in proportion to worth

# How many players at each position a league uses: starters per team, plus a third of the
# team's FLEX slot for each of RB/WR/TE. Replacement level = the points of the player at
# rank (teams x this), i.e. the last one a league would field.
REPLACEMENT_SLOTS = {"QB": 1.0, "RB": 2 + 1 / 3, "WR": 2 + 1 / 3, "TE": 1 + 1 / 3, "PK": 1.0}
PRIOR_CLASS_YEARS = (2, 3)    # the young players whose production defines the rating prior
MIN_CELL = 10                 # fewest players a rating x position cell needs to be trusted


def season_points_by_asset(db: Session, season: int | None = None) -> dict[int, float]:
    """asset_id -> the player's ESPN-standard points for the season (default: the newest
    season ingested - the last completed one). Only players with a stat line appear;
    defense groups never do."""
    # A sport-only deployment can legitimately omit the college-football
    # season-points table. NHL snake drafts still need an alphabetical board
    # until hockey projections are available, so treat that schema shape the
    # same as a database with no prior-season points rather than raising 500.
    if not inspect(db.get_bind()).has_table(PlayerSeasonPoints.__tablename__):
        return {}
    season = season if season is not None else last_points_season(db)
    if season is None:
        return {}
    by_player = {
        r.player_id: r.points
        for r in db.query(PlayerSeasonPoints).filter_by(season=season, scoring_system=SCORING_SYSTEM)
    }
    return {
        a.id: by_player[a.player_id]
        for a in db.query(DraftableAsset).filter(DraftableAsset.player_id.isnot(None))
        if a.player_id in by_player
    }


def _rating_bucket(rating: float | None) -> str:
    if rating is None:
        return "unrated"
    for cut, name in ((0.98, "98"), (0.95, "95"), (0.90, "90"), (0.85, "85"), (0.80, "80")):
        if rating >= cut:
            return name
    return "low"


def rating_priors(db: Session, rows: list, known: dict[int, float]):
    """(rating bucket, position) -> the mean last-season points of current young players (class
    year 2-3, i.e. last year's freshmen and sophomores) with that rating and position, zeros
    included for those who didn't play. Thin cells fall back to the bucket alone, then to all."""
    ratings = {r.player_id: r for r in db.query(PlayerRecruitRating)}
    cell: dict[tuple, list[float]] = defaultdict(list)
    for asset_id, position, player_id in rows:
        r = ratings.get(player_id)
        if r is None or r.class_year not in PRIOR_CLASS_YEARS:
            continue
        cell[(_rating_bucket(r.rating), position)].append(known.get(asset_id, 0.0))
    by_bucket: dict[str, list[float]] = defaultdict(list)
    everything: list[float] = []
    for (bucket, _), v in cell.items():
        by_bucket[bucket] += v
        everything += v
    overall = statistics.fmean(everything) if everything else 0.0

    def prior(rating: float | None, position: str) -> float:
        bucket = _rating_bucket(rating)
        v = cell.get((bucket, position), [])
        if len(v) >= MIN_CELL:
            return statistics.fmean(v)
        if len(by_bucket.get(bucket, [])) >= MIN_CELL:
            return statistics.fmean(by_bucket[bucket])
        return overall

    return prior, ratings


def worth_by_asset(db: Session, teams: int = 12, roster_size: int = 20) -> dict[int, float]:
    """asset_id -> worth: value over replacement (see the module docstring). Covers every
    active player and defense group. `teams` and `roster_size` size the replacement level
    and the drafted pool."""
    rows = [
        (a.id, p.position, p.id)
        for a, p in db.query(DraftableAsset, Player).join(Player, Player.id == DraftableAsset.player_id)
        .filter(Player.active.is_(True))
    ]
    known = season_points_by_asset(db)
    if not known:
        # No season points ingested (a bare database): fall back to the platform's weekly scores.
        weekly: dict[int, float] = defaultdict(float)
        for s in db.query(WeeklyPlayerScore):
            weekly[s.draftable_asset_id] += s.points
        known = dict(weekly)
    prior, ratings = rating_priors(db, rows, known)

    points: dict[int, float] = {}
    for asset_id, position, player_id in rows:
        if asset_id in known:
            points[asset_id] = max(0.0, known[asset_id])
        else:
            r = ratings.get(player_id)
            points[asset_id] = prior(r.rating if r else None, position)

    by_position: dict[str, list[float]] = defaultdict(list)
    for asset_id, position, _ in rows:
        by_position[position].append(points[asset_id])
    replacement: dict[str, float] = {}
    for position, values in by_position.items():
        values.sort(reverse=True)
        rank = max(1, math.ceil(REPLACEMENT_SLOTS.get(position, 1.0) * teams))
        replacement[position] = values[rank - 1] if len(values) >= rank else 0.0

    reference = statistics.fmean(replacement.values()) if replacement else 0.0
    worth = {
        asset_id: max(points[asset_id] - replacement[position], 0.0)
        + (BELOW_REPLACEMENT_WEIGHT * reference * min(points[asset_id] / replacement[position], 1.0)
           if replacement[position] > 0 else 0.0)
        for asset_id, position, _ in rows
    }
    # Defense groups: no fantasy points yet, so each is worth the average drafted player.
    drafted = drafted_pool([a for a, _, _ in rows], worth, {a: p for a, p, _ in rows}, teams, roster_size)
    group_worth = statistics.fmean(worth[a] for a in drafted) if drafted else 0.0
    for a in db.query(DraftableAsset).filter(DraftableAsset.defense_group_id.isnot(None)):
        worth[a.id] = group_worth
    return worth


def drafted_pool(asset_ids, worth: dict[int, float], position: dict[int, str], teams: int, roster_size: int) -> list[int]:
    """The players who actually get drafted: at each position, the best (teams x demand per team,
    scaled to the roster size) by worth."""
    out: list[int] = []
    for pos, per_team in DRAFT_DEMAND_PER_TEAM.items():
        ranked = sorted((a for a in asset_ids if position.get(a) == pos), key=lambda a: worth.get(a, 0.0), reverse=True)
        out += ranked[: max(1, round(teams * per_team * roster_size / 20))]
    return out


def value_by_asset(db: Session, teams: int = 12, roster_size: int = 20) -> dict[int, float]:
    """How good a player is, for the AI's bidding and free-agent picks: his worth."""
    return worth_by_asset(db, teams, roster_size)


def price_inputs(db: Session, asset_ids: list[int], teams: int, roster_size: int):
    """(worth, drafted pool, defense-group ids) for a league: everything expected_prices and
    generate_thresholds need beyond the random generator."""
    worth = worth_by_asset(db, teams, roster_size)
    position = {
        a.id: p.position
        for a, p in db.query(DraftableAsset, Player).join(Player, Player.id == DraftableAsset.player_id)
        .filter(DraftableAsset.id.in_(asset_ids))
    }
    groups = {a.id for a in db.query(DraftableAsset).filter(
        DraftableAsset.id.in_(asset_ids), DraftableAsset.defense_group_id.isnot(None))}
    return worth, drafted_pool(asset_ids, worth, position, teams, roster_size), groups


def expected_prices(
    asset_ids: list[int], worth: dict[int, float], weeks: int, drafted: list[int],
    groups: frozenset | set = frozenset(),
) -> tuple[dict[int, float], set[int]]:
    """asset_id -> expected price for the whole recruitment, and the set of elite assets. The
    curve is worth**gamma, scaled so the `drafted` pool averages ROLE_MEAN_PER_WEEK*weeks and
    its top ELITE_SHARE (by worth) averages ELITE_MEAN_PER_WEEK*weeks."""
    drafted = sorted(drafted, key=lambda a: worth.get(a, 0.0), reverse=True)
    elite = set(drafted[: max(1, round(ELITE_SHARE * len(drafted)))])
    w = {a: max(worth.get(a, 0.0), 1e-6) for a in asset_ids}
    target_ratio = ELITE_MEAN_PER_WEEK / ROLE_MEAN_PER_WEEK

    def ratio(gamma: float) -> float:
        top = statistics.fmean(w[a] ** gamma for a in elite)
        return top / statistics.fmean(w[a] ** gamma for a in drafted)

    lo, hi = 0.01, 3.0
    if drafted and ratio(hi) > target_ratio:
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if ratio(mid) < target_ratio else (lo, mid)
        gamma = (lo + hi) / 2
    else:
        gamma = 1.0
    scale = (ROLE_MEAN_PER_WEEK * weeks) / statistics.fmean(w[a] ** gamma for a in drafted) if drafted else 0.0
    cutoff = min((w[a] for a in drafted), default=1.0)      # the worth of the last drafted player

    def taper(a: int) -> float:
        return 1.0 if w[a] >= cutoff else (w[a] / cutoff) ** TAPER_EXPONENT

    return {a: max(float(MIN_PRICE), scale * w[a] ** gamma * taper(a)) for a in asset_ids}, elite


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# E[exp(sigma * Z)] for a standard normal Z clipped to +/- NOISE_CLIP: dividing the draw by it makes
# the clipped lognormal average exactly 1.
_CLIPPED_LOGNORMAL_MEAN = (
    math.exp(NOISE_SIGMA ** 2 / 2)
    * (_phi(NOISE_CLIP - NOISE_SIGMA) - _phi(-NOISE_CLIP - NOISE_SIGMA))
    / (_phi(NOISE_CLIP) - _phi(-NOISE_CLIP))
)


def _noise(rng: random.Random, elite: bool) -> float:
    """A random multiplier with mean exactly 1. Elite players occasionally come very cheap,
    other players occasionally very expensive; the ordinary draw is a clipped lognormal scaled
    so the whole mixture averages 1."""
    z = rng.gauss(0, 1)
    while abs(z) > NOISE_CLIP:
        z = rng.gauss(0, 1)
    lognormal = math.exp(NOISE_SIGMA * z) / _CLIPPED_LOGNORMAL_MEAN
    if elite:
        p, low, high = ELITE_BARGAIN_CHANCE, 0.15, 0.35
    else:
        p, low, high = ROLE_OVERPRICE_CHANCE, 2.0, 3.0
    if rng.random() < p:
        return rng.uniform(low, high)
    return lognormal * (1 - p * (low + high) / 2) / (1 - p)


def generate_thresholds(
    asset_ids: list[int], worth: dict[int, float], rng: random.Random, weeks: int, drafted: list[int],
    groups: frozenset | set = frozenset(),
) -> dict[int, tuple[int, bool]]:
    """asset_id -> (this league's price for the whole recruitment, is_elite): each player's
    expected price times a mean-1 random draw, so it varies league to league but averages
    to the expected price."""
    expected, elite = expected_prices(asset_ids, worth, weeks, drafted, groups)
    return {
        a: (max(MIN_PRICE, round(expected[a] * _noise(rng, a in elite))), a in elite)
        for a in asset_ids
    }
