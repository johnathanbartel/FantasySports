"""League branding: the custom URL slug (/l/<slug>) and the uploaded logo. Pure checks, no database access."""

import re

MAX_LOGO_BYTES = 512 * 1024
SLUG_MIN, SLUG_MAX = 3, 40
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Words a league can't claim: they would read like site pages or the platform itself.
RESERVED_SLUGS = frozenset({
    "admin", "api", "static", "login", "logout", "register", "signup", "dashboard", "leagues", "league", "health",
    "docs", "openapi", "redoc", "settings", "help", "support", "about", "fantasymotion", "www", "new", "create", "join",
})


def normalize_slug(raw: str) -> str:
    """Returns the stored form of a slug (trimmed, lowercase) or raises ValueError with a message for the user."""
    slug = (raw or "").strip().lower()
    if not (SLUG_MIN <= len(slug) <= SLUG_MAX):
        raise ValueError(f"The custom URL must be {SLUG_MIN} to {SLUG_MAX} characters long")
    if not _SLUG_RE.match(slug):
        raise ValueError("Use only letters, numbers and single hyphens (no spaces, no hyphen at the start or end)")
    if slug.isdigit():
        raise ValueError("The custom URL can't be only numbers")
    if slug in RESERVED_SLUGS:
        raise ValueError("That custom URL is reserved - pick another")
    return slug


def sniff_image(data: bytes) -> str | None:
    """The content type of a PNG / JPEG / WebP / GIF judged by its first bytes (never by what the client claims), else None.
    SVG is deliberately not accepted: it can carry scripts."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None
