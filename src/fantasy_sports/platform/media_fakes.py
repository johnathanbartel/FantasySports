"""In-memory fake providers for offline tests of the media seams (FS-032). Never registered by the application:
only tests call register_fakes(). They hold no state outside the process and make no network calls."""

from __future__ import annotations

from fantasy_sports.platform import media_providers as mp
from fantasy_sports.platform.ai.commentary import Commentary, PickContext


class FakeCommentary:
    provider_id = "fake"

    def __init__(self, settings):
        self.settings = settings

    def describe(self, ctx: PickContext) -> Commentary:
        return Commentary(text=f"[fake] {ctx.player_name} to {ctx.team_name}", tags=("fake",), value_label="unknown")


class FakeMusic:
    provider_id = "fake"

    def __init__(self, settings):
        self.queue: list[tuple[str, str, int]] = []
        self._n = 0

    def create_session(self, room_id):
        self._n += 1
        return mp.SessionHandle(f"music-{room_id}-{self._n}", mp.SHARED_MUSIC)

    def queue_track(self, session, track_ref, requested_by):
        self.queue.append((session.session_id, track_ref, requested_by))

    def now_playing(self, session):
        for sid, ref, _ in self.queue:
            if sid == session.session_id:
                return {"track": ref}
        return None

    def close_session(self, session):
        self.queue = [q for q in self.queue if q[0] != session.session_id]


class FakeChat:
    provider_id = "fake"

    def __init__(self, settings):
        self.rooms: set[str] = set()

    def create_room(self, room_id):
        self.rooms.add(room_id)
        return mp.SessionHandle(f"chat-{room_id}", mp.AV_CHAT)

    def issue_join_token(self, session, user_id, display_name):
        return f"fake-token:{session.session_id}:{user_id}"

    def close_room(self, session):
        self.rooms.discard(session.session_id.removeprefix("chat-"))


def register_fakes() -> None:
    mp.register_provider(mp.ProviderSpec(mp.AI_COMMENTARY, "fake", ("FAKE_AI_KEY",), FakeCommentary))
    mp.register_provider(mp.ProviderSpec(mp.SHARED_MUSIC, "fake", ("FAKE_MUSIC_KEY",), FakeMusic))
    mp.register_provider(mp.ProviderSpec(mp.AV_CHAT, "fake", ("FAKE_CHAT_KEY", "FAKE_CHAT_REGION"), FakeChat))
