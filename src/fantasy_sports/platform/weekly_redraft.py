"""
Weekly salary-cap redraft (NHL): each week every team builds a fresh six-player roster under a cap and faces one
private-league opponent. An explicit league mode (leagues.league_mode = 'weekly_redraft'), separate from the
multi-day series format. Salaries are abstract points-of-budget; no currency or money flow of any kind exists in this module.

Rules
  * Roster: 1 C, 2 W, 2 D, 1 G (the NHL starting six; W accepts LW/RW). A player may appear once per roster.
  * Salaries are an INPUT. Nothing here projects or invents a price: the commissioner loads a salary for each
    pool player per contest (set_salaries) before entries open, and an unpriced player cannot be rostered.
  * Cap: roster total <= the contest's cap. The cap is snapshotted onto the contest (`cap_applied`) and onto each
    submitted entry, so later settings changes never alter a past week.
  * Cap policy: FIXED only. The "adjust the cap from last week's finish" formula is an open product decision and is
    deliberately NOT implemented or guessed; see docs/weekly_redraft.md.
  * Lifecycle: OPEN until `lock_at`, then LOCKED (applied lazily by sync). A saved entry is a DRAFT that can be
    replaced until the lock; submit makes it final (idempotent). A draft that was never submitted does not count.
    Nothing can change after the lock. Opponents cannot see each other's roster before the lock.
  * Pairing: circle method over team ids (the same one the NHL series schedule uses), one round per week, so every
    team has exactly one opponent each week and opponents rotate; deterministic.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fantasy_sports.platform import series_schedule
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, LeagueSeries, Team, WeeklyContest, WeeklyContestEntry, WeeklyContestPairing,
    WeeklyContestSlot, WeeklyPlayerSalary, WeeklyRedraftSettings,
)
from fantasy_sports.platform.defense_modes import league_pool
from fantasy_sports.platform.pecking_order import GROUP_BY_POSITION

NEUTRAL_FIXED_CAP = 50000     # salary units; a neutral placeholder, not a tuned value
MAX_WEEKS = 60
SLOTS = (("C", 1), ("W", 2), ("D", 2), ("G", 1))   # slot code, how many
SLOT_COUNT = dict(SLOTS)
ROSTER_SIZE = sum(n for _, n in SLOTS)


class RedraftError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def settings_of(db: Session, league_id: int) -> WeeklyRedraftSettings | None:
    return db.query(WeeklyRedraftSettings).filter_by(league_id=league_id).first()


def enable(db: Session, league: League, base_cap: int | None) -> WeeklyRedraftSettings:
    """Switch an NHL league into weekly-redraft mode with a FIXED cap. Flushes, does not commit."""
    if not series_schedule.league_is_nhl(db, league.id):
        raise RedraftError("Weekly redraft is only available for NHL leagues", "NOT_NHL")
    cap = NEUTRAL_FIXED_CAP if base_cap is None else base_cap
    if not isinstance(cap, int) or cap <= 0:
        raise RedraftError("The cap must be a positive whole number", "BAD_CAP")
    if db.query(LeagueSeries).filter_by(league_id=league.id).count():
        raise RedraftError("This league already has a series schedule; weekly redraft is a separate mode", "HAS_SERIES")
    st = settings_of(db, league.id)
    if st is None:
        st = WeeklyRedraftSettings(league_id=league.id, base_cap=cap, cap_policy="FIXED")
        db.add(st)
    elif st.base_cap != cap:
        if db.query(WeeklyContest).filter_by(league_id=league.id).count():
            raise RedraftError("The cap cannot change once a contest exists", "CAP_LOCKED")
        st.base_cap = cap
    league.league_mode = "weekly_redraft"
    db.flush()
    return st


def create_contest(db: Session, league: League, start_date: datetime.date, end_date: datetime.date,
                   lock_at: datetime.datetime) -> WeeklyContest:
    if league.league_mode != "weekly_redraft":
        raise RedraftError("This league is not in weekly redraft mode", "NOT_ENABLED")
    st = settings_of(db, league.id)
    if end_date < start_date:
        raise RedraftError("The contest cannot end before it starts", "BAD_DATES")
    if lock_at > datetime.datetime.combine(end_date, datetime.time.max):
        raise RedraftError("Rosters must lock before the contest ends", "BAD_LOCK")
    team_ids = sorted(t.id for t in db.query(Team.id).filter_by(league_id=league.id))
    if len(team_ids) < 2 or len(team_ids) % 2:
        raise RedraftError(f"Head-to-head pairing needs an even number of teams (this league has {len(team_ids)})", "BAD_TEAM_COUNT")
    last = db.query(WeeklyContest).filter_by(league_id=league.id).order_by(WeeklyContest.week_index.desc()).first()
    week = 1 if last is None else last.week_index + 1
    if week > MAX_WEEKS:
        raise RedraftError("Too many contests", "TOO_MANY")
    if db.query(WeeklyContest).filter_by(league_id=league.id, start_date=start_date).count():
        raise RedraftError("A contest already starts on that date", "DUPLICATE")
    contest = WeeklyContest(league_id=league.id, week_index=week, start_date=start_date, end_date=end_date,
                            lock_at=lock_at, cap_applied=st.base_cap, status="OPEN")
    db.add(contest)
    db.flush()
    plan = series_schedule.plan_series(team_ids, 7, start_date)
    round_index = (week - 1) % (len(team_ids) - 1)
    for p in plan:
        if p.round_index == round_index:
            db.add(WeeklyContestPairing(contest_id=contest.id, team_a_id=p.team_a_id, team_b_id=p.team_b_id))
    db.flush()
    return contest


def sync(contest: WeeklyContest, now: datetime.datetime) -> None:
    if contest.status == "OPEN" and now >= contest.lock_at:
        contest.status = "LOCKED"


def pool_player_assets(db: Session, league_id: int) -> dict[int, DraftableAsset]:
    return {a.id: a for a in league_pool(db, league_id) if a.asset_type == "player" and a.player is not None}


def set_salaries(db: Session, contest: WeeklyContest, salaries: dict[int, int], now: datetime.datetime) -> int:
    """Replace the contest's price list. Only while OPEN and before any entry exists (prices an entry was checked
    against must not move). Returns the number priced."""
    sync(contest, now)
    if contest.status != "OPEN":
        raise RedraftError("The contest is locked", "LOCKED")
    if db.query(WeeklyContestEntry).filter_by(contest_id=contest.id).count():
        raise RedraftError("Salaries cannot change once a roster has been saved", "HAS_ENTRIES")
    if not salaries:
        raise RedraftError("No salaries supplied", "EMPTY")
    pool = pool_player_assets(db, contest.league_id)
    for asset_id, salary in salaries.items():
        if asset_id not in pool:
            raise RedraftError(f"Asset {asset_id} is not in this league's player pool", "NOT_IN_POOL")
        if not isinstance(salary, int) or isinstance(salary, bool) or salary <= 0 or salary > contest.cap_applied:
            raise RedraftError(f"Salary for {asset_id} must be a whole number between 1 and the cap", "BAD_SALARY")
    db.query(WeeklyPlayerSalary).filter_by(contest_id=contest.id).delete(synchronize_session=False)
    db.flush()
    for asset_id, salary in sorted(salaries.items()):
        db.add(WeeklyPlayerSalary(contest_id=contest.id, draftable_asset_id=asset_id, salary=salary))
    db.flush()
    return len(salaries)


def _opponent(db: Session, contest: WeeklyContest, team_id: int) -> int | None:
    p = db.query(WeeklyContestPairing).filter(WeeklyContestPairing.contest_id == contest.id,
                                              (WeeklyContestPairing.team_a_id == team_id) | (WeeklyContestPairing.team_b_id == team_id)).first()
    if p is None:
        return None
    return p.team_b_id if p.team_a_id == team_id else p.team_a_id


def opponent_of(db: Session, contest: WeeklyContest, team_id: int) -> int | None:
    return _opponent(db, contest, team_id)


@dataclass
class Checked:
    slots: list[tuple[str, int, int, int]]   # (slot code, index, asset id, salary)
    total: int


def validate_roster(db: Session, contest: WeeklyContest, payload: dict[str, list[int]], require_full: bool) -> Checked:
    """Pure-ish validation (reads salaries and players). Raises RedraftError with a code."""
    for code in payload:
        if code not in SLOT_COUNT:
            raise RedraftError(f"Unknown slot {code!r} (use C, W, D, G)", "BAD_SLOT")
    prices = {s.draftable_asset_id: s.salary for s in db.query(WeeklyPlayerSalary).filter_by(contest_id=contest.id)}
    if not prices:
        raise RedraftError("Salaries have not been published for this contest", "NO_SALARIES")
    pool = pool_player_assets(db, contest.league_id)
    seen: set[int] = set()
    out: list[tuple[str, int, int, int]] = []
    for code, count in SLOTS:
        ids = payload.get(code, [])
        if len(ids) > count:
            raise RedraftError(f"{code} takes at most {count} player(s), got {len(ids)}", "TOO_MANY")
        if require_full and len(ids) != count:
            raise RedraftError(f"A submitted roster needs {count} {code}; got {len(ids)}", "INCOMPLETE")
        for i, asset_id in enumerate(ids, start=1):
            if asset_id in seen:
                raise RedraftError(f"Player {asset_id} is on the roster more than once", "DUPLICATE_PLAYER")
            seen.add(asset_id)
            asset = pool.get(asset_id)
            if asset is None:
                raise RedraftError(f"Player {asset_id} is not in this league's player pool", "NOT_IN_POOL")
            if GROUP_BY_POSITION.get(asset.player.position) != code:
                raise RedraftError(f"{asset.player.full_name} ({asset.player.position}) cannot fill {code}", "INVALID_POSITION")
            if asset_id not in prices:
                raise RedraftError(f"{asset.player.full_name} has no salary this week", "UNPRICED_PLAYER")
            out.append((code, i, asset_id, prices[asset_id]))
    total = sum(s[3] for s in out)
    if total > contest.cap_applied:
        raise RedraftError(f"Roster salary {total} is over the cap of {contest.cap_applied}", "CAP_EXCEEDED")
    return Checked(out, total)


def _entry(db: Session, contest: WeeklyContest, team_id: int) -> WeeklyContestEntry | None:
    return db.query(WeeklyContestEntry).filter_by(contest_id=contest.id, team_id=team_id).first()


def get_entry(db: Session, contest: WeeklyContest, team_id: int) -> WeeklyContestEntry | None:
    return _entry(db, contest, team_id)


def _guard_editable(db: Session, contest: WeeklyContest, team_id: int, now: datetime.datetime) -> WeeklyContestEntry | None:
    sync(contest, now)
    if contest.status != "OPEN":
        raise RedraftError("The contest is locked; rosters can no longer change", "LOCKED")
    if _opponent(db, contest, team_id) is None:
        raise RedraftError("This team is not paired in this contest", "NOT_PAIRED")
    entry = _entry(db, contest, team_id)
    if entry is not None and entry.status == "SUBMITTED":
        raise RedraftError("This roster was already submitted and is final", "ALREADY_SUBMITTED")
    return entry


def save_entry(db: Session, contest: WeeklyContest, team_id: int, payload: dict[str, list[int]],
               now: datetime.datetime) -> WeeklyContestEntry:
    """Store (replace) a DRAFT roster. Replaying the same request leaves identical rows. Flushes, does not commit."""
    entry = _guard_editable(db, contest, team_id, now)
    checked = validate_roster(db, contest, payload, require_full=False)
    if entry is None:
        entry = WeeklyContestEntry(contest_id=contest.id, team_id=team_id, status="DRAFT", cap=contest.cap_applied)
        db.add(entry)
        db.flush()
    else:
        db.query(WeeklyContestSlot).filter_by(entry_id=entry.id).delete(synchronize_session=False)
        db.flush()
    for code, idx, asset_id, salary in checked.slots:
        db.add(WeeklyContestSlot(entry_id=entry.id, slot_code=code, slot_index=idx, draftable_asset_id=asset_id, salary=salary))
    entry.salary_total = checked.total
    entry.cap = contest.cap_applied
    db.flush()
    db.refresh(entry)
    return entry


def submit_entry(db: Session, contest: WeeklyContest, team_id: int, now: datetime.datetime) -> WeeklyContestEntry:
    """Make the saved roster final. Idempotent: a second call returns the same submitted entry unchanged."""
    sync(contest, now)
    existing = _entry(db, contest, team_id)
    if existing is not None and existing.status == "SUBMITTED":
        return existing
    entry = _guard_editable(db, contest, team_id, now)
    if entry is None:
        raise RedraftError("Save a roster before submitting it", "NO_ROSTER")
    payload: dict[str, list[int]] = {}
    for s in entry.slots:
        payload.setdefault(s.slot_code, []).append(s.draftable_asset_id)
    checked = validate_roster(db, contest, payload, require_full=True)   # re-check against today's prices and cap
    entry.salary_total = checked.total
    entry.status = "SUBMITTED"
    entry.submitted_at = now
    db.flush()
    return entry
