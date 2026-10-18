"""
Offline Draft Mode: a draft run in the room (or hybrid) where the platform stays the single source of truth.

Phase 1 (this module): the server-authoritative core a commissioner console drives. Every mutation is a function here;
the router only authenticates and translates OfflineDraftError into HTTP errors.

  * One writer at a time per session: a process lock plus a row lock (a no-op on SQLite). The first commit wins; the
    loser gets "Already taken by <team>" and its client resyncs. A player (and a board cell) can sit in only one
    non-voided pick per session, enforced by partial unique indexes as a backstop to the checks here.
  * board_round/board_slot (where a pick sits) and entry_seq (when it was entered) are separate, so cells can be filled in
    any order. Undo is a void (never a delete); an edit is a void + a new pick in one transaction.
  * Snake: pre-computed board (teams as columns, rounds as rows, reversing each round); a cell's owner can be reassigned
    (traded picks) before it is filled. Auction: "Log Sale" (player, team, price) enforcing budget and roster rules.
  * Finalize writes rosters through the same objects the online drafts write (RosterEntry, Draft / AuctionDraft complete,
    pecking order, schedule); re-opening inside the window reverts those writes in one transaction.

Functions flush, they do not commit, except where the docstring says so.
"""

from __future__ import annotations

import datetime
import secrets
import threading

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fantasy_sports.platform.auction.constants import MINIMUM_BID, STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.models.core import (
    AuctionDraft, Draft, DraftableAsset, DraftPick, League, RosterEntry, Team,
)
from fantasy_sports.platform.db.models.offline_draft import (
    OfflineAuditLog, OfflineDraftPick, OfflineDraftSession, OfflineTeamClaim,
)

FORMATS = ("snake", "auction")
ENTRY_MODES = ("live", "free")
SOURCES = ("phone", "commissioner", "upload", "autopick")
DEFAULT_REOPEN_HOURS = 24
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/I


class OfflineDraftError(ValueError):
    """A refused action; the message is meant for the user."""


_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def session_lock(session_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


def utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


# ---- session setup ---------------------------------------------------------------------------------------------
def _new_join_code(db: Session) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if db.query(OfflineDraftSession.id).filter_by(join_code=code).first() is None:
            return code


def _league_teams(db: Session, league_id: int) -> list[Team]:
    return db.query(Team).filter_by(league_id=league_id).order_by(Team.id).all()


def create_session(db: Session, league: League, user_id: int, fmt: str, entry_mode: str = "free",
                   rounds: int | None = None, team_order: list[int] | None = None, budget: int | None = None,
                   min_bid: int = MINIMUM_BID, increment: int = 1, reopen_hours: int = DEFAULT_REOPEN_HOURS) -> OfflineDraftSession:
    """Creates a session in 'setup'. The player pool is snapshotted now (the pool stays fixed for the session)."""
    from fantasy_sports.platform.defense_modes import league_pool
    if fmt not in FORMATS:
        raise OfflineDraftError(f"format must be one of {FORMATS}")
    if entry_mode not in ENTRY_MODES:
        raise OfflineDraftError(f"entry_mode must be one of {ENTRY_MODES}")
    open_session = db.query(OfflineDraftSession).filter(
        OfflineDraftSession.league_id == league.id, OfflineDraftSession.status != "finalized").first()
    if open_session is not None:
        raise OfflineDraftError("This league already has an offline draft in progress")
    teams = _league_teams(db, league.id)
    if len(teams) < 2:
        raise OfflineDraftError("The league needs at least two teams")
    ids = [t.id for t in teams]
    if team_order is not None and sorted(team_order) != sorted(ids):
        raise OfflineDraftError("team_order must list every team in the league exactly once")
    rostered = {r[0] for r in db.query(RosterEntry.draftable_asset_id).join(Team, Team.id == RosterEntry.team_id)
                .filter(Team.league_id == league.id)}
    pool = sorted(a.id for a in league_pool(db, league.id) if a.id not in rostered)
    settings = {
        "rounds": rounds or league.draft_rounds,
        "team_order": team_order or ids,
        "cell_owners": {},                       # "round-slot" -> team_id, for traded picks
        "budget": budget,                        # None: each team's program-size budget
        "min_bid": min_bid,
        "increment": increment,
        "reopen_hours": reopen_hours,
        "pool": pool,
    }
    s = OfflineDraftSession(league_id=league.id, format=fmt, join_code=_new_join_code(db),
                            display_token=secrets.token_urlsafe(24), status="setup", entry_mode=entry_mode,
                            settings_json=settings, version=1, created_by_user_id=user_id, created_at=utcnow())
    db.add(s)
    db.flush()
    for t in teams:
        db.add(OfflineTeamClaim(session_id=s.id, team_id=t.id, status="unclaimed"))
    audit(db, s, f"user:{user_id}", "create_session", None, {"format": fmt, "entry_mode": entry_mode})
    db.flush()
    return s


def audit(db: Session, s: OfflineDraftSession, actor: str, action: str, before: dict | None = None,
          after: dict | None = None) -> None:
    db.add(OfflineAuditLog(session_id=s.id, actor=actor, action=action, before_json=before, after_json=after,
                           created_at=utcnow()))


def _bump(s: OfflineDraftSession) -> None:
    s.version += 1


def lock_session(db: Session, session_id: int) -> OfflineDraftSession:
    """The session row, locked for the rest of the transaction. Callers hold session_lock(session_id) as well."""
    s = db.query(OfflineDraftSession).filter(OfflineDraftSession.id == session_id).with_for_update().one_or_none()
    if s is None:
        raise OfflineDraftError("Offline draft not found")
    db.refresh(s)
    return s


def _require_open(s: OfflineDraftSession) -> None:
    if s.status == "finalized":
        raise OfflineDraftError("This draft is finalized")


# ---- the board ---------------------------------------------------------------------------------------------------
def default_owner(s: OfflineDraftSession, rnd: int, slot: int) -> int:
    order = s.settings_json["team_order"]
    if s.format == "snake" and rnd % 2 == 0:
        order = list(reversed(order))
    return order[slot - 1]


def cell_owner(s: OfflineDraftSession, rnd: int, slot: int) -> int:
    return s.settings_json.get("cell_owners", {}).get(f"{rnd}-{slot}") or default_owner(s, rnd, slot)


def overall(s: OfflineDraftSession, rnd: int, slot: int) -> int:
    return (rnd - 1) * len(s.settings_json["team_order"]) + slot


def live_picks(db: Session, session_id: int) -> list[OfflineDraftPick]:
    return (db.query(OfflineDraftPick).filter(OfflineDraftPick.session_id == session_id, OfflineDraftPick.voided_at.is_(None))
            .order_by(OfflineDraftPick.entry_seq).all())


def _next_seq(db: Session, session_id: int) -> int:
    top = db.query(OfflineDraftPick.entry_seq).filter_by(session_id=session_id).order_by(OfflineDraftPick.entry_seq.desc()).first()
    return (top[0] if top else 0) + 1


def _team_name(db: Session, team_id: int) -> str:
    t = db.get(Team, team_id)
    return t.name if t else f"team {team_id}"


def _asset_name(db: Session, asset_id: int) -> str:
    from fantasy_sports.api.helpers import draftable_asset_display_name
    a = db.get(DraftableAsset, asset_id)
    return draftable_asset_display_name(a) if a else f"asset {asset_id}"


def set_cell_owner(db: Session, s: OfflineDraftSession, rnd: int, slot: int, team_id: int, actor: str) -> None:
    """Traded picks: reassign who owns an empty cell."""
    _require_open(s)
    if s.format != "snake":
        raise OfflineDraftError("Only a snake board has cells")
    _check_cell(s, rnd, slot)
    if team_id not in s.settings_json["team_order"]:
        raise OfflineDraftError("That team is not in this draft")
    filled = db.query(OfflineDraftPick).filter_by(session_id=s.id, board_round=rnd, board_slot=slot, voided_at=None).first()
    if filled is not None:
        raise OfflineDraftError("That cell is already filled; undo the pick first")
    owners = dict(s.settings_json.get("cell_owners", {}))
    before = cell_owner(s, rnd, slot)
    owners[f"{rnd}-{slot}"] = team_id
    s.settings_json = {**s.settings_json, "cell_owners": owners}
    audit(db, s, actor, "set_cell_owner", {"cell": [rnd, slot], "team_id": before}, {"cell": [rnd, slot], "team_id": team_id})
    _bump(s)
    db.flush()


def _check_cell(s: OfflineDraftSession, rnd: int, slot: int) -> None:
    n = len(s.settings_json["team_order"])
    if not (1 <= rnd <= s.settings_json["rounds"] and 1 <= slot <= n):
        raise OfflineDraftError("That cell is not on the board")


# ---- auction budgets ---------------------------------------------------------------------------------------------
def team_budget(db: Session, s: OfflineDraftSession, team_id: int) -> int:
    fixed = s.settings_json.get("budget")
    if fixed is not None:
        return fixed
    return STARTING_BUDGET_BY_PROGRAM_SIZE[db.get(Team, team_id).program_size]


def team_state(db: Session, s: OfflineDraftSession, team_id: int, picks: list[OfflineDraftPick] | None = None) -> dict:
    picks = live_picks(db, s.id) if picks is None else picks
    mine = [p for p in picks if p.team_id == team_id]
    spent = sum(p.price or 0 for p in mine)
    roster = s.settings_json["rounds"]
    open_slots = max(0, roster - len(mine))
    remaining = team_budget(db, s, team_id) - spent
    min_bid = s.settings_json["min_bid"]
    max_bid = remaining - max(0, open_slots - 1) * min_bid if open_slots else 0
    return {"team_id": team_id, "name": _team_name(db, team_id), "picks": len(mine), "spent": spent, "remaining": remaining,
            "open_slots": open_slots, "max_bid": max(0, max_bid)}


# ---- making picks ------------------------------------------------------------------------------------------------
def _record(db: Session, s: OfflineDraftSession, team_id: int, asset_id: int, rnd: int | None, slot: int | None,
            price: int | None, source: str, user_id: int | None, device: str | None, keeper: bool, override: bool,
            actor: str) -> OfflineDraftPick:
    taken = db.query(OfflineDraftPick).filter_by(session_id=s.id, draftable_asset_id=asset_id, voided_at=None).first()
    if taken is not None:
        raise OfflineDraftError(f"Already taken by {_team_name(db, taken.team_id)}")
    pick = OfflineDraftPick(session_id=s.id, team_id=team_id, draftable_asset_id=asset_id, board_round=rnd, board_slot=slot,
                            price=price, is_keeper=keeper, entry_seq=_next_seq(db, s.id), source=source,
                            entered_by_user_id=user_id, entered_by_device=device, override=override, created_at=utcnow())
    try:
        with db.begin_nested():
            db.add(pick)
            db.flush()
    except IntegrityError:
        raise OfflineDraftError("Already taken: another entry for that player or cell was committed first")
    audit(db, s, actor, "override_pick" if override else "pick", None,
          {"pick_id": pick.id, "team_id": team_id, "asset_id": asset_id, "cell": [rnd, slot], "price": price, "source": source})
    _bump(s)
    db.flush()
    return pick


def _check_pool(s: OfflineDraftSession, asset_id: int) -> None:
    if asset_id not in set(s.settings_json["pool"]):
        raise OfflineDraftError("That player is not in this draft's pool")


def make_pick(db: Session, s: OfflineDraftSession, asset_id: int, rnd: int, slot: int, source: str = "commissioner",
              user_id: int | None = None, device: str | None = None, team_id: int | None = None, keeper: bool = False,
              enforce_turn: bool = False, actor: str | None = None) -> OfflineDraftPick:
    """Snake: puts a player into a board cell. The team defaults to the cell's owner; a different team is a commissioner
    override. enforce_turn (phones in live mode) only allows the cell that is on the clock."""
    _require_open(s)
    if s.format != "snake":
        raise OfflineDraftError("This is an auction draft: log a sale instead")
    if source not in SOURCES:
        raise OfflineDraftError("Unknown source")
    _check_cell(s, rnd, slot)
    _check_pool(s, asset_id)
    if enforce_turn:
        if s.status != "live" or s.entry_mode != "live":
            raise OfflineDraftError("The draft clock is not running")
        if on_the_clock(db, s) != (rnd, slot):
            raise OfflineDraftError("That pick is not on the clock")
    owner = cell_owner(s, rnd, slot)
    team = team_id or owner
    if team not in s.settings_json["team_order"]:
        raise OfflineDraftError("That team is not in this draft")
    cell = db.query(OfflineDraftPick).filter_by(session_id=s.id, board_round=rnd, board_slot=slot, voided_at=None).first()
    if cell is not None:
        raise OfflineDraftError(f"Already taken: that cell holds {_asset_name(db, cell.draftable_asset_id)}")
    mine = [p for p in live_picks(db, s.id) if p.team_id == team]
    override = team != owner
    if len(mine) >= s.settings_json["rounds"]:
        raise OfflineDraftError(f"{_team_name(db, team)} already has a full roster")
    return _record(db, s, team, asset_id, rnd, slot, None, source, user_id, device, keeper, override,
                   actor or f"user:{user_id}")


def on_the_clock(db: Session, s: OfflineDraftSession) -> tuple[int, int] | None:
    """The first empty cell in draft order, or None when the board is full."""
    filled = {(p.board_round, p.board_slot) for p in live_picks(db, s.id)}
    n = len(s.settings_json["team_order"])
    for rnd in range(1, s.settings_json["rounds"] + 1):
        for slot in range(1, n + 1):
            if (rnd, slot) not in filled:
                return rnd, slot
    return None


def log_sale(db: Session, s: OfflineDraftSession, asset_id: int, team_id: int, price: int, user_id: int | None = None,
             override: bool = False, source: str = "commissioner", keeper: bool = False, actor: str | None = None) -> OfflineDraftPick:
    """Auction: the in-room auctioneer path. Enforces min bid, budget (max bid leaves the roster fillable at min bid) and
    roster size; override=True skips those checks and says so in the audit log."""
    _require_open(s)
    if s.format != "auction":
        raise OfflineDraftError("This is a snake draft: enter a pick into a cell instead")
    _check_pool(s, asset_id)
    if team_id not in s.settings_json["team_order"]:
        raise OfflineDraftError("That team is not in this draft")
    state = team_state(db, s, team_id)
    min_bid = s.settings_json["min_bid"]
    if not override:
        if price < min_bid:
            raise OfflineDraftError(f"The minimum price is {min_bid}")
        if state["open_slots"] == 0:
            raise OfflineDraftError(f"{state['name']} already has a full roster")
        if price > state["max_bid"]:
            raise OfflineDraftError(f"{state['name']} can pay at most {state['max_bid']} (budget left {state['remaining']}, "
                                    f"{state['open_slots']} open slots at {min_bid} minimum)")
    elif price < 0:
        raise OfflineDraftError("Price cannot be negative")
    return _record(db, s, team_id, asset_id, None, None, price, source, user_id, None, keeper, override,
                   actor or f"user:{user_id}")


# ---- undo / edit ----------------------------------------------------------------------------------------------------
def void_pick(db: Session, s: OfflineDraftSession, pick_id: int, user_id: int | None, reason: str | None = None) -> OfflineDraftPick:
    """Marks a pick voided: the player returns to the pool, the budget is refunded (spent is computed from live picks)."""
    _require_open(s)
    pick = db.get(OfflineDraftPick, pick_id)
    if pick is None or pick.session_id != s.id:
        raise OfflineDraftError("Pick not found")
    if pick.voided_at is not None:
        raise OfflineDraftError("That pick is already voided")
    pick.voided_at = utcnow()
    pick.voided_by_user_id = user_id
    pick.void_reason = (reason or "")[:200] or None
    audit(db, s, f"user:{user_id}", "void_pick", {"pick_id": pick.id, "team_id": pick.team_id,
                                                  "asset_id": pick.draftable_asset_id, "cell": [pick.board_round, pick.board_slot],
                                                  "price": pick.price}, None)
    _bump(s)
    db.flush()
    return pick


def undo_last(db: Session, s: OfflineDraftSession, user_id: int | None) -> OfflineDraftPick:
    """Voids the live pick with the highest entry_seq."""
    picks = live_picks(db, s.id)
    if not picks:
        raise OfflineDraftError("Nothing to undo")
    return void_pick(db, s, picks[-1].id, user_id, "undo last")


def edit_pick(db: Session, s: OfflineDraftSession, pick_id: int, user_id: int | None, asset_id: int | None = None,
              team_id: int | None = None, price: int | None = None, override: bool = False) -> OfflineDraftPick:
    """Void + a new pick in one transaction (history is kept). Any refusal leaves the old pick in place."""
    old = db.get(OfflineDraftPick, pick_id)
    if old is None or old.session_id != s.id or old.voided_at is not None:
        raise OfflineDraftError("Pick not found")
    sp = db.begin_nested()
    try:
        void_pick(db, s, pick_id, user_id, "edited")
        if s.format == "snake":
            new = make_pick(db, s, asset_id or old.draftable_asset_id, old.board_round, old.board_slot, "commissioner", user_id,
                            team_id=team_id or old.team_id, keeper=old.is_keeper)
        else:
            new = log_sale(db, s, asset_id or old.draftable_asset_id, team_id or old.team_id,
                           old.price if price is None else price, user_id, override=override, keeper=old.is_keeper)
        sp.commit()
    except OfflineDraftError:
        sp.rollback()
        db.refresh(old)
        raise
    return new


# ---- lifecycle -------------------------------------------------------------------------------------------------------
def set_status(db: Session, s: OfflineDraftSession, status: str, user_id: int | None) -> None:
    """start (setup -> live), pause, resume, and move to review."""
    _require_open(s)
    allowed = {("setup", "live"), ("live", "paused"), ("paused", "live"), ("live", "review"), ("paused", "review"),
               ("setup", "review"), ("review", "live")}
    if (s.status, status) not in allowed:
        raise OfflineDraftError(f"Cannot go from {s.status} to {status}")
    before = s.status
    s.status = status
    if status == "live" and s.started_at is None:
        s.started_at = utcnow()
    audit(db, s, f"user:{user_id}", "status", {"status": before}, {"status": status})
    _bump(s)
    db.flush()


def set_entry_mode(db: Session, s: OfflineDraftSession, mode: str, user_id: int | None) -> None:
    _require_open(s)
    if mode not in ENTRY_MODES:
        raise OfflineDraftError(f"entry_mode must be one of {ENTRY_MODES}")
    audit(db, s, f"user:{user_id}", "entry_mode", {"entry_mode": s.entry_mode}, {"entry_mode": mode})
    s.entry_mode = mode
    _bump(s)
    db.flush()


def rotate_display_token(db: Session, s: OfflineDraftSession, user_id: int | None) -> str:
    s.display_token = secrets.token_urlsafe(24)
    audit(db, s, f"user:{user_id}", "rotate_display_token")
    _bump(s)
    db.flush()
    return s.display_token


def review_summary(db: Session, s: OfflineDraftSession) -> dict:
    picks = live_picks(db, s.id)
    rounds = s.settings_json["rounds"]
    incomplete, anomalies = [], []
    for tid in s.settings_json["team_order"]:
        st = team_state(db, s, tid, picks)
        if st["picks"] < rounds:
            incomplete.append({"team_id": tid, "name": st["name"], "picks": st["picks"], "of": rounds})
        if s.format == "auction" and st["remaining"] < 0:
            anomalies.append({"team_id": tid, "name": st["name"], "over_by": -st["remaining"]})
    return {"total_picks": len(picks), "incomplete_rosters": incomplete, "budget_anomalies": anomalies,
            "overrides": sum(1 for p in picks if p.override)}


# ---- finalize / re-open ----------------------------------------------------------------------------------------------
def finalize(db: Session, s: OfflineDraftSession, user_id: int | None, allow_incomplete: bool = False) -> dict:
    """Writes the rosters (the objects the online drafts write), completes the league's draft, locks the session."""
    from fantasy_sports.platform import auto_schedule, pecking_order, season_rollover
    if s.status != "review":
        raise OfflineDraftError("Move the draft to review before finalizing")
    summary = review_summary(db, s)
    if summary["incomplete_rosters"] and not allow_incomplete:
        raise OfflineDraftError("Some rosters are incomplete; finalize anyway to accept that")
    league = db.get(League, s.league_id)
    picks = live_picks(db, s.id)
    clash = [p for p in picks if db.query(RosterEntry).join(Team, Team.id == RosterEntry.team_id)
             .filter(Team.league_id == s.league_id, RosterEntry.draftable_asset_id == p.draftable_asset_id).first()]
    if clash:
        raise OfflineDraftError(f"{_asset_name(db, clash[0].draftable_asset_id)} is already on a roster in this league")
    via = "draft" if s.format == "snake" else "auction"
    now = utcnow()
    for p in picks:
        db.add(RosterEntry(team_id=p.team_id, draftable_asset_id=p.draftable_asset_id, acquired_via=via))
    db.flush()
    for p in picks:
        pecking_order.rank_new_player(db, db.get(Team, p.team_id), p.draftable_asset_id)
    written: dict = {"via": via}
    if s.format == "snake":
        draft = Draft(league_id=league.id, status="complete", season_number=league.season_number, started_at=s.started_at or now,
                      completed_at=now)
        db.add(draft)
        db.flush()
        for p in sorted(picks, key=lambda x: overall(s, x.board_round, x.board_slot)):
            db.add(DraftPick(draft_id=draft.id, round=p.board_round, pick_number=overall(s, p.board_round, p.board_slot),
                             team_id=p.team_id, draftable_asset_id=p.draftable_asset_id, picked_at=p.created_at,
                             is_autopick=p.source == "autopick"))
        written["draft_id"] = draft.id
    else:
        ad = AuctionDraft(league_id=league.id, status="complete", round_seconds=0,
                          nomination_order=s.settings_json["team_order"], started_at=s.started_at or now, completed_at=now)
        db.add(ad)
        db.flush()
        written["auction_draft_id"] = ad.id
    db.flush()
    season_rollover.after_draft_complete(db, league)
    auto_schedule.ensure_schedule(db, league)
    s.status = "finalized"
    s.finalized_at = now
    s.settings_json = {**s.settings_json, "written": written}
    audit(db, s, f"user:{user_id}", "finalize", None, {"picks": len(picks), **written})
    _bump(s)
    db.flush()
    return {"picks": len(picks), **written}


def reopen(db: Session, s: OfflineDraftSession, user_id: int | None, now: datetime.datetime | None = None) -> None:
    """Re-opens a finalized draft inside its window, reverting the roster writes in the same transaction."""
    from fantasy_sports.platform import pecking_order
    if s.status != "finalized":
        raise OfflineDraftError("Only a finalized draft can be re-opened")
    now = now or utcnow()
    hours = s.settings_json.get("reopen_hours", DEFAULT_REOPEN_HOURS)
    if now - s.finalized_at > datetime.timedelta(hours=hours):
        raise OfflineDraftError(f"The {hours}-hour re-open window has passed")
    written = s.settings_json.get("written", {})
    for p in live_picks(db, s.id):
        team = db.get(Team, p.team_id)
        pecking_order.unrank_player(db, team, p.draftable_asset_id)
        db.query(RosterEntry).filter_by(team_id=p.team_id, draftable_asset_id=p.draftable_asset_id).delete()
    if written.get("draft_id"):
        db.query(DraftPick).filter_by(draft_id=written["draft_id"]).delete()
        db.query(Draft).filter_by(id=written["draft_id"]).delete()
    if written.get("auction_draft_id"):
        db.query(AuctionDraft).filter_by(id=written["auction_draft_id"]).delete()
    s.status = "review"
    s.finalized_at = None
    s.settings_json = {k: v for k, v in s.settings_json.items() if k != "written"}
    audit(db, s, f"user:{user_id}", "reopen")
    _bump(s)
    db.flush()


# ---- state snapshot -----------------------------------------------------------------------------------------------------
def snapshot(db: Session, s: OfflineDraftSession, include_pool: bool = False) -> dict:
    """The full state clients render: board, picks (live and voided), team budgets, audit tail, version."""
    from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
    rows = db.query(OfflineDraftPick).filter_by(session_id=s.id).order_by(OfflineDraftPick.entry_seq).all()
    live = [p for p in rows if p.voided_at is None]
    ids = {p.draftable_asset_id for p in rows}
    assets = {a.id: a for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_(ids or {0}))}

    def pick_out(p: OfflineDraftPick) -> dict:
        a = assets[p.draftable_asset_id]
        return {"id": p.id, "team_id": p.team_id, "asset_id": p.draftable_asset_id,
                "name": draftable_asset_display_name(a), "position": draftable_asset_position(a),
                "round": p.board_round, "slot": p.board_slot, "price": p.price, "keeper": p.is_keeper,
                "entry_seq": p.entry_seq, "source": p.source, "override": p.override,
                "voided": p.voided_at is not None}

    teams = [team_state(db, s, tid, live) for tid in s.settings_json["team_order"]]
    out = {"id": s.id, "league_id": s.league_id, "format": s.format, "status": s.status, "entry_mode": s.entry_mode,
           "version": s.version, "join_code": s.join_code, "rounds": s.settings_json["rounds"],
           "min_bid": s.settings_json["min_bid"], "team_order": s.settings_json["team_order"], "teams": teams,
           "picks": [pick_out(p) for p in rows], "pool_size": len(s.settings_json["pool"])}
    if s.format == "snake":
        n = len(s.settings_json["team_order"])
        out["cells"] = [{"round": r, "slot": c, "owner": cell_owner(s, r, c), "overall": overall(s, r, c)}
                        for r in range(1, s.settings_json["rounds"] + 1) for c in range(1, n + 1)]
        clock = on_the_clock(db, s)
        out["on_the_clock"] = {"round": clock[0], "slot": clock[1]} if clock else None
    if s.status == "review":
        out["review"] = review_summary(db, s)
    if include_pool:
        taken = {p.draftable_asset_id for p in live}
        pool = db.query(DraftableAsset).filter(DraftableAsset.id.in_(s.settings_json["pool"] or [0])).all()
        out["pool"] = sorted(({"asset_id": a.id, "name": draftable_asset_display_name(a), "position": draftable_asset_position(a)}
                              for a in pool if a.id not in taken), key=lambda x: x["name"])
    return out
