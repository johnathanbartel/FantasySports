"""
Draft-room media capability status (FS-032). Read-only status for the UI, plus the guarded entry point that proves a
disabled capability fails closed. No provider is registered in this build, so every capability reports unavailable.
Status never contains a secret or a setting value, only the NAMES of settings that are missing.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fantasy_sports.api.auth import current_user
from fantasy_sports.platform import media_providers as mp
from fantasy_sports.platform.db.models.core import User

router = APIRouter(prefix="/media", tags=["media"])


class CapabilityOut(BaseModel):
    capability: str
    label: str
    state: str
    reason: str
    missing: list[str]
    provider_id: str | None


class CapabilitiesOut(BaseModel):
    capabilities: list[CapabilityOut]
    note: str


def _out(s: mp.CapabilityStatus) -> CapabilityOut:
    return CapabilityOut(capability=s.capability, label=s.label, state=s.state, reason=s.reason, missing=list(s.missing),
                         provider_id=s.provider_id)


@router.get("/capabilities", response_model=CapabilitiesOut)
def capabilities():
    return CapabilitiesOut(capabilities=[_out(s) for s in mp.all_statuses()],
                           note="External media is off by default. Nothing here is used until the owner approves a provider.")


@router.post("/{capability}/session")
def open_session(capability: str, user: User = Depends(current_user)):
    """Open a media session. Fails closed with 503 unless the capability is fully configured and approved."""
    if capability not in (mp.SHARED_MUSIC, mp.AV_CHAT):
        raise HTTPException(404, "Unknown capability")
    try:
        provider = mp.get_provider(capability)
    except mp.MediaDisabled as e:
        raise HTTPException(503, {"code": "MEDIA_DISABLED", "state": e.state, "message": e.reason})
    handle = provider.create_session("draft") if capability == mp.SHARED_MUSIC else provider.create_room("draft")
    return {"session_id": handle.session_id, "capability": handle.capability}
