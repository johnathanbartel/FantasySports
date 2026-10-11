# Draft-room media provider seams (FS-032)

Status: **interfaces only, everything off.** No vendor is chosen, no credential or SDK is in the repository, and the build makes no outbound request for these features.

## What exists

| Piece | Where |
|---|---|
| Provider protocols (`CommentaryProvider`, `SharedMusicProvider`, `AvChatProvider`), capability status, fail-closed lookup | `src/fantasy_sports/platform/media_providers.py` |
| In-memory fakes used only by tests | `src/fantasy_sports/platform/media_fakes.py` |
| `GET /media/capabilities` (status only), `POST /media/{shared_music\|av_chat}/session` (503 unless fully configured) | `src/fantasy_sports/api/routers/media.py` |
| "Draft room extras" panel (shows Not enabled / Setup incomplete / Available) | `web/static/draft-board.html` |
| Verification | `scripts/verify_draft_media_seams.py` |

## How a capability could be switched on (later)

Environment variables only, per capability `<CAP>` = `AI_COMMENTARY`, `SHARED_MUSIC`, `AV_CHAT`:

- `FSC_MEDIA_<CAP>=on` asks for it (default off).
- `FSC_MEDIA_<CAP>_PROVIDER=<id>` must name a provider **registered in the running process**. The registry is empty in this build, so a real deployment can never become `available`.
- `FSC_MEDIA_<CAP>_OWNER_APPROVED=yes` records that the owner decided vendor, cost, privacy and moderation.
- Every setting the provider declares as required (credentials) must be present.

Anything short of all of that reports `misconfigured` (naming only the missing setting *names*) and **fails closed**: `get_provider` raises, the HTTP entry point returns 503, the UI shows "Not enabled". On Vercel (production) every capability is forced off regardless of settings.

## Security and privacy rules for any future provider

- Credentials come from the environment or a secret manager, never from the repository, logs, status output, or the browser. The status API returns setting names, never values.
- No provider SDK or package is added without owner approval (`requirements*.txt` stays unchanged).
- Video/audio needs explicit per-user consent before any join token is issued, and a documented retention policy; join tokens must be short-lived and scoped to one room and user.
- Music needs the provider's own playback entitlement check; the platform never proxies or stores audio.
- AI commentary must stay optional: if the provider fails or is disabled, `media_providers.commentary_provider()` returns the local deterministic commentary, and the draft is unaffected. Provider output must not be shown unless labelled as generated.
- Moderation (chat, track requests) is the provider integration's responsibility and must be decided before enabling.

## Decision checklist (all open, owner-owned)

1. AI commentary: vendor and model, budget per draft, moderation, retention of prompts/outputs, whether commentary may name real people.
2. Shared music: vendor, who needs an account, playback entitlement, whether it is allowed in leagues with minors or public leagues.
3. Video/audio chat: vendor, consent and age rules, recording policy, moderation and reporting, cost per participant-minute.
4. Whether any of these may run in production, and who can enable them per league.
5. Approval to add each provider's SDK or call its API (this repository's rules forbid it until approved).

Stop and ask the owner before choosing or activating a vendor.
