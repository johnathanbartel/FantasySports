"""NHL game type -> the season_type stored on games.

An NHL game id is 10 digits: season year (4), game type (2), game number (4).
Game type 02 is a regular-season game and 03 is a playoff game. MoneyPuck's
5-digit game ids drop the year and the type's leading zero, so 20001 is
regular-season game 1 and 30111 is playoff game 111.
"""
from __future__ import annotations

REGULAR = "regular"
PLAYOFF = "playoff"

_BY_TYPE_DIGITS = {"02": REGULAR, "03": PLAYOFF}


def season_type_from_nhl_game_id(game_id: int | str | None) -> str | None:
    """"regular" or "playoff" for a 10-digit NHL id or a 5-digit MoneyPuck id.

    Anything else (preseason, all-star, an id of another shape) gives None, so
    the game is left unmarked rather than guessed.
    """
    s = str(game_id).strip() if game_id is not None else ""
    if not s.isdigit():
        return None
    if len(s) == 10:
        return _BY_TYPE_DIGITS.get(s[4:6])
    if len(s) == 5:
        return _BY_TYPE_DIGITS.get("0" + s[0])
    return None
