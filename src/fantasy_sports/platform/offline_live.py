"""
Offline Draft Mode, phase 3: the live side. Phones join with a code and a team PIN, then pick (snake) or nominate / bid
(auction) while the server owns the clocks.

  * Claims. One active device per team: a new claim after the PIN is confirmed replaces the old device (audited, so the
    commissioner sees it). Five wrong PINs lock the team until the commissioner unlocks it. Attempts are rate limited per IP
    and per join code. "approve" mode skips PINs: the commissioner approves each claim instead. Device tokens are random;
    only their hash is stored, and they stop working when the draft is finalized.
  * Snake clock. With pick_seconds set and entry_mode live, the cell on the clock has a deadline. On timeout the draft either
    waits (default) or autopicks from the team's queue, else best available. Everything is recomputed from the stored
    deadline, so a restart or a late poll gives the same result. Pause stores the time left and resume restores it.
  * Auction lots. One open lot at a time. Bids must beat the current bid by the increment and stay within the team's max bid;
    a bid inside reset_window seconds of the end pushes the end out (anti-snipe). The server alone decides when a lot closes;
    clients render a countdown from ends_at plus the server-time offset. A closed lot becomes a sale through the same rules
    as the commissioner's Log Sale.

tick() applies whatever is due and is called (under the session lock) before every read and write of a live session. Functions
flush, they do not commit.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import re
import secrets
import time

from sqlalchemy.orm import Session

from fantasy_sports.platform import offline_draft as od
from fantasy_sports.platform.db.models.core import DraftableAsset
from fantasy_sports.platform.db.models.offline_draft import (
    OfflineAuctionLot, OfflineBidEvent, OfflineDraftPick, OfflineDraftSession, OfflineTeamClaim,
)

OfflineDraftError = od.OfflineDraftError
MAX_PIN_FAILS = 5
PIN_RE = re.compile(r"^\d{4}$")
DEFAULTS = {"pick_seconds": None, "autopick": False, "lot_seconds": 30, "reset_window": 10, "claim_mode": "pin",
            "managers_self_enter": False}
_attempts: dict[tuple, list[float]] = {}


class RateLimited(OfflineDraftError):
    pass


def setting(s: OfflineDraftSession, key: str):
    return s.settings_json.get(key, DEFAULTS.get(key))


def _iso(t: datetime.datetime | None) -> str | None:
    return t.isoformat() if t else None


def _parse(t: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(t) if t else None


def _put(s: OfflineDraftSession, **kw) -> None:
    s.settings_json = {**s.settings_json, **kw}


# ---- settings ------------------------------------------------------------------------------------------------------------
def update_settings(db: Session, s: OfflineDraftSession, user_id: int | None, **kw) -> None:
    od._require_open(s)
    clean = {}
    for key, val in kw.items():
        if val is None and key != "pick_seconds":
            continue
        if key == "pick_seconds":
            if val is not None and not 5 <= val <= 3600:
                raise OfflineDraftError("pick_seconds must be between 5 and 3600 (or off)")
        elif key in ("lot_seconds", "reset_window"):
            if not 3 <= val <= 600:
                raise OfflineDraftError(f"{key} must be between 3 and 600")
        elif key == "increment":
            if val < 1:
                raise OfflineDraftError("increment must be at least 1")
        elif key == "claim_mode":
            if val not in ("pin", "approve"):
                raise OfflineDraftError("claim_mode must be pin or approve")
        elif key not in ("autopick", "managers_self_enter"):
            raise OfflineDraftError(f"Unknown setting {key}")
        clean[key] = val
    od.audit(db, s, f"user:{user_id}", "settings", {k: s.settings_json.get(k) for k in clean}, clean)
    _put(s, **clean)
    s.settings_json = {**s.settings_json, "clock": None}        # a changed clock length starts the pick fresh
    od._bump(s)
    db.flush()


# ---- PINs and claims -----------------------------------------------------------------------------------------------------
def _hash(value: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), 60_000).hex()}"


def _check(value: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(_hash(value, salt), stored)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def claim_row(db: Session, s: OfflineDraftSession, team_id: int) -> OfflineTeamClaim:
    c = db.query(OfflineTeamClaim).filter_by(session_id=s.id, team_id=team_id).first()
    if c is None:
        raise OfflineDraftError("That team is not in this draft")
    return c


def generate_pins(db: Session, s: OfflineDraftSession, user_id: int | None, team_id: int | None = None) -> dict[int, str]:
    """New random 4-digit PINs (one team, or all). Returned once; only hashes are stored. Unlocks and signs the device out."""
    od._require_open(s)
    rows = db.query(OfflineTeamClaim).filter_by(session_id=s.id)
    if team_id is not None:
        rows = rows.filter_by(team_id=team_id)
    out = {}
    for c in rows:
        pin = f"{secrets.randbelow(10000):04d}"
        c.pin_hash, c.failed_pins, c.locked = _hash(pin), 0, False
        c.claimed_device_id, c.status = None, "unclaimed"
        out[c.team_id] = pin
    od.audit(db, s, f"user:{user_id}", "generate_pins", None, {"teams": sorted(out)})
    od._bump(s)
    db.flush()
    return out


def unlock(db: Session, s: OfflineDraftSession, team_id: int, user_id: int | None) -> None:
    c = claim_row(db, s, team_id)
    c.locked, c.failed_pins = False, 0
    od.audit(db, s, f"user:{user_id}", "unlock_team", None, {"team_id": team_id})
    od._bump(s)
    db.flush()


def release_claim(db: Session, s: OfflineDraftSession, team_id: int, user_id: int | None) -> None:
    """Signs a team's device out (a lost phone, or approve mode before re-claiming)."""
    c = claim_row(db, s, team_id)
    c.claimed_device_id, c.status = None, "unclaimed"
    od.audit(db, s, f"user:{user_id}", "release_claim", None, {"team_id": team_id})
    od._bump(s)
    db.flush()


def approve_claim(db: Session, s: OfflineDraftSession, team_id: int, user_id: int | None, approve: bool = True) -> None:
    c = claim_row(db, s, team_id)
    if c.status != "pending":
        raise OfflineDraftError("No claim is waiting for approval on that team")
    if approve:
        c.status, c.claimed_at = "claimed", od.utcnow()
    else:
        c.claimed_device_id, c.status = None, "unclaimed"
    od.audit(db, s, f"user:{user_id}", "approve_claim" if approve else "deny_claim", None, {"team_id": team_id})
    od._bump(s)
    db.flush()


def rate_limit(ip: str, code: str, limit: int = 20, window: int = 60) -> None:
    now = time.monotonic()
    for key in ((ip, code), (None, code)):
        hits = [t for t in _attempts.get(key, []) if now - t < window]
        if len(hits) >= (limit if key[0] else limit * 3):
            _attempts[key] = hits
            raise RateLimited("Too many attempts; wait a minute and try again")
        hits.append(now)
        _attempts[key] = hits


def claim_team(db: Session, s: OfflineDraftSession, team_id: int, pin: str | None) -> tuple[str, str]:
    """Returns (device token, status). In approve mode the token only works once the commissioner approves."""
    od._require_open(s)
    c = claim_row(db, s, team_id)
    mode = setting(s, "claim_mode")
    if mode == "approve":
        if c.status != "unclaimed":
            raise OfflineDraftError("That team is already claimed; ask the commissioner to release it")
        token = secrets.token_urlsafe(24)
        c.claimed_device_id, c.status = _token_hash(token), "pending"
        od.audit(db, s, f"team:{team_id}", "claim_requested", None, {"team_id": team_id})
        od._bump(s)
        db.flush()
        return token, "pending"
    if c.locked:
        raise OfflineDraftError("That team is locked after too many wrong PINs; ask the commissioner to unlock it")
    if c.pin_hash is None:
        raise OfflineDraftError("The commissioner hasn't set PINs yet")
    if not pin or not PIN_RE.match(pin) or not _check(pin, c.pin_hash):
        c.failed_pins += 1
        if c.failed_pins >= MAX_PIN_FAILS:
            c.locked = True
            od.audit(db, s, f"team:{team_id}", "team_locked", None, {"team_id": team_id})
        od._bump(s)
        db.flush()
        raise PinRejected("Wrong PIN" if not c.locked else "Wrong PIN; the team is now locked")
    c.failed_pins = 0
    replaced = c.claimed_device_id is not None
    token = secrets.token_urlsafe(24)
    c.claimed_device_id, c.status, c.claimed_at = _token_hash(token), "claimed", od.utcnow()
    od.audit(db, s, f"team:{team_id}", "device_replaced" if replaced else "team_claimed", None, {"team_id": team_id})
    od._bump(s)
    db.flush()
    return token, "claimed"


class PinRejected(OfflineDraftError):
    """A wrong PIN. The failed-attempt counter must still be committed, so the router commits before answering."""


def find_claim(db: Session, token: str | None) -> OfflineTeamClaim | None:
    if not token:
        return None
    return db.query(OfflineTeamClaim).filter_by(claimed_device_id=_token_hash(token)).first()


def join_info(db: Session, s: OfflineDraftSession) -> dict:
    claims = {c.team_id: c for c in db.query(OfflineTeamClaim).filter_by(session_id=s.id)}
    return {"session_id": s.id, "status": s.status, "format": s.format, "claim_mode": setting(s, "claim_mode"),
            "teams": [{"team_id": t["team_id"], "name": t["name"], "claimed": claims[t["team_id"]].status != "unclaimed",
                       "locked": claims[t["team_id"]].locked} for t in od.snapshot(db, s)["teams"]]}


def claims_view(db: Session, s: OfflineDraftSession) -> list[dict]:
    recent = {}
    for a in db.query(od.OfflineAuditLog).filter_by(session_id=s.id, action="device_replaced").order_by(od.OfflineAuditLog.id):
        recent[a.after_json["team_id"]] = a.created_at.isoformat()
    return [{"team_id": c.team_id, "status": c.status, "locked": c.locked, "has_pin": c.pin_hash is not None,
             "device_replaced_at": recent.get(c.team_id)}
            for c in db.query(OfflineTeamClaim).filter_by(session_id=s.id).order_by(OfflineTeamClaim.team_id)]


# ---- queue + autopick -----------------------------------------------------------------------------------------------------
def set_queue(db: Session, s: OfflineDraftSession, claim: OfflineTeamClaim, asset_ids: list[int]) -> None:
    pool = set(s.settings_json["pool"])
    clean = []
    for a in asset_ids:
        if a in pool and a not in clean:
            clean.append(a)
    claim.queue_json = clean[:50]
    db.flush()


def _taken(db: Session, s: OfflineDraftSession) -> set[int]:
    return {p.draftable_asset_id for p in od.live_picks(db, s.id)}


def best_available(db: Session, s: OfflineDraftSession, team_id: int) -> int | None:
    from fantasy_sports.platform.recruiting.pricing import season_points_by_asset
    taken = _taken(db, s)
    claim = db.query(OfflineTeamClaim).filter_by(session_id=s.id, team_id=team_id).first()
    for a in (claim.queue_json or []) if claim else []:
        if a not in taken:
            return a
    left = [a for a in s.settings_json["pool"] if a not in taken]
    if not left:
        return None
    pts = season_points_by_asset(db)
    return max(left, key=lambda a: (pts.get(a, -1.0), -a))


# ---- tick: clocks and lots -------------------------------------------------------------------------------------------------
def tick(db: Session, s: OfflineDraftSession, now: datetime.datetime | None = None) -> bool:
    """Applies what is due: a lapsed pick clock (autopick when on) or a lot whose time is up. True if anything changed."""
    now = now or od.utcnow()
    if s.status != "live":
        return False
    if s.format == "auction":
        return _tick_lot(db, s, now)
    return _tick_clock(db, s, now) if s.entry_mode == "live" else False


def _tick_clock(db: Session, s: OfflineDraftSession, now: datetime.datetime) -> bool:
    secs = setting(s, "pick_seconds")
    clock = s.settings_json.get("clock")
    if not secs:
        if clock:
            _put(s, clock=None)
            return True
        return False
    changed = False
    guard = s.settings_json["rounds"] * len(s.settings_json["team_order"]) + 2
    while guard:
        guard -= 1
        cell = od.on_the_clock(db, s)
        if cell is None:
            if clock:
                _put(s, clock=None)
                changed = True
            return changed
        if not clock or clock.get("cell") != list(cell):
            clock = {"cell": list(cell), "deadline": _iso(now + datetime.timedelta(seconds=secs)), "remaining": None, "expired": False}
            _put(s, clock=clock)
            changed = True
        elif clock.get("deadline") is None and clock.get("remaining") is not None:
            clock = {**clock, "deadline": _iso(now + datetime.timedelta(seconds=clock["remaining"])), "remaining": None}
            _put(s, clock=clock)
            changed = True
        deadline = _parse(clock["deadline"])
        if deadline is None or deadline > now:
            return changed
        if not setting(s, "autopick"):
            if not clock.get("expired"):
                _put(s, clock={**clock, "expired": True})
                changed = True
            return changed
        team = od.cell_owner(s, *cell)
        asset = best_available(db, s, team)
        if asset is None:
            return changed
        try:
            od.make_pick(db, s, asset, cell[0], cell[1], "autopick", None, actor="autopick")
        except OfflineDraftError:
            _put(s, clock={**clock, "expired": True})
            return True
        # the next pick's clock starts where this one fell due (restart-safe)
        clock = {"cell": None, "deadline": None, "remaining": None, "expired": False, "carry": clock["deadline"]}
        nxt = od.on_the_clock(db, s)
        if nxt is not None:
            clock = {"cell": list(nxt), "deadline": _iso(deadline + datetime.timedelta(seconds=secs)), "remaining": None, "expired": False}
        _put(s, clock=clock)
        changed = True
    return changed


def _open_lot(db: Session, s: OfflineDraftSession) -> OfflineAuctionLot | None:
    return db.query(OfflineAuctionLot).filter_by(session_id=s.id, status="open").first()


def _tick_lot(db: Session, s: OfflineDraftSession, now: datetime.datetime) -> bool:
    lot = _open_lot(db, s)
    if lot is not None and lot.ends_at is not None and lot.ends_at <= now:
        close_lot(db, s, lot, now)
        return True
    return False


def on_status_change(db: Session, s: OfflineDraftSession, before: str, now: datetime.datetime | None = None) -> None:
    """Freeze clocks when the draft leaves 'live'; restore them when it goes live again."""
    now = now or od.utcnow()
    clock = s.settings_json.get("clock")
    lot = _open_lot(db, s)
    if before == "live" and s.status != "live":
        if clock and clock.get("deadline"):
            left = max(0.0, (_parse(clock["deadline"]) - now).total_seconds())
            _put(s, clock={**clock, "deadline": None, "remaining": left})
        if lot is not None and lot.ends_at is not None:
            lot.paused_remaining = max(0.0, (lot.ends_at - now).total_seconds())
            lot.ends_at = None
    elif s.status == "live":
        if lot is not None and lot.paused_remaining is not None:
            lot.ends_at = now + datetime.timedelta(seconds=lot.paused_remaining)
            lot.paused_remaining = None
        # the snake clock is restored by tick(), which sees deadline None with time remaining
    db.flush()


# ---- auction lots ------------------------------------------------------------------------------------------------------
def nominator(db: Session, s: OfflineDraftSession) -> int | None:
    """Next team in the nomination rotation that still has a slot to fill and can afford the minimum."""
    order = s.settings_json["team_order"]
    idx = s.settings_json.get("nom_idx", 0)
    picks = od.live_picks(db, s.id)
    for k in range(len(order)):
        tid = order[(idx + k) % len(order)]
        st = od.team_state(db, s, tid, picks)
        if st["open_slots"] > 0 and st["max_bid"] >= s.settings_json["min_bid"]:
            return tid
    return None


def _advance_nominator(s: OfflineDraftSession, nominated_by: int) -> None:
    order = s.settings_json["team_order"]
    _put(s, nom_idx=(order.index(nominated_by) + 1) % len(order))


def nominate(db: Session, s: OfflineDraftSession, team_id: int, asset_id: int, opening_bid: int | None = None,
             enforce_turn: bool = True, now: datetime.datetime | None = None) -> OfflineAuctionLot:
    od._require_open(s)
    now = now or od.utcnow()
    if s.format != "auction":
        raise OfflineDraftError("Only an auction has lots")
    if s.status != "live":
        raise OfflineDraftError("The draft is not live")
    if _open_lot(db, s) is not None:
        raise OfflineDraftError("A lot is already open")
    if enforce_turn and nominator(db, s) != team_id:
        raise OfflineDraftError("It is not your turn to nominate")
    od._check_pool(s, asset_id)
    taken = db.query(OfflineDraftPick).filter_by(session_id=s.id, draftable_asset_id=asset_id, voided_at=None).first()
    if taken is not None:
        raise OfflineDraftError(f"Already taken by {od._team_name(db, taken.team_id)}")
    st = od.team_state(db, s, team_id)
    bid = opening_bid if opening_bid is not None else s.settings_json["min_bid"]
    if st["open_slots"] == 0:
        raise OfflineDraftError(f"{st['name']} already has a full roster")
    if bid < s.settings_json["min_bid"]:
        raise OfflineDraftError(f"The minimum bid is {s.settings_json['min_bid']}")
    if bid > st["max_bid"]:
        raise OfflineDraftError(f"{st['name']} can bid at most {st['max_bid']}")
    lot = OfflineAuctionLot(session_id=s.id, draftable_asset_id=asset_id, nominated_by_team_id=team_id, current_bid=bid,
                            current_bidder_team_id=team_id, status="open", created_at=now,
                            ends_at=now + datetime.timedelta(seconds=setting(s, "lot_seconds")))
    db.add(lot)
    db.flush()
    db.add(OfflineBidEvent(lot_id=lot.id, team_id=team_id, amount=bid, source="phone" if enforce_turn else "commissioner",
                           created_at=now, accepted=True))
    od.audit(db, s, f"team:{team_id}", "nominate", None, {"lot_id": lot.id, "asset_id": asset_id, "bid": bid})
    od._bump(s)
    db.flush()
    return lot


def bid(db: Session, s: OfflineDraftSession, team_id: int, amount: int, source: str = "phone",
        now: datetime.datetime | None = None) -> OfflineAuctionLot:
    od._require_open(s)
    now = now or od.utcnow()
    lot = _open_lot(db, s)
    if lot is None:
        raise OfflineDraftError("No lot is open")
    if s.status != "live" or lot.ends_at is None:
        raise OfflineDraftError("Bidding is paused")
    if lot.ends_at <= now:
        raise OfflineDraftError("Bidding has closed on this lot")
    if lot.current_bidder_team_id == team_id:
        raise OfflineDraftError("You already have the high bid")
    need = lot.current_bid + s.settings_json["increment"]
    st = od.team_state(db, s, team_id)
    if amount < need:
        raise OfflineDraftError(f"The next bid must be at least {need}")
    if st["open_slots"] == 0:
        raise OfflineDraftError(f"{st['name']} already has a full roster")
    if amount > st["max_bid"]:
        raise OfflineDraftError(f"{st['name']} can bid at most {st['max_bid']}")
    lot.current_bid, lot.current_bidder_team_id = amount, team_id
    window = setting(s, "reset_window")
    if (lot.ends_at - now).total_seconds() < window:
        lot.ends_at = now + datetime.timedelta(seconds=window)
    db.add(OfflineBidEvent(lot_id=lot.id, team_id=team_id, amount=amount, source=source, created_at=now, accepted=True))
    od._bump(s)
    db.flush()
    return lot


def close_lot(db: Session, s: OfflineDraftSession, lot: OfflineAuctionLot, now: datetime.datetime | None = None,
              reason: str | None = None) -> OfflineDraftPick | None:
    """Sells the lot to the high bidder through the Log Sale rules (None if it had to be cancelled)."""
    now = now or od.utcnow()
    _advance_nominator(s, lot.nominated_by_team_id)
    try:
        pick = od.log_sale(db, s, lot.draftable_asset_id, lot.current_bidder_team_id, lot.current_bid, None,
                           source="phone", actor="auction")
    except OfflineDraftError as e:
        lot.status, lot.ends_at = "cancelled", None
        od.audit(db, s, "auction", "lot_cancelled", {"lot_id": lot.id}, {"reason": str(e)})
        od._bump(s)
        db.flush()
        return None
    lot.status, lot.ends_at = "sold", None
    db.flush()
    return pick


def cancel_lot(db: Session, s: OfflineDraftSession, user_id: int | None) -> None:
    lot = _open_lot(db, s)
    if lot is None:
        raise OfflineDraftError("No lot is open")
    lot.status, lot.ends_at = "cancelled", None
    _advance_nominator(s, lot.nominated_by_team_id)
    od.audit(db, s, f"user:{user_id}", "lot_cancelled", {"lot_id": lot.id}, {"reason": "commissioner"})
    od._bump(s)
    db.flush()


def extend_lot(db: Session, s: OfflineDraftSession, seconds: int, user_id: int | None) -> None:
    lot = _open_lot(db, s)
    if lot is None:
        raise OfflineDraftError("No lot is open")
    if not 1 <= seconds <= 300:
        raise OfflineDraftError("Extend by 1 to 300 seconds")
    if lot.ends_at is not None:
        lot.ends_at += datetime.timedelta(seconds=seconds)
    elif lot.paused_remaining is not None:
        lot.paused_remaining += seconds
    od.audit(db, s, f"user:{user_id}", "lot_extended", {"lot_id": lot.id}, {"seconds": seconds})
    od._bump(s)
    db.flush()


def force_sell(db: Session, s: OfflineDraftSession, user_id: int | None) -> None:
    lot = _open_lot(db, s)
    if lot is None:
        raise OfflineDraftError("No lot is open")
    close_lot(db, s, lot)
    od.audit(db, s, f"user:{user_id}", "lot_force_sold", {"lot_id": lot.id}, None)


def commissioner_nominate(db: Session, s: OfflineDraftSession, team_id: int, asset_id: int, opening_bid: int | None,
                          user_id: int | None) -> OfflineAuctionLot:
    return nominate(db, s, team_id, asset_id, opening_bid, enforce_turn=False)


def close_expired_on_boot(session_factory) -> int:
    """After a restart: close every lot whose time ran out while the server was down, on the last accepted bid."""
    n = 0
    db = session_factory()
    try:
        for sid, in db.query(OfflineDraftSession.id).filter(OfflineDraftSession.status == "live",
                                                             OfflineDraftSession.format == "auction"):
            with od.session_lock(sid):
                s = od.lock_session(db, sid)
                if tick(db, s):
                    n += 1
                db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    return n


# ---- views -------------------------------------------------------------------------------------------------------------------
def live_info(db: Session, s: OfflineDraftSession, now: datetime.datetime | None = None) -> dict:
    """Clock / lot / nominator for snapshots. Times are ISO strings in server UTC plus server_time for the offset."""
    now = now or od.utcnow()
    out = {"server_time": _iso(now)}
    clock = s.settings_json.get("clock")
    secs = setting(s, "pick_seconds")
    if s.format == "snake":
        out["clock"] = ({"deadline": clock.get("deadline"), "remaining": clock.get("remaining"), "expired": clock.get("expired", False),
                         "seconds": secs} if clock and secs else None)
        return out
    lot = _open_lot(db, s)
    last = db.query(OfflineAuctionLot).filter_by(session_id=s.id).order_by(OfflineAuctionLot.id.desc()).first()
    out["lot"] = None
    if lot is not None:
        out["lot"] = {"id": lot.id, "asset_id": lot.draftable_asset_id, "name": od._asset_name(db, lot.draftable_asset_id),
                      "nominated_by": lot.nominated_by_team_id, "current_bid": lot.current_bid,
                      "bidder": lot.current_bidder_team_id, "ends_at": _iso(lot.ends_at), "remaining": lot.paused_remaining,
                      "status": lot.status}
    out["last_lot_status"] = last.status if last else None
    out["nominator"] = nominator(db, s) if s.status == "live" else None
    out["increment"] = s.settings_json["increment"]
    return out


def phone_view(db: Session, s: OfflineDraftSession, claim: OfflineTeamClaim) -> dict:
    snap = od.snapshot(db, s)
    team = claim.team_id
    mine = [p for p in snap["picks"] if p["team_id"] == team and not p["voided"]]
    state = next(t for t in snap["teams"] if t["team_id"] == team)
    me = {"team_id": team, "name": state["name"], "state": state, "roster": mine, "queue": claim.queue_json or [],
          "status": claim.status}
    on_clock = snap.get("on_the_clock")
    if s.format == "snake" and on_clock:
        owner = od.cell_owner(s, on_clock["round"], on_clock["slot"])
        me["on_the_clock"] = owner == team
        me["can_pick"] = (owner == team and s.status == "live"
                          and (s.entry_mode == "live" or bool(setting(s, "managers_self_enter"))))
        order = [c for c in snap["cells"] if c["round"] > on_clock["round"] or (c["round"] == on_clock["round"] and c["slot"] >= on_clock["slot"])]
        me["next_pick"] = next((c["overall"] for c in order if c["owner"] == team), None)
        me["on_deck"] = len(order) > 1 and order[1]["owner"] == team
    if s.format == "auction":
        info = snap["live"]
        me["can_nominate"] = bool(s.status == "live" and s.entry_mode == "live" and info.get("lot") is None
                                  and info.get("nominator") == team)
        me["can_bid"] = bool(s.status == "live" and s.entry_mode == "live" and info.get("lot") is not None)
    return {**snap, "me": me}
