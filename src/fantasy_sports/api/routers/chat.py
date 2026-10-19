"""
Chat router: league room, draft room and direct messages. See platform/chat.py for who may do what.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import current_user
from fantasy_sports.platform import chat
from fantasy_sports.platform.db.models.core import League, User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["chat"])


class ChatPostRequest(BaseModel):
    thread: str                 # 'league' | 'draft:<id>' | 'dm:<user id>'
    body: str


class ChatReadRequest(BaseModel):
    thread: str
    up_to_id: int | None = None  # default: everything in the thread


def _league(db: Session, user: User, league_id: int) -> League:
    return chat.require_member(db, db.get(League, league_id), user.id)


@router.get("/leagues/{league_id}/chat")
def get_messages(league_id: int, thread: str = "league", after_id: int = 0, before_id: int | None = None,
                 limit: int = Query(50, ge=1, le=100), user: User = Depends(current_user), db: Session = Depends(get_db)):
    league = _league(db, user, league_id)
    return {"thread": thread, "messages": chat.list_messages(db, league, user.id, thread, after_id, before_id, limit)}


@router.post("/leagues/{league_id}/chat")
def post_message(league_id: int, req: ChatPostRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return chat.post_message(db, _league(db, user, league_id), user.id, req.thread, req.body)


@router.post("/leagues/{league_id}/chat/read")
def mark_read(league_id: int, req: ChatReadRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    chat.mark_read(db, _league(db, user, league_id), user.id, req.thread, req.up_to_id)
    db.commit()
    return {"ok": True}


@router.get("/leagues/{league_id}/chat/summary")
def get_summary(league_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return chat.summary(db, _league(db, user, league_id), user.id)


@router.get("/leagues/{league_id}/chat/members")
def get_members(league_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return chat.members(db, _league(db, user, league_id), user.id)


@router.delete("/leagues/{league_id}/chat/{message_id}")
def delete_message(league_id: int, message_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    chat.delete_message(db, _league(db, user, league_id), user.id, message_id)
    return {"ok": True}
