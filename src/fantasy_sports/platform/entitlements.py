"""
Premium features. A user has a feature if they hold a UserEntitlement row for
it - or, while the platform is in dev/testing, if FSC_GRANT_ALL_PREMIUM=1 is set
(scripts/serve_demo.py turns it on so every dev user gets everything).

COMMISSIONER MODE (premium): lets a user run a league as its commissioner
without owning a team in it - create an all-AI league, simulate its recruiting
draft instantly, and act on behalf of the league's AI teams (e.g. set their
lineups). Nothing here is billing; it is only the gate the billing would flip.
"""

import os

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import User, UserEntitlement

COMMISSIONER_MODE = "commissioner_mode"


def grant_all_in_dev() -> bool:
    return os.environ.get("FSC_GRANT_ALL_PREMIUM") == "1"


def has_feature(db: Session, user: User, feature: str) -> bool:
    if grant_all_in_dev():
        return True
    return db.query(UserEntitlement).filter_by(user_id=user.id, feature=feature).first() is not None


def grant(db: Session, user_id: int, feature: str) -> None:
    if db.query(UserEntitlement).filter_by(user_id=user_id, feature=feature).first() is None:
        db.add(UserEntitlement(user_id=user_id, feature=feature))
        db.flush()
