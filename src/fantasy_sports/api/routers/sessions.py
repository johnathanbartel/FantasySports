import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import (
    create_session,
    current_user,
    delete_session,
    hash_password,
    verify_password,
)
from fantasy_sports.platform.db.models.core import User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["sessions"])


class SessionRequest(BaseModel):
    user_id: int


class GuestSessionRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class SessionOut(BaseModel):
    token: str
    user_id: int
    display_name: str
    email: str | None = None


class UserOut(BaseModel):
    id: int
    display_name: str
    email: str | None = None


@router.post("/sessions/register", response_model=SessionOut)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """Create a new user account with email and password, issuing a bearer session."""
    email = payload.email.strip().lower()
    display_name = payload.display_name.strip()
    password = payload.password
    if "@" not in email or "." not in email:
        raise HTTPException(400, "Please provide a valid email address")
    if not display_name:
        raise HTTPException(400, "Display name is required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    existing = db.query(User).filter(func.lower(User.email) == email).first()
    if existing is not None:
        if existing.password_hash is not None:
            raise HTTPException(409, "An account with that email already exists")
        existing.password_hash = hash_password(password)
        if display_name:
            existing.display_name = display_name
        db.commit()
        db.refresh(existing)
        session = create_session(db, existing.id)
        return SessionOut(token=session.token, user_id=existing.id, display_name=existing.display_name, email=existing.email)

    user = User(
        email=email,
        display_name=display_name,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    session = create_session(db, user.id)
    return SessionOut(token=session.token, user_id=user.id, display_name=user.display_name, email=user.email)


@router.post("/sessions/login", response_model=SessionOut)
def email_login(payload: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate with email and password, issuing a bearer session."""
    email = payload.email.strip().lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    session = create_session(db, user.id)
    return SessionOut(token=session.token, user_id=user.id, display_name=user.display_name, email=user.email)


@router.post("/sessions/logout")
def logout(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    """Revoke the current bearer session token."""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        delete_session(db, token)
    return {"status": "ok"}


@router.post("/sessions", response_model=SessionOut)
def login(payload: SessionRequest, db: Session = Depends(get_db)):
    """Dev-grade sign-in: issues a bearer token for the given user id with
    no password check (users.password_hash is only a hook). See api/auth.py."""
    user = db.get(User, payload.user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    session = create_session(db, user.id)
    return SessionOut(token=session.token, user_id=user.id, display_name=user.display_name, email=user.email)


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
    return SessionOut(token=session.token, user_id=user.id, display_name=user.display_name, email=user.email)


@router.get("/sessions/me", response_model=UserOut)
def whoami(user: User = Depends(current_user)):
    return UserOut(id=user.id, display_name=user.display_name, email=user.email)


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    """Lets the sign-in picker show who you can sign in as (dev only)."""
    return [UserOut(id=u.id, display_name=u.display_name, email=u.email) for u in db.query(User).order_by(User.id)]
