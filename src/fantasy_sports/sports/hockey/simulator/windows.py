"""
Window resolution for the hockey adapter (docs/hockey_spec.md section 5).

The adapter scores one fantasy window as one game. A window game is built from ALL of the week's stored credit for
the starters (any real game they played that week), and this module reduces it to what counts: for each player the
ONE real game that scores in this window - his only game, the Best Matchup when he has several (the opponent with
fewer NHL standings points at lock time), or a surplus game his manager moved here - and drops every other game's
credit lines and shot attempts. It also names the counting game of every starter, which the chronological period
division needs (docs/hockey_spec.md section 6.1). The choice itself is sports/hockey/pipeline.py (pure and tested on
its own); this module only applies it to simulator inputs.

Pure and offline: no database, no clock, no network.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Iterable, Mapping

from fantasy_sports.sports.hockey import pipeline
from fantasy_sports.sports.hockey.simulator.model import (
    CountingGame, HockeyGameInputs, InputError, SideInput, WindowContext,
)


def side_starters(side: SideInput) -> list[tuple[int, str]]:
    """(asset id, name) of a side's starters: the declared ones, else every credited asset."""
    if side.starters:
        return list(side.starters)
    seen: dict[int, str] = {}
    for item in (*side.lines, *side.attempts):
        seen.setdefault(item.asset_id, item.player_name)
    return sorted(seen.items())


def resolve_window_inputs(inputs: HockeyGameInputs, game_dates: Mapping[int, datetime.date], assignments=(),
                          scheduled_games: Mapping[int, Iterable[int]] | None = None,
                          context: WindowContext | None = None) -> tuple[HockeyGameInputs, dict]:
    """(inputs reduced to one counting game per player, a report of what was chosen). `inputs` must name its window
    (window_index, week_start) and every credit line must carry its real game id; `game_dates` maps game id -> date.
    `scheduled_games` maps a player to every real game his team plays that week: a game with no credit for him still
    occupies its window (a surplus game can only move to a window where he truly has no game). `context` carries
    kickoffs, opponents and the standings: without it a window with two or more games for a player cannot be resolved
    (Best Matchup needs them) and the matchup's division falls back to a common kickoff."""
    if inputs.window_index is None or inputs.week_start is None:
        raise InputError("Window scoring needs inputs that name their window (window_index and week_start)")
    if inputs.window_index not in pipeline.WINDOWS:
        raise InputError(f"window_index must be 1, 2 or 3, got {inputs.window_index}")
    start = datetime.date.fromisoformat(inputs.week_start)
    if start.weekday() != 6:
        raise InputError("week_start must be a Sunday")

    credits = []
    for key in ("home", "away"):
        for line in inputs.side(key).lines:
            if line.game_id is None:
                raise InputError(f"{line.line_id}: window scoring needs the real game id of every credit line")
            if line.game_id not in game_dates:
                raise InputError(f"{line.line_id}: no date for real game {line.game_id}")
            credits.append((line.asset_id, line.game_id, game_dates[line.game_id], line.points))
    scheduled = []
    for asset_id, game_ids in (scheduled_games or {}).items():
        for g in game_ids:
            if g not in game_dates:
                raise InputError(f"scheduled game {g} has no date")
            scheduled.append((asset_id, g, game_dates[g]))

    meta: dict[tuple[int, int], tuple] = {}
    if context is not None:
        for asset_id, game_id in {(a, g) for a, g, _, _ in credits} | {(a, g) for a, g, _ in scheduled}:
            game = context.games.get(game_id)
            team = context.asset_team.get(asset_id)
            if game is None or team is None:
                raise InputError(f"no kickoff or team for asset {asset_id} in real game {game_id}")
            meta[(asset_id, game_id)] = (game.kickoff_at, game.away_key if team == game.home_key else game.home_key)

    games = pipeline.player_game_points(credits, scheduled, meta)
    for g in games:
        if g.window[0] != start:
            raise InputError(f"game {g.game_id} ({g.game_date}) is not in the week starting {start}")
    try:
        resolved = pipeline.resolve_windows(games, assignments, context.standings if context else None)
    except pipeline.WindowError as e:
        raise InputError(str(e)) from e

    selection: dict[int, int] = {}
    chosen = {}
    for (asset_id, _), scores in resolved.items():
        score = scores[inputs.window_index - 1]
        if score.game_id is not None:
            selection[asset_id] = score.game_id
            entry = {"game_id": score.game_id, "points": round(score.points, 6), "source": score.source,
                     "dropped": list(score.dropped)}
            if score.source == "best_matchup":    # reproduces the selection made at lock
                entry["lock_at"] = score.lock_at.isoformat() if score.lock_at else None
                entry["opponent_points"] = {str(g): p for g, p in score.opponent_points}
            chosen[str(asset_id)] = entry

    def keep(side: SideInput) -> SideInput:
        return dataclasses.replace(
            side, lines=tuple(l for l in side.lines if selection.get(l.asset_id) == l.game_id),
            attempts=tuple(a for a in side.attempts if selection.get(a.asset_id) == a.game_id))

    counting: list[CountingGame] = []
    if context is not None:
        for key in ("home", "away"):
            for asset_id, name in side_starters(inputs.side(key)):
                game_id = selection.get(asset_id)
                if game_id is None:
                    continue                         # an empty slot: no game, no real periods
                game = context.games[game_id]
                team = context.asset_team[asset_id]
                at_home = team == game.home_key
                counting.append(CountingGame(
                    asset_id, context.asset_names.get(asset_id, name), key, game_id, game.kickoff_at,
                    f"vs {game.away_label}" if at_home else f"@ {game.home_label}"))
        counting.sort(key=lambda c: (c.side, c.asset_id))

    reduced = dataclasses.replace(inputs, home=keep(inputs.home), away=keep(inputs.away),
                                  counting_games=tuple(counting))
    report = {"week_start": inputs.week_start, "window_index": inputs.window_index, "chosen": chosen,
              "moved": [{"asset_id": a.asset_id, "game_id": a.game_id, "target_window": a.target_window} for a in assignments]}
    return reduced, report
