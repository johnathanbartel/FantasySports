"""
NHL injury report sync from ESPN's unofficial public injuries endpoint (free, no key, undocumented: it can change
without notice, so parsing is defensive and a bad response changes nothing).

parse_report() is pure. apply_report() matches ESPN players to our Player rows by normalised name (ESPN ids are not
NHL ids), using the team abbreviation only to break ties between same-named players, then sets each player's status via
injuries.set_injury(). Players ESPN no longer lists are cleared (their injury ended), except long-term IR set by hand.
Both return/accept plain data so callers can build alerts from the changes.
"""

from __future__ import annotations

import datetime
import json
import unicodedata
import urllib.request
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fantasy_sports.platform import injuries
from fantasy_sports.platform.db.models.core import Player, RealTeam, Universe

ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
HEADERS = {"User-Agent": "FantasySportsPlatform-InjurySync/1.0"}

STATUS_MAP = {"day-to-day": "DTD", "out": "OUT", "injured reserve": "IR"}
# ESPN team abbreviation -> NHL tricode, where they differ.
TEAM_ALIASES = {"NJ": "NJD", "SJ": "SJS", "TB": "TBL", "LA": "LAK", "UTAH": "UTA", "WAS": "WSH"}
# A response with fewer injuries than this is treated as broken, so a bad fetch can't mass-clear injuries.
MIN_PLAUSIBLE_ENTRIES = 15


@dataclass(frozen=True)
class ReportEntry:
    name: str
    team_code: str | None
    status: str          # DTD / OUT / IR
    note: str | None     # e.g. "Upper Body"
    comment: str | None  # ESPN's short blurb


@dataclass(frozen=True)
class Change:
    player_id: int
    name: str
    old_status: str | None
    new_status: str | None
    note: str | None


def fetch_report(timeout: float = 20.0) -> dict | None:
    req = urllib.request.Request(ESPN_INJURIES_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def normalise_name(name: str) -> str:
    stripped = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return "".join(c for c in stripped.lower() if c.isalnum())


def parse_report(payload: dict | None) -> list[ReportEntry]:
    """ESPN's {"injuries": [{team, "injuries": [{status, athlete, details, ...}]}]} as a flat list. Entries with an
    unknown status or no name are skipped."""
    out: list[ReportEntry] = []
    for team in (payload or {}).get("injuries") or []:
        for item in team.get("injuries") or []:
            athlete = item.get("athlete") or {}
            name = athlete.get("displayName")
            status = STATUS_MAP.get(str(item.get("status") or "").strip().lower())
            if not name or status is None:
                continue
            code = (athlete.get("team") or {}).get("abbreviation")
            code = TEAM_ALIASES.get(code, code) if code else None
            note = (item.get("details") or {}).get("type") or None
            out.append(ReportEntry(name=name, team_code=code, status=status, note=note,
                                   comment=item.get("shortComment")))
    return out


def apply_report(db: Session, entries: list[ReportEntry], now: datetime.datetime | None = None) -> list[Change]:
    """Updates NHL players' injury status from the report and returns what changed. Flushes, does not commit. Does
    nothing (returns []) when the report is implausibly short."""
    if len(entries) < MIN_PLAUSIBLE_ENTRIES:
        return []
    nhl = db.query(Universe).filter_by(code="nhl").first()
    if nhl is None:
        return []
    codes = {t.id: t.external_ref for t in db.query(RealTeam).filter_by(universe_id=nhl.id)}
    by_name: dict[str, list[Player]] = {}
    for p in db.query(Player).filter_by(universe_id=nhl.id, active=True):
        by_name.setdefault(normalise_name(p.full_name), []).append(p)

    changes: list[Change] = []
    seen: set[int] = set()
    for e in entries:
        candidates = by_name.get(normalise_name(e.name), [])
        if len(candidates) > 1 and e.team_code:
            candidates = [p for p in candidates if codes.get(p.real_team_id) == e.team_code] or candidates
        if len(candidates) != 1:
            continue    # unknown or ambiguous: better no status than the wrong player's
        player = candidates[0]
        seen.add(player.id)
        if (player.injury_status, player.injury_note) != (e.status, e.note):
            changes.append(Change(player.id, player.full_name, player.injury_status, e.status, e.note))
            injuries.set_injury(db, player, e.status, e.note, now)

    for players in by_name.values():
        for player in players:
            if player.injury_status in ("DTD", "OUT", "IR") and player.id not in seen:
                changes.append(Change(player.id, player.full_name, player.injury_status, None, None))
                injuries.set_injury(db, player, None, None, now)
    return changes


def sync(db: Session, now: datetime.datetime | None = None) -> list[Change] | None:
    """Fetches the live report and applies it. Returns the changes, or None if the fetch failed. Commits."""
    payload = fetch_report()
    if payload is None:
        return None
    changes = apply_report(db, parse_report(payload), now)
    db.commit()
    return changes
