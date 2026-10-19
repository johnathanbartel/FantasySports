"""
Chat: the league room, a draft's room and direct messages between two managers of the same league.

A thread is named by a key: 'league', 'draft:<draft id>' or 'dm:<other user's id>'. Who may take part: the league's
commissioner and co-commissioners and the human owner of any team in it (an AI team has no person behind it). The league
and draft rooms are open to all of them; a direct message is seen only by its two people - not even a commissioner reads
one. A commissioner can remove any message from the two rooms; anyone can remove their own message anywhere. Removing
keeps the row (deleted_at) so the thread stays in order, but the text is never sent again.
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from fantasy_sports.platform import commissioner
from fantasy_sports.platform.db.models.chat import ChatMessage, ChatReadMarker
from fantasy_sports.platform.db.models.core import Draft, League, LeagueCoCommissioner, Team, User

MAX_BODY = 1000
RATE_LIMIT_MESSAGES = 8         # at most this many messages from one person ...
RATE_LIMIT_SECONDS = 10         # ... in this many seconds
PAGE = 100


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def is_member(db: Session, league: League, user_id: int) -> bool:
    if commissioner.is_commissioner(db, league, user_id):
        return True
    return db.query(Team.id).filter(Team.league_id == league.id, Team.owner_user_id == user_id,
                                    Team.is_ai_owned.is_(False)).first() is not None


def require_member(db: Session, league: League | None, user_id: int) -> League:
    if league is None:
        raise HTTPException(404, "League not found")
    if not is_member(db, league, user_id):
        raise HTTPException(403, "Only people in this league can use its chat")
    return league


def parse_thread(db: Session, league: League, user_id: int, key: str) -> tuple[str, int | None, int | None]:
    """-> (channel, draft_id, other_user_id) for a thread key, checking it exists and the user may use it."""
    if key == "league":
        return "league", None, None
    kind, _, arg = key.partition(":")
    if kind not in ("draft", "dm") or not arg.isdigit():
        raise HTTPException(422, "Thread must be 'league', 'draft:<id>' or 'dm:<user id>'")
    ident = int(arg)
    if kind == "draft":
        draft = db.get(Draft, ident)
        if draft is None or draft.league_id != league.id:
            raise HTTPException(404, "Draft not found in this league")
        return "draft", ident, None
    if ident == user_id:
        raise HTTPException(422, "You can't message yourself")
    if db.get(User, ident) is None or not is_member(db, league, ident):
        raise HTTPException(404, "That person isn't in this league")
    return "dm", None, ident


def _thread_filter(league_id: int, user_id: int, channel: str, draft_id: int | None, other: int | None):
    if channel == "league":
        return and_(ChatMessage.league_id == league_id, ChatMessage.channel == "league")
    if channel == "draft":
        return and_(ChatMessage.league_id == league_id, ChatMessage.channel == "draft", ChatMessage.draft_id == draft_id)
    return and_(ChatMessage.league_id == league_id, ChatMessage.channel == "dm", or_(
        and_(ChatMessage.sender_user_id == user_id, ChatMessage.recipient_user_id == other),
        and_(ChatMessage.sender_user_id == other, ChatMessage.recipient_user_id == user_id)))


def display_names(db: Session, league_id: int, user_ids: set[int]) -> dict[int, dict]:
    """user id -> {name, team_name}: the person's display name and the team they run in this league (if any)."""
    if not user_ids:
        return {}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids))}
    teams = {t.owner_user_id: t.name for t in db.query(Team).filter(
        Team.league_id == league_id, Team.owner_user_id.in_(user_ids), Team.is_ai_owned.is_(False))}
    return {uid: {"name": users[uid].display_name if uid in users else "Unknown", "team_name": teams.get(uid)} for uid in user_ids}


def serialize(db: Session, league_id: int, rows: list[ChatMessage]) -> list[dict]:
    names = display_names(db, league_id, {r.sender_user_id for r in rows})
    out = []
    for r in rows:
        who = names.get(r.sender_user_id, {})
        out.append({
            "id": r.id, "sender_user_id": r.sender_user_id, "sender_name": who.get("name"), "team_name": who.get("team_name"),
            "body": None if r.deleted_at else r.body, "deleted": r.deleted_at is not None,
            "created_at": r.created_at.isoformat() + "Z",
        })
    return out


def list_messages(db: Session, league: League, user_id: int, key: str, after_id: int = 0, before_id: int | None = None,
                  limit: int = PAGE) -> list[dict]:
    """Oldest first. after_id fetches only newer messages (polling); otherwise the latest `limit`, or those before before_id."""
    channel, draft_id, other = parse_thread(db, league, user_id, key)
    q = db.query(ChatMessage).filter(_thread_filter(league.id, user_id, channel, draft_id, other))
    limit = max(1, min(limit, PAGE))
    if after_id:
        rows = q.filter(ChatMessage.id > after_id).order_by(ChatMessage.id).limit(PAGE).all()
    else:
        if before_id:
            q = q.filter(ChatMessage.id < before_id)
        rows = list(reversed(q.order_by(ChatMessage.id.desc()).limit(limit).all()))
    return serialize(db, league.id, rows)


def post_message(db: Session, league: League, user_id: int, key: str, body: str) -> dict:
    channel, draft_id, other = parse_thread(db, league, user_id, key)
    body = (body or "").strip()
    if not body:
        raise HTTPException(422, "Write something first")
    if len(body) > MAX_BODY:
        raise HTTPException(422, f"Messages are limited to {MAX_BODY} characters")
    now = _now()
    recent = db.query(func.count(ChatMessage.id)).filter(
        ChatMessage.sender_user_id == user_id, ChatMessage.created_at > now - datetime.timedelta(seconds=RATE_LIMIT_SECONDS)).scalar()
    if recent >= RATE_LIMIT_MESSAGES:
        raise HTTPException(429, "Slow down - too many messages in a few seconds")
    msg = ChatMessage(league_id=league.id, channel=channel, draft_id=draft_id, sender_user_id=user_id,
                      recipient_user_id=other, body=body, created_at=now)
    db.add(msg)
    db.flush()
    mark_read(db, league, user_id, key, msg.id)         # your own message is never unread for you
    db.commit()
    return serialize(db, league.id, [msg])[0]


def delete_message(db: Session, league: League, user_id: int, message_id: int) -> None:
    msg = db.get(ChatMessage, message_id)
    if msg is None or msg.league_id != league.id:
        raise HTTPException(404, "Message not found")
    if msg.channel == "dm":
        if msg.sender_user_id != user_id:
            raise HTTPException(403, "You can only remove your own direct messages")
    elif msg.sender_user_id != user_id and not commissioner.is_commissioner(db, league, user_id):
        raise HTTPException(403, "Only the sender or a commissioner can remove a message")
    if msg.deleted_at is None:
        msg.deleted_at = _now()
        msg.deleted_by_user_id = user_id
        db.commit()


def mark_read(db: Session, league: League, user_id: int, key: str, up_to_id: int | None = None) -> None:
    """Does not commit; the caller does."""
    channel, draft_id, other = parse_thread(db, league, user_id, key)
    if up_to_id is None:
        up_to_id = db.query(func.max(ChatMessage.id)).filter(
            _thread_filter(league.id, user_id, channel, draft_id, other)).scalar() or 0
    marker = db.query(ChatReadMarker).filter_by(user_id=user_id, league_id=league.id, thread_key=key).first()
    if marker is None:
        db.add(ChatReadMarker(user_id=user_id, league_id=league.id, thread_key=key, last_read_id=up_to_id))
    elif up_to_id > marker.last_read_id:
        marker.last_read_id = up_to_id
    db.flush()


def _unread(db: Session, user_id: int, key: str, flt, last_read: dict[str, int]) -> int:
    return db.query(func.count(ChatMessage.id)).filter(
        flt, ChatMessage.id > last_read.get(key, 0), ChatMessage.sender_user_id != user_id, ChatMessage.deleted_at.is_(None)).scalar()


def summary(db: Session, league: League, user_id: int) -> dict:
    """Unread counts for the league room, the latest draft's room and each direct-message conversation."""
    last_read = {m.thread_key: m.last_read_id for m in db.query(ChatReadMarker).filter_by(user_id=user_id, league_id=league.id)}
    league_unread = _unread(db, user_id, "league", _thread_filter(league.id, user_id, "league", None, None), last_read)
    draft = db.query(Draft).filter(Draft.league_id == league.id).order_by(Draft.id.desc()).first()
    draft_info = None
    if draft is not None:
        draft_info = {"draft_id": draft.id, "unread": _unread(
            db, user_id, f"draft:{draft.id}", _thread_filter(league.id, user_id, "draft", draft.id, None), last_read)}
    dm_rows = db.query(ChatMessage).filter(ChatMessage.league_id == league.id, ChatMessage.channel == "dm", or_(
        ChatMessage.sender_user_id == user_id, ChatMessage.recipient_user_id == user_id)).order_by(ChatMessage.id.desc()).all()
    latest: dict[int, ChatMessage] = {}
    for r in dm_rows:
        latest.setdefault(r.recipient_user_id if r.sender_user_id == user_id else r.sender_user_id, r)
    names = display_names(db, league.id, set(latest))
    dms = [{"user_id": other, "name": names[other]["name"], "team_name": names[other]["team_name"],
            "last_message_id": r.id, "last_at": r.created_at.isoformat() + "Z", "preview": None if r.deleted_at else r.body[:80],
            "unread": _unread(db, user_id, f"dm:{other}", _thread_filter(league.id, user_id, "dm", None, other), last_read)}
           for other, r in latest.items()]
    dms.sort(key=lambda d: d["last_message_id"], reverse=True)
    return {"league_unread": league_unread, "draft": draft_info, "dms": dms,
            "total_unread": league_unread + (draft_info["unread"] if draft_info else 0) + sum(d["unread"] for d in dms)}


def members(db: Session, league: League, user_id: int) -> list[dict]:
    """People you can message: everyone in the league but you."""
    ids = {t.owner_user_id for t in db.query(Team).filter(
        Team.league_id == league.id, Team.owner_user_id.isnot(None), Team.is_ai_owned.is_(False))}
    ids.add(league.commissioner_user_id)
    ids |= {c.user_id for c in db.query(LeagueCoCommissioner).filter_by(league_id=league.id)}
    ids.discard(user_id)
    names = display_names(db, league.id, ids)
    return sorted(({"user_id": i, **names[i]} for i in ids), key=lambda m: (m["team_name"] or "~", m["name"]))
