from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import current_user
from fantasy_sports.platform import entitlements
from fantasy_sports.platform.db.models.core import User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(prefix="/entitlements", tags=["entitlements"])


class EntitlementsOut(BaseModel):
    commissioner_mode: bool
    granted_to_everyone_in_dev: bool


@router.get("/me", response_model=EntitlementsOut)
def my_entitlements(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Which premium features the signed-in user has."""
    return EntitlementsOut(
        commissioner_mode=entitlements.has_feature(db, user, entitlements.COMMISSIONER_MODE),
        granted_to_everyone_in_dev=entitlements.grant_all_in_dev(),
    )
