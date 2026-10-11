"""
NHL off-day credit banking (docs/hockey_spec.md 5.2).

What banks: a rostered player's positive credit on a day when his fantasy team has no head-to-head game
(a series day with is_matchup_day = False). The credit is scored upstream and is only STORED here, keyed so that
ingesting it twice is a no-op; nothing in this module values or recomputes a point.

Rules:
  * A credit can be assigned to one game. A game takes at most one credit per team (database-enforced).
  * The assigned amount is capped at BANK_CAP (1.50); the credit keeps its full earned value for display,
    and `applied_points < credit_points` means it was capped.
  * Eligible games for a credit: the team's own SCHEDULED matchup-day games after the source date and within
    WINDOW_DAYS of it (the rest of the ongoing series, or the next 7 days), not in the past, and not already
    holding a credit for that team.
  * Assignment is final: no reassignment, no un-assignment.
  * Fallback: a credit still AVAILABLE FALLBACK_HOURS after it was banked is assigned automatically to the
    immediately next eligible game; if there is none it EXPIRES. process_fallback is a plain function with an
    injected clock - nothing here schedules itself.

Recommendation: games are compared on projected margin, opponent strength and win-probability swing, using only
inputs that exist. No projection source exists yet, so projected margin and swing are always reported as
unavailable (never estimated) unless a caller supplies projections. With nothing usable the recommendation is
the explicit neutral one: the earliest eligible game, marked confidence NEUTRAL.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass, field
from typing import Mapping

from sqlalchemy.orm import Session

from fantasy_sports.platform import series_schedule, series_standings
from fantasy_sports.platform.db.models.core import (
    BankedCredit, LeagueSeries, RosterEntry, SeriesDailyGame, Team,
)

BANK_CAP = 1.50
FALLBACK_HOURS = 24
WINDOW_DAYS = 7
MIN_OPPONENT_GAMES = 3  # final games an opponent needs before its record is treated as a strength signal
EPS = 1e-9


class BankingError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def fallback_at(credit: BankedCredit) -> datetime.datetime:
    return credit.created_at + datetime.timedelta(hours=FALLBACK_HOURS)


def window_end(credit: BankedCredit) -> datetime.date:
    return credit.source_calendar_date + datetime.timedelta(days=WINDOW_DAYS)


def is_capped(credit: BankedCredit) -> bool:
    return credit.credit_points > BANK_CAP + EPS


def is_off_day(db: Session, team_id: int, date: datetime.date) -> bool:
    """True when the team has a series day on `date` that is not a matchup day."""
    return db.query(SeriesDailyGame).filter(
        SeriesDailyGame.calendar_date == date, SeriesDailyGame.is_matchup_day.is_(False),
        (SeriesDailyGame.home_team_id == team_id) | (SeriesDailyGame.away_team_id == team_id)).count() > 0


def ingest_credit(db: Session, *, team_id: int, asset_id: int, source_date: datetime.date, points: float,
                  now: datetime.datetime, source_key: str | None = None) -> tuple[BankedCredit, bool]:
    """Store an off-day credit once. Returns (credit, created). The first write wins: ingesting the same
    source_key again (default key: player + date) returns the stored credit untouched, even if `points`
    differs, so a re-run can never change an assigned credit. Flushes, does not commit. Raises BankingError."""
    team = db.get(Team, team_id)
    if team is None:
        raise BankingError("Team not found", "NOT_FOUND")
    if not series_schedule.league_is_nhl(db, team.league_id):
        raise BankingError("Banking is only available for NHL leagues", "NOT_NHL")
    if not math.isfinite(points) or points <= EPS:
        raise BankingError("Only a positive, finite credit can be banked", "NON_POSITIVE")
    if db.query(RosterEntry).filter_by(team_id=team_id, draftable_asset_id=asset_id).first() is None:
        raise BankingError("That player is not on this team's roster", "NOT_ROSTERED")
    if not is_off_day(db, team_id, source_date):
        raise BankingError("That date is not an off day for this team", "NOT_OFF_DAY")
    key = source_key or f"asset:{asset_id}:{source_date.isoformat()}"
    existing = db.query(BankedCredit).filter_by(league_id=team.league_id, source_key=key).first()
    if existing is not None:
        return existing, False
    credit = BankedCredit(league_id=team.league_id, team_id=team_id, draftable_asset_id=asset_id,
                          source_calendar_date=source_date, source_key=key, credit_points=float(points),
                          status="AVAILABLE", created_at=now)
    db.add(credit)
    db.flush()
    return credit, True


def _team_games(db: Session, credit: BankedCredit) -> list[SeriesDailyGame]:
    return (db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == credit.league_id,
                    (SeriesDailyGame.home_team_id == credit.team_id) | (SeriesDailyGame.away_team_id == credit.team_id))
            .order_by(SeriesDailyGame.calendar_date, SeriesDailyGame.id).all())


def _taken(db: Session, credit: BankedCredit) -> set[int]:
    return {c.assigned_daily_game_id for c in db.query(BankedCredit).filter(
        BankedCredit.team_id == credit.team_id, BankedCredit.status == "ASSIGNED")}


def ineligible_reason(game: SeriesDailyGame, credit: BankedCredit, now: datetime.datetime, taken: set[int]) -> str | None:
    """Why `game` cannot take `credit` (a REASON_TEXT code), or None. Date rules come before status rules so the
    message names the most basic problem."""
    if credit.team_id not in (game.home_team_id, game.away_team_id):
        return "NOT_YOUR_GAME"
    if not game.is_matchup_day:
        return "OFF_DAY"
    if game.calendar_date <= credit.source_calendar_date:
        return "BEFORE_CREDIT"
    if game.calendar_date > window_end(credit):
        return "OUTSIDE_WINDOW"
    if game.calendar_date < now.date():
        return "IN_THE_PAST"
    if game.status != "SCHEDULED":
        return "GAME_STARTED"
    if game.id in taken:
        return "GAME_HAS_CREDIT"
    return None


def eligible_games(db: Session, credit: BankedCredit, now: datetime.datetime) -> list[SeriesDailyGame]:
    taken = _taken(db, credit)
    return [g for g in _team_games(db, credit) if ineligible_reason(g, credit, now, taken) is None]


def _assign(db: Session, credit: BankedCredit, game: SeriesDailyGame, now: datetime.datetime, mode: str) -> None:
    credit.status = "ASSIGNED"
    credit.assigned_daily_game_id = game.id
    credit.applied_points = min(credit.credit_points, BANK_CAP)
    credit.assignment_mode = mode
    credit.assigned_at = now
    db.flush()


def assign(db: Session, credit: BankedCredit, daily_game_id: int, now: datetime.datetime,
           mode: str = "MANUAL") -> BankedCredit:
    """Assign an AVAILABLE credit to one eligible game. Flushes, does not commit. Raises BankingError."""
    if credit.status == "ASSIGNED":
        raise BankingError("This credit is already assigned and cannot be moved", "ALREADY_ASSIGNED")
    if credit.status == "EXPIRED":
        raise BankingError("This credit has expired", "EXPIRED")
    game = db.get(SeriesDailyGame, daily_game_id)
    if game is None:
        raise BankingError("Game not found", "NOT_FOUND")
    reason = ineligible_reason(game, credit, now, _taken(db, credit))
    if reason is not None:
        raise BankingError(REASON_TEXT[reason], reason)
    _assign(db, credit, game, now, mode)
    return credit


REASON_TEXT = {
    "NOT_YOUR_GAME": "That is not one of your team's games",
    "OFF_DAY": "That day is an off day, so there is no game to bank into",
    "GAME_STARTED": "That game has already started",
    "BEFORE_CREDIT": "Credits can only go to games after the day they were earned",
    "OUTSIDE_WINDOW": f"Credits can only go to games within {WINDOW_DAYS} days of the day they were earned",
    "IN_THE_PAST": "That game is in the past",
    "GAME_HAS_CREDIT": "That game already has a banked credit (one per game)",
}


# --------------------------------------------------------------------------------------------------------
# Recommendation
# --------------------------------------------------------------------------------------------------------

@dataclass
class GameOption:
    daily_game_id: int
    calendar_date: datetime.date
    opponent_id: int
    opponent_name: str
    home_away: str
    game_score: int
    opponent_strength: dict | None = None   # {"win_pct", "games", "goal_diff_per_game"} or None
    projected_margin: float | None = None
    win_probability_swing: float | None = None
    rank: int = 0


@dataclass
class Recommendation:
    credit_id: int
    recommended_game_id: int | None
    confidence: str                         # FULL / PARTIAL / NEUTRAL / NONE
    basis: str                              # how the order was decided
    reasons: list[str] = field(default_factory=list)
    inputs_available: dict = field(default_factory=dict)
    options: list[GameOption] = field(default_factory=list)  # best first


def _strength(standings: dict[int, series_standings.TeamStanding], team_id: int) -> dict | None:
    row = standings.get(team_id)
    if row is None:
        return None
    games = row.wins + row.losses + row.ties
    if games < MIN_OPPONENT_GAMES:
        return None
    return {"win_pct": round((row.wins + 0.5 * row.ties) / games, 4), "games": games,
            "goal_diff_per_game": round(row.goal_diff / games, 4)}


def recommend(db: Session, credit: BankedCredit, now: datetime.datetime,
              projections: Mapping[int, Mapping[str, float]] | None = None) -> Recommendation:
    """Explainable, deterministic recommendation among the eligible games. `projections` maps a daily game id
    to {"projected_margin": ..., "win_probability_swing": ...} and is only ever supplied by a caller that has a
    real projection source; the HTTP API supplies none."""
    games = eligible_games(db, credit, now)
    names = {t.id: t.name for t in db.query(Team).filter_by(league_id=credit.league_id)}
    team_ids, series, all_games = series_standings.load_league(db, credit.league_id)
    standings = {r.team_id: r for r in series_standings.compute_standings(
        team_ids, series, all_games, credit.source_calendar_date)}
    options = []
    for g in games:
        opp = g.away_team_id if g.home_team_id == credit.team_id else g.home_team_id
        proj = (projections or {}).get(g.id, {})
        options.append(GameOption(
            daily_game_id=g.id, calendar_date=g.calendar_date, opponent_id=opp, opponent_name=names.get(opp, "?"),
            home_away="home" if g.home_team_id == credit.team_id else "away", game_score=g.game_score_rating,
            opponent_strength=_strength(standings, opp), projected_margin=proj.get("projected_margin"),
            win_probability_swing=proj.get("win_probability_swing")))

    avail = {
        "projected_margin": bool(options) and all(o.projected_margin is not None for o in options),
        "opponent_strength": bool(options) and all(o.opponent_strength is not None for o in options),
        "win_probability_swing": bool(options) and all(o.win_probability_swing is not None for o in options),
    }
    rec = Recommendation(credit_id=credit.id, recommended_game_id=None, confidence="NONE", basis="no_eligible_game",
                         inputs_available=avail)
    if not options:
        rec.reasons.append("There is no eligible game left to bank this credit into.")
        return rec

    if avail["win_probability_swing"]:
        key, basis = (lambda o: (-o.win_probability_swing, o.calendar_date, o.daily_game_id)), "win_probability_swing"
        rec.reasons.append("Ranked by the largest win-probability swing the credit would add.")
    elif avail["opponent_strength"]:
        key, basis = (lambda o: (-o.opponent_strength["win_pct"], -o.opponent_strength["goal_diff_per_game"],
                                 o.calendar_date, o.daily_game_id)), "opponent_strength"
        rec.reasons.append("Ranked by opponent strength (win rate, then goal difference per game) because the "
                           "credit helps most against a tougher opponent.")
    else:
        key, basis = (lambda o: (o.calendar_date, o.daily_game_id)), "earliest_game"
        rec.reasons.append("Neutral recommendation: the earliest eligible game.")
    options.sort(key=key)
    for i, o in enumerate(options, start=1):
        o.rank = i
    rec.options = options
    rec.basis = basis
    rec.recommended_game_id = options[0].daily_game_id
    rec.confidence = "FULL" if all(avail.values()) else "PARTIAL" if basis != "earliest_game" else "NEUTRAL"
    missing = [k.replace("_", " ") for k, v in avail.items() if not v]
    if missing:
        rec.reasons.append("Not available, so not used: " + ", ".join(missing) + ". No probability is estimated without them.")
    if is_capped(credit):
        rec.reasons.append(f"Only {BANK_CAP:.2f} of the {credit.credit_points:.2f} earned points can be applied to one game.")
    return rec


# --------------------------------------------------------------------------------------------------------
# 24-hour fallback
# --------------------------------------------------------------------------------------------------------

@dataclass
class FallbackResult:
    processed_at: datetime.datetime
    assigned: list[tuple[int, int]] = field(default_factory=list)   # (credit id, daily game id)
    expired: list[int] = field(default_factory=list)
    waiting: int = 0                                                  # AVAILABLE but not yet 24 hours old


def process_fallback(db: Session, now: datetime.datetime, league_id: int | None = None) -> FallbackResult:
    """Auto-assign every AVAILABLE credit that is FALLBACK_HOURS old to its immediately next eligible game, or
    expire it when none is left. Idempotent: assigned and expired credits are never touched again, and the
    order is fixed (banked time, then id). Flushes, does not commit."""
    result = FallbackResult(processed_at=now)
    query = db.query(BankedCredit).filter(BankedCredit.status == "AVAILABLE")
    if league_id is not None:
        query = query.filter(BankedCredit.league_id == league_id)
    for credit in query.order_by(BankedCredit.created_at, BankedCredit.id).all():
        if now < fallback_at(credit):
            result.waiting += 1
            continue
        games = eligible_games(db, credit, now)
        if games:
            _assign(db, credit, games[0], now, "AUTO")
            result.assigned.append((credit.id, games[0].id))
        else:
            credit.status = "EXPIRED"
            credit.status_reason = "NO_ELIGIBLE_GAME"
            credit.expired_at = now
            db.flush()
            result.expired.append(credit.id)
    return result


def assigned_for_game(db: Session, daily_game_id: int) -> list[BankedCredit]:
    """The credits applied to a game, for the scoring step that will include them (not wired into the
    simulator yet)."""
    return db.query(BankedCredit).filter_by(assigned_daily_game_id=daily_game_id, status="ASSIGNED").order_by(
        BankedCredit.id).all()
