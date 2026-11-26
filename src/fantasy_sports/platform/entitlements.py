"""
Premium features. A user has a feature if they hold a UserEntitlement row for
it - or, while the platform is in dev/testing, if FSC_GRANT_ALL_PREMIUM=1 is set
(scripts/serve_demo.py turns it on so every dev user gets everything).

COMMISSIONER MODE (premium): lets a user run a league as its commissioner
without owning a team in it - create an all-AI league, simulate its recruiting
draft instantly, and act on behalf of the league's AI teams (e.g. set their
lineups).

DRAFT GRADES (premium): the AI draft grades and pick values (platform/draft_grades.py). Power rankings stay free.

A paying subscriber (platform/billing.py, Stripe) gets every feature in PREMIUM_FEATURES. To make a feature free
again, take it out of that tuple; to make another feature premium, add its name and check has_feature() where it
is served.
"""

import os

from sqlalchemy.orm import Session

from fantasy_sports.platform import billing
from fantasy_sports.platform.db.models.core import User, UserEntitlement

COMMISSIONER_MODE = "commissioner_mode"
DRAFT_GRADES = "draft_grades"

# What the premium subscription unlocks. Everything else on the site is free.
PREMIUM_FEATURES = (COMMISSIONER_MODE, DRAFT_GRADES)


def grant_all_in_dev() -> bool:
    return os.environ.get("FSC_GRANT_ALL_PREMIUM") == "1"


def is_premium_feature(feature: str) -> bool:
    return feature in PREMIUM_FEATURES


def has_feature(db: Session, user: User | None, feature: str) -> bool:
    """Free features are always on. A premium one is on in dev (FSC_GRANT_ALL_PREMIUM=1), for a hand-granted
    entitlement row, or for an active subscriber."""
    if feature not in PREMIUM_FEATURES:
        return True
    if grant_all_in_dev():
        return True
    if user is None:
        return False
    if db.query(UserEntitlement).filter_by(user_id=user.id, feature=feature).first() is not None:
        return True
    return billing.is_premium(db, user.id)


def grant(db: Session, user_id: int, feature: str) -> None:
    if db.query(UserEntitlement).filter_by(user_id=user_id, feature=feature).first() is None:
        db.add(UserEntitlement(user_id=user_id, feature=feature))
        db.flush()
