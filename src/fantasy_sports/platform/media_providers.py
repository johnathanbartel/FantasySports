"""
Disabled-by-default seams for external draft-room media (FS-032): AI commentary, shared music, video/audio chat.

Nothing here selects a vendor, holds a credential, or makes a network call. It defines the narrow interfaces a
provider must implement, the configuration that would switch one on, and the rule that anything short of a complete,
explicitly approved configuration FAILS CLOSED (capability reported unavailable, no provider returned).

Configuration (environment variables only; nothing is read from the repository, and values are never echoed):

    FSC_MEDIA_<CAP>                 "on" to ask for the capability; anything else (the default) is off
    FSC_MEDIA_<CAP>_PROVIDER        the provider id, which must be registered in the running process
    FSC_MEDIA_<CAP>_OWNER_APPROVED  must be "yes": the owner's recorded decision on vendor, cost, privacy, moderation
    <provider's required_env names> every setting the registered provider declares (credentials live here)

<CAP> is AI_COMMENTARY, SHARED_MUSIC or AV_CHAT. The registry is EMPTY in this build: no vendor is chosen, so a real
deployment can never report "available". Tests register in-memory fakes (platform/media_fakes.py). On Vercel
(production) every capability is forced off whatever the variables say.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from fantasy_sports.platform.ai.commentary import CommentaryProvider

AI_COMMENTARY, SHARED_MUSIC, AV_CHAT = "ai_commentary", "shared_music", "av_chat"
CAPABILITIES = (AI_COMMENTARY, SHARED_MUSIC, AV_CHAT)
LABELS = {AI_COMMENTARY: "AI commentary", SHARED_MUSIC: "Shared music", AV_CHAT: "Video / audio chat"}

DISABLED, MISCONFIGURED, AVAILABLE = "disabled", "misconfigured", "available"


class MediaDisabled(RuntimeError):
    """The capability is not available (disabled or misconfigured); the caller must not proceed."""

    def __init__(self, capability: str, state: str, reason: str):
        super().__init__(reason)
        self.capability, self.state, self.reason = capability, state, reason


@dataclass(frozen=True)
class SessionHandle:
    session_id: str
    capability: str


class SharedMusicProvider(Protocol):
    """Group listening for a draft room. Playback entitlement and account linking are the provider's problem; the
    platform only asks for these operations."""
    provider_id: str
    def create_session(self, room_id: str) -> SessionHandle: ...
    def queue_track(self, session: SessionHandle, track_ref: str, requested_by: int) -> None: ...
    def now_playing(self, session: SessionHandle) -> Mapping[str, object] | None: ...
    def close_session(self, session: SessionHandle) -> None: ...


class AvChatProvider(Protocol):
    """Live video/audio for a draft room. Consent and moderation are enforced by the provider integration."""
    provider_id: str
    def create_room(self, room_id: str) -> SessionHandle: ...
    def issue_join_token(self, session: SessionHandle, user_id: int, display_name: str) -> str: ...
    def close_room(self, session: SessionHandle) -> None: ...


@dataclass(frozen=True)
class ProviderSpec:
    capability: str
    provider_id: str
    required_env: tuple[str, ...]            # names of settings the provider needs (never their values)
    factory: Callable[[Mapping[str, str]], object]


_REGISTRY: dict[tuple[str, str], ProviderSpec] = {}


def register_provider(spec: ProviderSpec) -> None:
    if spec.capability not in CAPABILITIES:
        raise ValueError(f"Unknown capability {spec.capability!r}")
    _REGISTRY[(spec.capability, spec.provider_id)] = spec


def unregister_all() -> None:
    _REGISTRY.clear()


def registered_ids(capability: str) -> list[str]:
    return sorted(pid for (cap, pid) in _REGISTRY if cap == capability)


@dataclass(frozen=True)
class CapabilityStatus:
    capability: str
    label: str
    state: str                                  # disabled / misconfigured / available
    reason: str                                 # plain English, safe to show
    missing: tuple[str, ...] = ()               # names of missing settings (never values)
    provider_id: str | None = None


def _env_name(cap: str, suffix: str = "") -> str:
    return f"FSC_MEDIA_{cap.upper()}{suffix}"


def _is_production(env: Mapping[str, str]) -> bool:
    return bool(env.get("VERCEL"))


def status(capability: str, env: Mapping[str, str] | None = None) -> CapabilityStatus:
    """Evaluate one capability. Never raises, never returns a secret, and only reports `available` when the request,
    the provider id, the owner approval and every required setting are all present."""
    env = os.environ if env is None else env
    label = LABELS[capability]
    if _is_production(env):
        return CapabilityStatus(capability, label, DISABLED, "Not enabled in production.")
    if (env.get(_env_name(capability)) or "").strip().lower() != "on":
        reason = "Not enabled; the built-in commentary is used." if capability == AI_COMMENTARY else "Not enabled."
        return CapabilityStatus(capability, label, DISABLED, reason)
    missing: list[str] = []
    provider_id = (env.get(_env_name(capability, "_PROVIDER")) or "").strip()
    if not provider_id:
        missing.append(_env_name(capability, "_PROVIDER"))
    if (env.get(_env_name(capability, "_OWNER_APPROVED")) or "").strip().lower() != "yes":
        missing.append(_env_name(capability, "_OWNER_APPROVED"))
    spec = _REGISTRY.get((capability, provider_id)) if provider_id else None
    if provider_id and spec is None:
        return CapabilityStatus(capability, label, MISCONFIGURED,
                                "The requested provider is not installed in this build.", tuple(missing), None)
    if spec is not None:
        missing += [name for name in spec.required_env if not (env.get(name) or "").strip()]
    if missing:
        return CapabilityStatus(capability, label, MISCONFIGURED, "Setup is incomplete, so the feature stays off.",
                                tuple(missing), provider_id or None)
    return CapabilityStatus(capability, label, AVAILABLE, "Available.", (), provider_id)


def all_statuses(env: Mapping[str, str] | None = None) -> list[CapabilityStatus]:
    return [status(c, env) for c in CAPABILITIES]


def get_provider(capability: str, env: Mapping[str, str] | None = None):
    """The configured provider, or raises MediaDisabled. Fail closed: the only path to a provider is `available`."""
    env = os.environ if env is None else env
    st = status(capability, env)
    if st.state != AVAILABLE:
        raise MediaDisabled(capability, st.state, st.reason)
    spec = _REGISTRY[(capability, st.provider_id)]
    return spec.factory({name: env[name] for name in spec.required_env})


def commentary_provider(env: Mapping[str, str] | None = None) -> CommentaryProvider:
    """The commentary source for a draft: the configured external provider if (and only if) it is available,
    otherwise the local deterministic one. Never raises."""
    from fantasy_sports.platform.ai.commentary import DeterministicCommentary
    try:
        return get_provider(AI_COMMENTARY, env)
    except MediaDisabled:
        return DeterministicCommentary()
