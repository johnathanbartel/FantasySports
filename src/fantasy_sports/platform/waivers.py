"""
Waivers: a dropped player is on waivers for 24 hours, and teams claim him in waiver order.

  * A drop (platform/free_agency.drop_player) puts the player on waivers until the drop time + 24 hours. While he is
    on waivers nobody can simply sign him; a team places a claim instead (WaiverClaim). AI teams do not claim, and they
    leave waiver players alone until they clear.
  * When the 24 hours end, the pending claim from the team that is first in the waiver order signs him (price and
    roster rules as for any signing; a claim that cannot be filled is skipped for the next team's). With no claims he
    simply becomes a free agent. Nothing is scheduled: `process` applies whatever has fallen due, each claim resolved
    at the instant its window ended, so running it often or once after a week gives the same result.
  * Waiver order is the REVERSE of goal differential (the worst differential claims first). It resets at midnight
    Pacific as Saturday turns to Sunday, the start of a new fantasy week (Sunday - Saturday): the order is rebuilt from
    the standings through the latest week whose games are ALL final. Ties go to the team that is lower in the
    standings, then to the team drafted later. If no week is fully complete yet the order is the reverse of the first
    round of the draft (the team that picked last claims first).
  * Winning a claim sends that team to the back of the order until the next reset.

Goal differential exists for NHL leagues (series standings). Other leagues have no weekly goal differential here, so
their order is the draft fallback all season - PROVISIONAL, until their standings are wired in.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from fantasy_sports.platform import series_standings
from fantasy_sports.platform.db.models.core import (
    Draft,
    DraftPick,
    FreeAgencyTransaction,
    League,
    LeagueSeries,
    RosterEntry,
    SeriesDailyGame,
    Team,
    WaiverClaim,
)

WAIVER_HOURS = 24
WAIVER = "waiver"                      # FreeAgencyTransaction.reason for a signing won off waivers
PACIFIC = "America/Los_Angeles"
_PST = datetime.timedelta(hours=-8)    # used only when the platform has no tz database (Windows without tzdata)
BASIS_GOAL_DIFF, BASIS_DRAFT = "goal_diff", "draft"


# ---- the weekly reset (midnight Pacific, Saturday night -> Sunday) --------------------------------------------------
def _to_local(utc: datetime.datetime) -> datetime.datetime:
    try:
        return utc.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo(PACIFIC)).replace(tzinfo=None)
    except Exception:
        return utc + _PST


def _to_utc(local: datetime.datetime) -> datetime.datetime:
    try:
        return local.replace(tzinfo=ZoneInfo(PACIFIC)).astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except Exception:
        return local - _PST


def _sunday_on_or_before(d: datetime.date) -> datetime.date:
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)       # Python: Monday = 0 ... Sunday = 6


def last_reset(now: datetime.datetime) -> datetime.datetime:
    """The latest Sunday 00:00 Pacific at or before `now`, as naive UTC."""
    sunday = _sunday_on_or_before(_to_local(now).date())
    return _to_utc(datetime.datetime.combine(sunday, datetime.time()))


def next_reset(now: datetime.datetime) -> datetime.datetime:
    sunday = _sunday_on_or_before(_to_local(last_reset(now)).date()) + datetime.timedelta(days=7)
    return _to_utc(datetime.datetime.combine(sunday, datetime.time()))


# ---- who is on waivers --------------------------------------------------------------------------------------------
def waiver_until(db: Session, league_id: int, now: datetime.datetime) -> dict[int, datetime.datetime]:
    """asset id -> the moment his waiver period ends, for every player dropped in the last 24 hours and not signed since."""
    drops = db.query(FreeAgencyTransaction).filter(
        FreeAgencyTransaction.league_id == league_id, FreeAgencyTransaction.kind == "drop",
        FreeAgencyTransaction.created_at > now - datetime.timedelta(hours=WAIVER_HOURS),
    ).order_by(FreeAgencyTransaction.id).all()
    latest = {t.draftable_asset_id: t for t in drops}
    if not latest:
        return {}
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league_id)]
    rostered = {r.draftable_asset_id for r in db.query(RosterEntry).filter(
        RosterEntry.team_id.in_(team_ids or [0]), RosterEntry.draftable_asset_id.in_(list(latest)))}
    until = {a: t.created_at + datetime.timedelta(hours=WAIVER_HOURS) for a, t in latest.items() if a not in rostered}
    return {a: u for a, u in until.items() if u > now}


def dropped_by(db: Session, league_id: int, asset_ids: list[int]) -> dict[int, int]:
    """asset id -> the team that most recently dropped him."""
    rows = db.query(FreeAgencyTransaction).filter(
        FreeAgencyTransaction.league_id == league_id, FreeAgencyTransaction.kind == "drop",
        FreeAgencyTransaction.draftable_asset_id.in_(asset_ids or [0])).order_by(FreeAgencyTransaction.id).all()
    return {t.draftable_asset_id: t.team_id for t in rows}


# ---- the order ----------------------------------------------------------------------------------------------------
@dataclass
class WaiverOrder:
    basis: str                                     # BASIS_GOAL_DIFF or BASIS_DRAFT
    teams: list[int]                               # team ids, first claim first
    goal_diff: dict[int, int] = field(default_factory=dict)
    reset_at: datetime.datetime | None = None
    as_of: datetime.date | None = None             # the Saturday the goal differentials run through
    moved_back: list[int] = field(default_factory=list)     # teams that won a claim since the reset (a subset of teams)


def _draft_reverse(db: Session, league_id: int, team_ids: list[int]) -> list[int]:
    """Reverse of the first round of the draft; teams with no first-round pick (auction, recruiting) go last, newest first."""
    draft = db.query(Draft).filter_by(league_id=league_id).order_by(Draft.id).first()
    first_round = []
    if draft is not None:
        first_round = [p.team_id for p in db.query(DraftPick).filter_by(draft_id=draft.id, round=1).order_by(DraftPick.pick_number)]
    order = [t for t in reversed(first_round) if t in team_ids]
    order += sorted((t for t in team_ids if t not in order), reverse=True)
    return order


def _complete_weeks(db: Session, league_id: int) -> list[datetime.date]:
    """Sundays of the fantasy weeks (Sunday - Saturday) in which every game is final, oldest first."""
    rows = (db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league_id).all())
    weeks: dict[datetime.date, bool] = {}
    for g in rows:
        sunday = _sunday_on_or_before(g.calendar_date_end or g.calendar_date)
        weeks[sunday] = weeks.get(sunday, True) and g.status == "FINAL"
    return sorted(w for w, complete in weeks.items() if complete)


def _base_order(db: Session, league: League, reset: datetime.datetime) -> WaiverOrder:
    team_ids = sorted(t.id for t in db.query(Team.id).filter_by(league_id=league.id))
    fallback = _draft_reverse(db, league.id, team_ids)
    out = WaiverOrder(BASIS_DRAFT, fallback, reset_at=reset)
    if league.series_length_days is None:
        return out
    reset_day = _to_local(reset).date()
    done = [w for w in _complete_weeks(db, league.id) if w + datetime.timedelta(days=7) <= reset_day]
    if not done:
        return out
    saturday = done[-1] + datetime.timedelta(days=6)
    ids, series, games = series_standings.load_league(db, league.id)
    rows = series_standings.compute_standings(ids, series, games, as_of=saturday)
    tie = {t: i for i, t in enumerate(fallback)}                       # earlier in the draft-reverse list = drafted later
    rows.sort(key=lambda r: (r.goal_diff, -r.rank, tie.get(r.team_id, 0)))
    return WaiverOrder(BASIS_GOAL_DIFF, [r.team_id for r in rows], {r.team_id: r.goal_diff for r in rows}, reset, saturday)


def waiver_order(db: Session, league_id: int, at: datetime.datetime | None = None) -> WaiverOrder:
    """The order at an instant: this week's base order, with teams that have won a claim since the reset moved to the back
    (the one who won longest ago ahead of the one who won most recently)."""
    at = at or datetime.datetime.utcnow()
    league = db.get(League, league_id)
    reset = last_reset(at)
    order = _base_order(db, league, reset)
    wins = db.query(WaiverClaim).filter(
        WaiverClaim.league_id == league_id, WaiverClaim.status == "won",
        WaiverClaim.resolved_at >= reset, WaiverClaim.resolved_at <= at).order_by(WaiverClaim.resolved_at, WaiverClaim.id).all()
    last_win = {c.team_id: i for i, c in enumerate(wins)}
    if last_win:
        moved = sorted(last_win, key=last_win.get)
        order.teams = [t for t in order.teams if t not in last_win] + [t for t in moved if t in order.teams]
        order.moved_back = moved
    return order


# ---- claims -------------------------------------------------------------------------------------------------------
def place_claim(db: Session, team: Team, asset_id: int, now: datetime.datetime | None = None) -> WaiverClaim:
    """Raises ValueError with a message meant for the user."""
    from fantasy_sports.platform import free_agency

    now = now or datetime.datetime.utcnow()
    process(db, team.league_id, now)
    if not free_agency.is_open(db, team.league_id):
        raise ValueError("Free agency opens when the draft ends")
    until = waiver_until(db, team.league_id, now).get(asset_id)
    if until is None:
        raise ValueError("That player isn't on waivers - sign him from the free-agent list")
    existing = db.query(WaiverClaim).filter_by(team_id=team.id, draftable_asset_id=asset_id, available_at=until, status="pending").first()
    if existing is not None:
        raise ValueError("You already have a claim on that player")
    claim = WaiverClaim(league_id=team.league_id, team_id=team.id, draftable_asset_id=asset_id, available_at=until,
                        created_at=now, status="pending")
    db.add(claim)
    db.flush()
    return claim


def cancel_claim(db: Session, team: Team, asset_id: int, now: datetime.datetime | None = None) -> None:
    now = now or datetime.datetime.utcnow()
    process(db, team.league_id, now)
    claim = db.query(WaiverClaim).filter_by(team_id=team.id, draftable_asset_id=asset_id, status="pending").first()
    if claim is None:
        raise ValueError("You have no pending claim on that player")
    claim.status, claim.resolved_at = "cancelled", now
    db.flush()


def pending_claims(db: Session, team_id: int) -> set[int]:
    return {c.draftable_asset_id for c in db.query(WaiverClaim).filter_by(team_id=team_id, status="pending")}


def process(db: Session, league_id: int, now: datetime.datetime | None = None) -> int:
    """Resolves every claim whose waiver period has ended, in the order the periods ended. Flushes, does not commit.
    Returns how many players were signed off waivers."""
    from fantasy_sports.platform import free_agency

    now = now or datetime.datetime.utcnow()
    due = db.query(WaiverClaim).filter(
        WaiverClaim.league_id == league_id, WaiverClaim.status == "pending", WaiverClaim.available_at <= now,
    ).order_by(WaiverClaim.available_at, WaiverClaim.id).all()
    if not due:
        return 0
    league = db.get(League, league_id)
    groups: dict[tuple[datetime.datetime, int], list[WaiverClaim]] = {}
    for c in due:
        groups.setdefault((c.available_at, c.draftable_asset_id), []).append(c)
    signed = 0
    for (at, asset_id), claims in groups.items():
        rank = {t: i for i, t in enumerate(waiver_order(db, league_id, at).teams)}
        rostered = free_agency.rostered_ids(db, league_id)
        winner = None
        for c in sorted(claims, key=lambda c: (rank.get(c.team_id, len(rank)), c.id)):
            c.resolved_at = at
            team = db.get(Team, c.team_id)
            problem = "He was signed by another team" if asset_id in rostered else free_agency.signing_problem(db, league, team, asset_id)
            if winner is not None:
                c.status = "lost"
            elif problem:
                c.status, c.note = "failed", problem[:200]
            else:
                free_agency._sign(db, team, asset_id, free_agency.asking_prices(db, league)[asset_id], WAIVER, at)
                c.status, winner = "won", c
                signed += 1
            db.flush()
    return signed
