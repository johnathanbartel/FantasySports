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
  * With the league's post_draft_waivers setting on (default off), the same 24 hours apply to every undrafted player
    once the draft ends: nobody can sign them outright, teams claim them in waiver order, and unclaimed players clear.
  * The hold is the league's waiver_hours (default 24; 0 = a drop is a free agent at once). With waiver_processing_days
    set (0 = Monday ... 6 = Sunday), a hold that has run its hours lasts on to the next midnight Pacific that begins one of
    those days, and claims are resolved then. With none set, claims resolve the moment the hours end.
  * Winning a claim sends that team to the back of the order until the next reset.
  * A commissioner can set the order by hand (platform/commissioner.py); it holds until the next reset, and a team that wins
    a claim after it was set still drops to the back.

Acquisition types (leagues.acquisition_type):
  * no_waivers   - a drop is a free agent at once; waiver_hours and post-draft waivers are ignored.
  * waivers      - the default, described above.
  * full_waivers - every player nobody rosters is always on waivers. Claims resolve at the next processing day (midnight
                   Pacific), or at the weekly reset if the league set none; players nobody claims stay on waivers. The AI's
                   roster fill ignores this hold so AI teams can still fill.
  * budget       - as "waivers", but a claim carries a bid and the highest bid wins (waiver order breaks ties). The winner
                   pays his bid, non-refundable. The budget is a paid league's annual dues (whole dollars), else
                   leagues.acquisition_budget (default 100), and starts over each season. Direct signings of free agents
                   cost nothing and the college asking-price system is off.

Goal differential exists for NHL leagues (series standings). Other leagues have no weekly goal differential here, so
their order is the draft fallback all season - PROVISIONAL, until their standings are wired in.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from fantasy_sports.platform import commissioner, elimination, series_standings
from fantasy_sports.platform.db.models.core import (
    Draft,
    DraftPick,
    FreeAgencyTransaction,
    League,
    LeagueSeries,
    RecruitingDraft,
    RosterEntry,
    SeriesDailyGame,
    Team,
    WaiverClaim,
)

WAIVER_HOURS = 24
NO_WAIVERS, WAIVERS, FULL_WAIVERS, BUDGET = "no_waivers", "waivers", "full_waivers", "budget"
ACQUISITION_TYPES = (NO_WAIVERS, WAIVERS, FULL_WAIVERS, BUDGET)
DEFAULT_ACQUISITION_BUDGET = 100
WAIVER = "waiver"                      # FreeAgencyTransaction.reason for a signing won off waivers
PACIFIC = "America/Los_Angeles"
_PST = datetime.timedelta(hours=-8)    # used only when the platform has no tz database (Windows without tzdata)
BASIS_GOAL_DIFF, BASIS_DRAFT, BASIS_COMMISSIONER = "goal_diff", "draft", "commissioner"


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
def acquisition_type(league: League) -> str:
    return league.acquisition_type or WAIVERS


def is_budget(league: League) -> bool:
    return acquisition_type(league) == BUDGET


def waiver_hours(league: League) -> int:
    if acquisition_type(league) == NO_WAIVERS:
        return 0
    return WAIVER_HOURS if league.waiver_hours is None else league.waiver_hours


def budget_total(league: League) -> int:
    """Whole dollars each team can bid over a season: a paid league's dues, else the league's own budget."""
    if league.league_type == "PAID":
        return (league.annual_dues_cents or 0) // 100
    return DEFAULT_ACQUISITION_BUDGET if league.acquisition_budget is None else league.acquisition_budget


def budget_left(db: Session, team: Team) -> int:
    """The team's budget less the bids it has won this season (live transactions are cleared at rollover), plus any
    commissioner correction."""
    league = db.get(League, team.league_id)
    spent = sum(t.price for t in db.query(FreeAgencyTransaction).filter_by(
        team_id=team.id, kind="add", reason=WAIVER))
    return budget_total(league) - spent + commissioner.faab_adjustment(db, team.id)


def next_processing(league: League, now: datetime.datetime) -> datetime.datetime:
    """The first processing moment after `now`: the next midnight Pacific that begins a processing day, else the weekly reset."""
    days = set(league.waiver_processing_days or [])
    if not days:
        return next_reset(now)
    day = _to_local(now).date() + datetime.timedelta(days=1)
    while day.weekday() not in days:
        day += datetime.timedelta(days=1)
    return _to_utc(datetime.datetime.combine(day, datetime.time()))


def window_end(league: League, start: datetime.datetime) -> datetime.datetime:
    """When a hold that began at `start` (naive UTC) is over under the league's settings: the waiver period later, then, if
    the league has processing days, the next midnight Pacific that begins one of them (the hold ending exactly at such a
    midnight is over then). With no hold (0 hours) there is no window at all."""
    hours = waiver_hours(league)
    if hours <= 0:
        return start
    end = start + datetime.timedelta(hours=hours)
    days = set(league.waiver_processing_days or [])
    if not days:
        return end
    local = _to_local(end)
    day = local.date() if local.time() == datetime.time() else local.date() + datetime.timedelta(days=1)
    while day.weekday() not in days:
        day += datetime.timedelta(days=1)
    return _to_utc(datetime.datetime.combine(day, datetime.time()))


def waiver_until(db: Session, league_id: int, now: datetime.datetime, blanket: bool = True) -> dict[int, datetime.datetime]:
    """asset id -> the moment his waiver period ends, for every player dropped recently enough still to be held and not signed since.
    In a full-waivers league that is every unrostered player; blanket=False leaves that out (the AI's roster fill)."""
    from fantasy_sports.platform import free_agency

    league = db.get(League, league_id)
    if league is None:
        return {}
    if acquisition_type(league) == FULL_WAIVERS:
        if not blanket:
            return {}
        end = next_processing(league, now)
        taken = free_agency.rostered_ids(db, league_id)
        return {a: end for a in free_agency.pool_ids(db, league_id) if a not in taken}
    post_draft = _post_draft_until(db, league_id, now)
    # Looking back the longest a hold can last: the period plus a full week to the next processing day.
    reach = datetime.timedelta(hours=waiver_hours(league)) + datetime.timedelta(days=8)
    drops = db.query(FreeAgencyTransaction).filter(
        FreeAgencyTransaction.league_id == league_id, FreeAgencyTransaction.kind == "drop",
        FreeAgencyTransaction.reason != commissioner.EDIT,         # a commissioner's correction skips waivers
        FreeAgencyTransaction.created_at > now - reach,
    ).order_by(FreeAgencyTransaction.id).all()
    latest = {t.draftable_asset_id: t for t in drops}
    if not latest and not post_draft:
        return {}
    team_ids = [t.id for t in db.query(Team.id).filter_by(league_id=league_id)]
    rostered = {r.draftable_asset_id for r in db.query(RosterEntry).filter(
        RosterEntry.team_id.in_(team_ids or [0]), RosterEntry.draftable_asset_id.in_(list(latest)))}
    until = {a: window_end(league, t.created_at) for a, t in latest.items() if a not in rostered}
    out = {a: u for a, u in until.items() if u > now}
    if post_draft is not None:          # a later drop has its own window; everyone else undrafted clears with the draft's
        end, undrafted = post_draft
        for a in undrafted:
            if a not in latest:
                out[a] = end
    return out


def _draft_ended_at(db: Session, league_id: int) -> datetime.datetime | None:
    """When the league's most recent draft finished (snake/auction, or the recruiting draft closing)."""
    ends = [d.completed_at for d in db.query(Draft).filter_by(league_id=league_id, status="complete") if d.completed_at]
    rec = db.query(RecruitingDraft).filter_by(league_id=league_id, status="closed").first()
    if rec is not None and rec.ended_at is not None:
        ends.append(rec.ended_at)
    return max(ends) if ends else None


def _post_draft_until(db: Session, league_id: int, now: datetime.datetime) -> tuple[datetime.datetime, list[int]] | None:
    """(end of the window, undrafted asset ids) while the league's post-draft waivers run, else None. With the league's
    post_draft_waivers setting on, every player nobody drafted is on waivers for 24 hours after the draft ends."""
    from fantasy_sports.platform import free_agency

    league = db.get(League, league_id)
    if league is None or not league.post_draft_waivers or acquisition_type(league) in (NO_WAIVERS, FULL_WAIVERS):
        return None
    ended = _draft_ended_at(db, league_id)
    if ended is None:
        return None
    end = window_end(league, ended)
    if end <= now:
        return None
    rostered = free_agency.rostered_ids(db, league_id)
    return end, [a for a in free_agency.pool_ids(db, league_id) if a not in rostered]


def dropped_by(db: Session, league_id: int, asset_ids: list[int]) -> dict[int, int]:
    """asset id -> the team that most recently dropped him."""
    rows = db.query(FreeAgencyTransaction).filter(
        FreeAgencyTransaction.league_id == league_id, FreeAgencyTransaction.kind == "drop",
        FreeAgencyTransaction.draftable_asset_id.in_(asset_ids or [0])).order_by(FreeAgencyTransaction.id).all()
    return {t.draftable_asset_id: t.team_id for t in rows}


# ---- the order ----------------------------------------------------------------------------------------------------
@dataclass
class WaiverOrder:
    basis: str                                     # BASIS_GOAL_DIFF, BASIS_DRAFT or BASIS_COMMISSIONER
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
    since = reset
    manual = commissioner.latest_waiver_override(db, league_id, reset, at)
    if manual is not None:
        listed = [t for t in manual.team_order if t in order.teams]
        order.teams = listed + [t for t in order.teams if t not in listed]
        order.basis, since = BASIS_COMMISSIONER, manual.set_at
    wins = db.query(WaiverClaim).filter(
        WaiverClaim.league_id == league_id, WaiverClaim.status == "won",
        WaiverClaim.resolved_at >= since, WaiverClaim.resolved_at <= at).order_by(WaiverClaim.resolved_at, WaiverClaim.id).all()
    last_win = {c.team_id: i for i, c in enumerate(wins)}
    if last_win:
        moved = sorted(last_win, key=last_win.get)
        order.teams = [t for t in order.teams if t not in last_win] + [t for t in moved if t in order.teams]
        order.moved_back = moved
    return order


# ---- claims -------------------------------------------------------------------------------------------------------
def place_claim(db: Session, team: Team, asset_id: int, bid: int = 0, now: datetime.datetime | None = None) -> WaiverClaim:
    """Raises ValueError with a message meant for the user."""
    from fantasy_sports.platform import free_agency

    now = now or datetime.datetime.utcnow()
    process(db, team.league_id, now)
    if not free_agency.is_open(db, team.league_id):
        raise ValueError("Free agency opens when the draft ends")
    locked, why = elimination.free_agency_locked(db.get(League, team.league_id), team)
    if locked:
        raise ValueError(why)
    league = db.get(League, team.league_id)
    if is_budget(league):
        left = budget_left(db, team)
        if bid < 0:
            raise ValueError("A bid can't be negative")
        if bid > left:
            raise ValueError(f"You have ${max(0, left)} of budget left")
    else:
        bid = 0
    until = waiver_until(db, team.league_id, now).get(asset_id)
    if until is None:
        raise ValueError("That player isn't on waivers - sign him from the free-agent list")
    existing = db.query(WaiverClaim).filter_by(team_id=team.id, draftable_asset_id=asset_id, available_at=until, status="pending").first()
    if existing is not None:
        raise ValueError("You already have a claim on that player")
    claim = WaiverClaim(league_id=team.league_id, team_id=team.id, draftable_asset_id=asset_id, available_at=until,
                        created_at=now, status="pending", bid=bid)
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


def retime_pending(db: Session, league_id: int, now: datetime.datetime | None = None) -> None:
    """After the league's waiver period or processing days change: each pending claim follows its player's new end of hold
    (a player no longer held has his claims resolved at the next `process`). Flushes, does not commit."""
    now = now or datetime.datetime.utcnow()
    claims = db.query(WaiverClaim).filter_by(league_id=league_id, status="pending").all()
    if not claims:
        return
    until = waiver_until(db, league_id, now)
    for c in claims:
        c.available_at = until.get(c.draftable_asset_id, min(c.available_at, now))
    db.flush()


def pending_claims(db: Session, team_id: int) -> set[int]:
    return {c.draftable_asset_id for c in db.query(WaiverClaim).filter_by(team_id=team_id, status="pending")}


def process(db: Session, league_id: int, now: datetime.datetime | None = None) -> int:
    """Resolves every claim whose waiver period has ended, in the order the periods ended. Flushes, does not commit.
    Returns how many players were signed off waivers."""
    from fantasy_sports.platform import free_agency, game_lock

    now = now or datetime.datetime.utcnow()
    due =db.query(WaiverClaim).filter(
        WaiverClaim.league_id == league_id, WaiverClaim.status == "pending", WaiverClaim.available_at <= now,
    ).order_by(WaiverClaim.available_at, WaiverClaim.id).all()
    if not due:
        return 0
    league = db.get(League, league_id)
    groups: dict[tuple[datetime.datetime, int], list[WaiverClaim]] = {}
    for c in due:
        groups.setdefault((c.available_at, c.draftable_asset_id), []).append(c)
    signed = 0
    locked = game_lock.locked_assets(db, league_id, now)
    for (at, asset_id), claims in groups.items():
        if asset_id in locked:
            continue        # his game has started: the claims wait, still pending, until his period ends
        rank = {t: i for i, t in enumerate(waiver_order(db, league_id, at).teams)}
        rostered = free_agency.rostered_ids(db, league_id)
        winner = None
        budget = is_budget(league)
        for c in sorted(claims, key=lambda c: (-c.bid if budget else 0, rank.get(c.team_id, len(rank)), c.id)):
            c.resolved_at = at
            team = db.get(Team, c.team_id)
            problem = "He was signed by another team" if asset_id in rostered else free_agency.signing_problem(db, league, team, asset_id)
            if budget and not problem and c.bid > budget_left(db, team):
                problem = f"Bid of ${c.bid} is more than the budget left"
            if winner is not None:
                c.status = "lost"
            elif problem:
                c.status, c.note = "failed", problem[:200]
            else:
                price = c.bid if budget else free_agency.asking_prices(db, league)[asset_id]
                free_agency._sign(db, team, asset_id, price, WAIVER, at)
                c.status, winner = "won", c
                signed += 1
            db.flush()
    return signed
