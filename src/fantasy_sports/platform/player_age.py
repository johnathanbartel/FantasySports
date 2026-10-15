"""Player age from a stored birth date. Pure helpers; no database access."""

from __future__ import annotations

import datetime


def parse_birth_date(value: object) -> datetime.date | None:
    """A source's "YYYY-MM-DD" (NHL API) as a date; anything unparseable is None, never an error."""
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def age_on(birth_date: datetime.date | None, today: datetime.date | None = None) -> int | None:
    """Whole years on `today` (default: today); None when the birth date is unknown or implausible."""
    if birth_date is None:
        return None
    today = today or datetime.date.today()
    age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
    return age if 14 <= age <= 60 else None
