"""
Dev-grade session identity and the ownership checks built on it.

Before this, every mutating endpoint trusted a team_id in the request body,
so any caller could nominate, bid, or set a lineup as any team. Now:

  * POST /sessions {user_id} issues a bearer token (see UserSession - no
    password yet, so this is identity plumbing, not authentication).
  * require_team_owner(team_id) passes only for the human owner of a
    non-AI team. AI teams have no owner_user_id, so no token can ever act
    for them over HTTP - their moves go through platform/ai/, server-side.
  * require_commissioner(league_id) guards league-wide actions.

No/invalid token -> 401; valid token but not the owner/commissioner -> 403.
"""

import hashlib
import hmac
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.platform import entitlements
from fantasy_sports.platform.db.models.core import League, Team, User, UserSession
from fantasy_sports.platform.db.session import get_db


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"pbkdf2_sha256$100000${salt}${key.hex()}"


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        parts = hashed.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = parts[2]
        hex_key = parts[3]
        test_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
        return hmac.compare_digest(test_key.hex(), hex_key)
    except Exception:
        return False


def create_session(db: Session, user_id: int) -> UserSession:
    session = UserSession(token=secrets.token_urlsafe(32), user_id=user_id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def delete_session(db: Session, token: str) -> bool:
    count = db.query(UserSession).filter(UserSession.token == token).delete()
    db.commit()
    return count > 0


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in first (no session token supplied)")
    token = authorization[7:].strip()
    session = db.query(UserSession).filter(UserSession.token == token).first()
    if session is None:
        raise HTTPException(401, "Session token not recognised - sign in again")
    return db.get(User, session.user_id)


def optional_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User | None:
    if not authorization:
        return None
    return current_user(authorization, db)


def assert_owns_team(db: Session, user: User, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    if team.is_ai_owned or team.owner_user_id != user.id:
        raise HTTPException(403, "You don't own this team")
    return team


def assert_commissioner(db: Session, user: User, league_id: int) -> League:
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if league.commissioner_user_id != user.id:
        raise HTTPException(403, "Only the league commissioner can do that")
    return league


def require_commissioner_mode(db: Session, user: User) -> None:
    """Commissioner mode is a premium feature (see platform/entitlements.py)."""
    if not entitlements.has_feature(db, user, entitlements.COMMISSIONER_MODE):
        raise HTTPException(403, "Commissioner mode is a premium feature - it isn't switched on for your account")


def assert_can_manage_team(db: Session, user: User, team_id: int) -> Team:
    """Who may act for a team (set its lineup, ...): its human owner, or - for an
    AI-owned team - the league's commissioner, if they have commissioner mode.
    An AI team has no owner, so no other token can ever act for it."""
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    if not team.is_ai_owned:
        if team.owner_user_id != user.id:
            raise HTTPException(403, "You don't own this team")
        return team
    league = db.get(League, team.league_id)
    if league.commissioner_user_id != user.id:
        raise HTTPException(403, "Only the league's commissioner can act for an AI team")
    require_commissioner_mode(db, user)
    return team


def assert_lineup_editable(db: Session, user: User, team: Team, week) -> None:
    """A week's lineups lock when it starts. After that only the league's commissioner, with
    commissioner mode, may change them (to correct history: results are derived live, so they update)."""
    from fantasy_sports.platform import game_lock
    if not game_lock.is_week_locked(db, week):
        return
    league = db.get(League, team.league_id)
    if league.commissioner_user_id == user.id and entitlements.has_feature(db, user, entitlements.COMMISSIONER_MODE):
        return
    raise HTTPException(403, "This week has started - lineups are locked (a commissioner-mode commissioner can still change them)")
