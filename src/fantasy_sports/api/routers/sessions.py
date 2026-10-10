import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import create_session, current_user
from fantasy_sports.platform.db.models.core import User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["sessions"])


class SessionRequest(BaseModel):
    user_id: int


class GuestSessionRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


class SessionOut(BaseModel):
    token: str
    user_id: int
    display_name: str


class UserOut(BaseModel):
    id: int
    display_name: str


@router.post("/sessions", response_model=SessionOut)
def login(payload: SessionRequest, db: Session = Depends(get_db)):
    """Dev-grade sign-in: issues a bearer token for the given user id with
    no password check (users.password_hash is only a hook). See api/auth.py."""
    user = db.get(User, payload.user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    session = create_session(db, user.id)
    return SessionOut(token=session.token, user_id=user.id, display_name=user.display_name)


@router.post("/sessions/guest", response_model=SessionOut)
def guest_login(payload: GuestSessionRequest, db: Session = Depends(get_db)):
    """Create a lightweight commissioner identity for the public prototype.

    This is intentionally not presented as durable account authentication;
    it gives a fresh deployment a usable first-run path without seeded users.
    """
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(400, "Display name is required")
    user = User(
        email=f"guest-{uuid.uuid4().hex}@users.invalid",
        display_name=display_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    session = create_session(db, user.id)
    return SessionOut(token=session.token, user_id=user.id, display_name=user.display_name)


@router.get("/sessions/me", response_model=UserOut)
def whoami(user: User = Depends(current_user)):
    return UserOut(id=user.id, display_name=user.display_name)


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    """Lets the sign-in picker show who you can sign in as (dev only)."""
    return [UserOut(id=u.id, display_name=u.display_name) for u in db.query(User).order_by(User.id)]
