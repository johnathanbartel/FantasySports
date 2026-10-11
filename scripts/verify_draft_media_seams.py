"""
Offline verification for the disabled-by-default draft media provider seams (FS-032).

Checks that every capability is off by default and fails closed on any incomplete or unapproved configuration, that
production forces everything off, that status output never leaks a setting value, that fake in-memory providers
work only when fully configured (with outbound sockets blocked to prove no network use), and that no vendor package,
credential or outbound URL was added. Throwaway SQLite for the HTTP checks.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_draft_media_seams.py
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    from fantasy_sports.platform import media_fakes, media_providers as mp
    from fantasy_sports.platform.ai.commentary import DeterministicCommentary, PickContext

    # Block every outbound connection for the whole run: any attempt fails the verification.
    attempts: list[object] = []

    real_connect = socket.socket.connect

    def blocked(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if not isinstance(host, str) or host in ("127.0.0.1", "::1", "localhost") or not host[:1].isdigit() and "." not in host:
            return real_connect(self, address, *a, **k)   # loopback only (the test client's own event loop)
        attempts.append(address)
        raise OSError("outbound network blocked by verify_draft_media_seams")
    socket.socket.connect = blocked  # type: ignore[assignment]

    # ---- nothing is registered or enabled in the shipped build -------------------------------------------------------
    check(all(mp.registered_ids(c) == [] for c in mp.CAPABILITIES), "the shipped registry is empty: no vendor is chosen")
    for st in mp.all_statuses({}):
        check(st.state == mp.DISABLED and st.provider_id is None, f"{st.label}: disabled by default")
    check(mp.status(mp.AI_COMMENTARY, {}).reason.endswith("built-in commentary is used."), "AI commentary says the built-in commentary is used")
    for cap in mp.CAPABILITIES:
        try:
            mp.get_provider(cap, {})
            raise AssertionError("a provider was returned for a disabled capability")
        except mp.MediaDisabled as e:
            check(e.state == mp.DISABLED, f"{cap}: asking for a provider while disabled raises MediaDisabled")
    check(isinstance(mp.commentary_provider({}), DeterministicCommentary), "commentary falls back to the local deterministic provider")

    # ---- fail closed: every incomplete configuration ----------------------------------------------------------------
    on = {"FSC_MEDIA_SHARED_MUSIC": "on"}
    s = mp.status(mp.SHARED_MUSIC, on)
    check(s.state == mp.MISCONFIGURED and set(s.missing) == {"FSC_MEDIA_SHARED_MUSIC_PROVIDER", "FSC_MEDIA_SHARED_MUSIC_OWNER_APPROVED"},
          "'on' alone is misconfigured and names the two missing settings")
    s = mp.status(mp.SHARED_MUSIC, {**on, "FSC_MEDIA_SHARED_MUSIC_PROVIDER": "somevendor", "FSC_MEDIA_SHARED_MUSIC_OWNER_APPROVED": "yes"})
    check(s.state == mp.MISCONFIGURED and "not installed" in s.reason, "an unregistered provider id is refused ('not installed in this build')")
    for flag in ("YES", "true", "1", "", "no"):
        s = mp.status(mp.SHARED_MUSIC, {**on, "FSC_MEDIA_SHARED_MUSIC_PROVIDER": "x", "FSC_MEDIA_SHARED_MUSIC_OWNER_APPROVED": flag})
        check(s.state == mp.MISCONFIGURED, f"owner approval {flag!r} does not enable anything" if flag != "YES" else "owner approval is case-insensitive but still needs a registered provider")
    check(mp.status(mp.SHARED_MUSIC, {"FSC_MEDIA_SHARED_MUSIC": "true"}).state == mp.DISABLED and mp.status(mp.SHARED_MUSIC, {"FSC_MEDIA_SHARED_MUSIC": "1"}).state == mp.DISABLED,
          "only the exact value 'on' requests a capability")

    # ---- fakes: fully configured works, partially configured does not -----------------------------------------------
    media_fakes.register_fakes()
    full_music = {"FSC_MEDIA_SHARED_MUSIC": "on", "FSC_MEDIA_SHARED_MUSIC_PROVIDER": "fake", "FSC_MEDIA_SHARED_MUSIC_OWNER_APPROVED": "yes", "FAKE_MUSIC_KEY": "sekrit-music-value"}
    s = mp.status(mp.SHARED_MUSIC, full_music)
    check(s.state == mp.AVAILABLE and s.provider_id == "fake" and s.missing == (), "a complete, approved configuration with a registered fake is available")
    check(not any("sekrit" in str(v) for v in (s.state, s.reason, s.missing, s.provider_id, s.label)), "status never contains a setting value")
    no_key = {k: v for k, v in full_music.items() if k != "FAKE_MUSIC_KEY"}
    s = mp.status(mp.SHARED_MUSIC, no_key)
    check(s.state == mp.MISCONFIGURED and s.missing == ("FAKE_MUSIC_KEY",), "a missing provider credential keeps it off (named, not valued)")
    check(mp.status(mp.SHARED_MUSIC, {**full_music, "FAKE_MUSIC_KEY": "   "}).state == mp.MISCONFIGURED, "a blank credential counts as missing")
    chat_env = {"FSC_MEDIA_AV_CHAT": "on", "FSC_MEDIA_AV_CHAT_PROVIDER": "fake", "FSC_MEDIA_AV_CHAT_OWNER_APPROVED": "yes", "FAKE_CHAT_KEY": "k"}
    check(mp.status(mp.AV_CHAT, chat_env).missing == ("FAKE_CHAT_REGION",), "every declared required setting is checked (chat needs two)")
    chat_env["FAKE_CHAT_REGION"] = "r"
    music = mp.get_provider(mp.SHARED_MUSIC, full_music)
    h = music.create_session("room1")
    music.queue_track(h, "track-1", 7)
    check(music.now_playing(h) == {"track": "track-1"} and h.capability == mp.SHARED_MUSIC, "the fake music provider satisfies the protocol")
    chat = mp.get_provider(mp.AV_CHAT, chat_env)
    room = chat.create_room("room1")
    check(chat.issue_join_token(room, 3, "Fan").startswith("fake-token:") and room.capability == mp.AV_CHAT, "the fake chat provider satisfies the protocol")
    ai_env = {"FSC_MEDIA_AI_COMMENTARY": "on", "FSC_MEDIA_AI_COMMENTARY_PROVIDER": "fake", "FSC_MEDIA_AI_COMMENTARY_OWNER_APPROVED": "yes", "FAKE_AI_KEY": "k"}
    ctx = PickContext(1, 1, 4, "T", "P", "C", 0, 0, None, None)
    check(mp.commentary_provider(ai_env).describe(ctx).text.startswith("[fake]"), "a fully configured AI provider replaces the local commentary")
    check(isinstance(mp.commentary_provider({**ai_env, "FAKE_AI_KEY": ""}), DeterministicCommentary), "a half-configured AI provider falls back to local commentary")

    # ---- production forces everything off ---------------------------------------------------------------------------
    prod = {**full_music, "VERCEL": "1"}
    s = mp.status(mp.SHARED_MUSIC, prod)
    check(s.state == mp.DISABLED and "production" in s.reason, "on Vercel a fully configured capability is still off")
    check(isinstance(mp.commentary_provider({**ai_env, "VERCEL": "1"}), DeterministicCommentary), "on Vercel commentary stays local")

    # ---- HTTP: status and the fail-closed entry point ---------------------------------------------------------------------
    mp.unregister_all()
    with tempfile.TemporaryDirectory(prefix="fs032-media-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'media').as_posix()}"
        for k in [k for k in os.environ if k.startswith("FSC_MEDIA_")]:
            del os.environ[k]
        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import User
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            u = User(email="u@example.com", display_name="U")
            db.add(u)
            db.flush()
            token = create_session(db, u.id).token
            db.commit()
        client = TestClient(app)
        r = client.get("/media/capabilities").json()
        check([c["state"] for c in r["capabilities"]] == ["disabled"] * 3 and [c["capability"] for c in r["capabilities"]] == list(mp.CAPABILITIES)
              and "off by default" in r["note"], "GET /media/capabilities reports all three disabled")
        check(client.post("/media/shared_music/session").status_code == 401, "opening a session needs sign-in")
        auth = {"Authorization": f"Bearer {token}"}
        for cap in ("shared_music", "av_chat"):
            r = client.post(f"/media/{cap}/session", headers=auth)
            check(r.status_code == 503 and r.json()["detail"]["code"] == "MEDIA_DISABLED" and r.json()["detail"]["state"] == "disabled", f"{cap} session fails closed with 503")
        check(client.post("/media/ai_commentary/session", headers=auth).status_code == 404, "AI commentary has no session endpoint")
        os.environ.update({"FSC_MEDIA_AV_CHAT": "on", "FSC_MEDIA_AV_CHAT_PROVIDER": "ghost", "FSC_MEDIA_AV_CHAT_OWNER_APPROVED": "yes", "SECRET_LOOKING": "do-not-leak"})
        r = client.get("/media/capabilities")
        chat = [c for c in r.json()["capabilities"] if c["capability"] == "av_chat"][0]
        check(chat["state"] == "misconfigured" and "do-not-leak" not in r.text and "ghost" not in r.text.replace("not installed", ""),
              "a misconfigured capability is reported without echoing configuration values")
        check(client.post("/media/av_chat/session", headers=auth).status_code == 503, "a misconfigured capability still fails closed over HTTP")
        for k in ("FSC_MEDIA_AV_CHAT", "FSC_MEDIA_AV_CHAT_PROVIDER", "FSC_MEDIA_AV_CHAT_OWNER_APPROVED", "SECRET_LOOKING"):
            os.environ.pop(k, None)
        html = client.get("/static/draft-board.html").text
        check("Draft room extras" in html and "/media/capabilities" in html and "Not enabled" in html and "Nothing here connects to an outside service" in html,
              "the draft room shows the disabled states and says nothing connects outward")
        check(not re.search(r'(src|href)=["\']https?://', html), "no external assets in the draft room")
        engine.dispose()

    # ---- nothing added that would make this real ---------------------------------------------------------------------------
    check(not attempts, "no outbound network connection was attempted during this run")
    src = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in (
        "src/fantasy_sports/platform/media_providers.py", "src/fantasy_sports/platform/media_fakes.py", "src/fantasy_sports/api/routers/media.py"))
    check(not re.search(r"^\s*(import|from)\s+(requests|httpx|urllib|socket|aiohttp|websockets|spotipy|openai|anthropic|twilio|agora|livekit)", src, re.M),
          "the seam modules import no network or vendor library")
    check(not re.search(r"https?://", src), "the seam modules contain no URLs")
    reqs = "".join((ROOT / f).read_text(encoding="utf-8") for f in ("requirements.txt", "requirements-api.txt", "pyproject.toml")).lower()
    check(not re.search(r"spotipy|spotify|openai|anthropic|twilio|agora|livekit|daily|zoom|elevenlabs|vonage|webrtc", reqs), "no vendor package is listed in the dependency files")
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout
    changed = (diff + untracked).split()
    check(not any(c in ("requirements.txt", "requirements-api.txt", "pyproject.toml", ".env") or c.endswith(".db") for c in changed), "no dependency file, .env or database was changed")
    secret_hits = [c for c in changed if c.endswith((".py", ".js", ".html", ".md")) and re.search(r"(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}",
                                                                                                  (ROOT / c).read_text(encoding="utf-8"), re.I)]
    check(not secret_hits, "no credential-looking literal in the changed files")

    print("\nAll draft media seam checks passed.")


if __name__ == "__main__":
    main()
