"""
Scores real games for the app: for each requested week of a CURRENT season it
pulls the FBS games (and their plays) from CFBD, runs every play through the
attribution pipeline (offense split, kicking, defense by DL/LB/DB group - see
pipeline.py and shared/defense.py), and stores

    games -> plays -> play_credits          (the per-play detail behind a score)
    weekly_player_scores                    (one summed credit per player/group per week)

The credit is EPA-based ("the replay is EPA, not an invention"): a player's
weekly score is the sum of his credit on every play in his team's game, and a
defense group's is its credit on every defensive play. These raw credits are the
platform's fantasy scores until the league-level scale (see docs/scoring_scale_fit)
is fitted - scoring.to_fantasy_points is the single place that will apply it.

Only games on or after `first_game_date` count (week 1 of a league's season
starts then; earlier games are ignored). Re-running a week replaces it.

Defender names in play text are mapped to DL/LB/DB through the CURRENT season's
roster, and each team's unnamed-play split is tilted by its season-to-date defensive
stats - both pulled live here, since the cached seasons (2022-2024) are finished ones.
"""

from __future__ import annotations

import datetime
import re
from collections import defaultdict

import cfbd
from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    DefenseGroup,
    DraftableAsset,
    Game,
    Player,
    RealTeam,
    Universe,
    Week,
)
from fantasy_sports.sports.football.college.ingest_current import _retry
from fantasy_sports.sports.football.college.pipeline import process_plays
from fantasy_sports.sports.football.models import Play, PlayCredit, WeeklyPlayerScore
from fantasy_sports.sports.football.shared.attribution import FieldGoalEpLookup, YacEpaLookup
from fantasy_sports.sports.football.shared.defense import DefenseShares, PositionResolver, TeamDefenseProfile

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(name: str) -> str:
    """Name key tolerant of punctuation and generational suffixes ("Marvin Harrison Jr." == "Marvin Harrison")."""
    n = re.sub(r"[.,'’`-]", " ", (name or "").lower())
    n = _SUFFIX.sub(" ", n)
    return " ".join(n.split())


# which positions can earn a credit of each role - breaks ties between same-initial teammates (D.Moore QB vs WR)
ROLE_POSITIONS = {"passer": ("QB",), "receiver": ("WR", "TE", "RB"), "rusher": ("RB", "QB", "WR"), "kicker": ("PK",)}

_FORMATION_NUMBER = re.compile(r"^.*?#\d+\s+")
_INITIAL_DOT_LAST = re.compile(r"^([A-Za-z])\.\s*([A-Za-z].*)$")


def initial_last(name: str) -> tuple[str, str] | None:
    """("j", "kohl") for the 2026 play-text style "No Huddle-Shotgun #17 J.Kohl" (formation, jersey
    number, first initial, last name) and for a plain "J.Kohl"; None if it isn't that shape."""
    m = _INITIAL_DOT_LAST.match(_FORMATION_NUMBER.sub("", name or "").strip())
    return (m.group(1).lower(), norm_name(m.group(2))) if m else None


def _universe(db: Session) -> Universe:
    u = db.query(Universe).filter_by(code="ncaaf").first()
    if u is None:
        raise RuntimeError("No 'ncaaf' universe")
    return u


def _team_row(db: Session, universe: Universe, cache: dict, name: str, conference, classification) -> RealTeam:
    if name not in cache:
        row = RealTeam(universe_id=universe.id, name=name, conference=conference,
                       classification=str(getattr(classification, "value", classification) or "fbs"))
        db.add(row)
        db.flush()
        cache[name] = row
    return cache[name]


def defense_context(client: cfbd.ApiClient, season: int) -> tuple[PositionResolver, TeamDefenseProfile]:
    """The season roster (defender name -> DL/LB/DB) and season-to-date team defensive stats."""
    roster = _retry(cfbd.TeamsApi(client).get_roster, year=season)
    resolver = PositionResolver([
        {"id": x.id, "team": x.team, "first": x.first_name, "last": x.last_name, "position": x.position} for x in roster])
    stats = cfbd.StatsApi(client)
    rows = []
    for category in ("defensive", "interceptions"):
        for x in _retry(stats.get_player_season_stats, year=season, category=category):
            rows.append({"player_id": x.player_id, "team": x.team, "position": x.position,
                         "category": category, "stat_type": x.stat_type, "stat": x.stat})
    return resolver, TeamDefenseProfile.build(rows)


def score_weeks(
    db: Session, client: cfbd.ApiClient, season: int, weeks: list[int], first_game_date: datetime.date,
) -> dict:
    universe = _universe(db)
    resolver, profile = defense_context(client, season)
    lookup, fg_lookup, shares = YacEpaLookup(), FieldGoalEpLookup(), DefenseShares()

    teams = {t.name: t for t in db.query(RealTeam).filter_by(universe_id=universe.id)}
    player_asset: dict[tuple, int] = {}
    by_initial: dict[tuple, list] = defaultdict(list)     # (team, first initial, LAST TOKEN) -> [(asset id, position)]
    for asset, player in db.query(DraftableAsset, Player).join(Player, Player.id == DraftableAsset.player_id)             .filter(Player.active.is_(True)):
        team = next((t for t in teams.values() if t.id == player.real_team_id), None)
        if team is None:
            continue
        key = norm_name(player.full_name)
        player_asset.setdefault((key, team.name), asset.id)
        tokens = key.split()
        if len(tokens) >= 2:
            # a multi-part first name ("John Alan Richter") still matches "J.Richter" through the last token
            by_initial[(team.name, tokens[0][0], tokens[-1])].append((asset.id, player.position))

    def asset_for_player(name: str, team: str, role: str | None) -> int | None:
        exact = player_asset.get((norm_name(name), team))
        if exact is not None:
            return exact
        il = initial_last(name)
        if not il:
            return None
        found = by_initial.get((team, il[0], il[1].split()[-1])) or []
        if len(found) > 1:                                   # ambiguous initials: let the credit's role pick
            fits = [f for f in found if f[1] in ROLE_POSITIONS.get(role or "", ())]
            found = fits if len(fits) == 1 else found
        return found[0][0] if len(found) == 1 else None      # still ambiguous: unmatched

    group_asset: dict[tuple, int] = {}
    for asset, group in db.query(DraftableAsset, DefenseGroup).join(DefenseGroup, DefenseGroup.id == DraftableAsset.defense_group_id):
        team = next((t for t in teams.values() if t.id == group.real_team_id), None)
        if team is not None:
            group_asset[(team.name, group.position_group)] = asset.id

    fbs_names = {t.name for t in teams.values() if t.classification == "fbs"}
    summary = {"weeks": {}, "credit_total": 0.0, "credit_unmatched": 0.0,
               "unmatched_non_fbs_team": 0.0, "unmatched_fbs_names": defaultdict(float)}
    games_api, plays_api = cfbd.GamesApi(client), cfbd.PlaysApi(client)
    for week_number in weeks:
        week = db.query(Week).filter_by(universe_id=universe.id, season_year=season, week_number=week_number).first()
        if week is None:
            raise RuntimeError(f"No Week row for {season} week {week_number} - run scripts/ingest_current_season.py first")
        # this league season's week 1 starts on the commissioner's date; later weeks a week apart
        week.start_date = first_game_date + datetime.timedelta(days=7 * (week_number - 1))

        # replace anything already scored for this week
        old_games = [g.id for g in db.query(Game).filter_by(week_id=week.id)]
        if old_games:
            old_plays = [p.id for p in db.query(Play).filter(Play.game_id.in_(old_games))]
            if old_plays:
                db.query(PlayCredit).filter(PlayCredit.play_id.in_(old_plays)).delete(synchronize_session=False)
                db.query(Play).filter(Play.id.in_(old_plays)).delete(synchronize_session=False)
            db.query(Game).filter(Game.id.in_(old_games)).delete(synchronize_session=False)
        db.query(WeeklyPlayerScore).filter_by(week_id=week.id).delete()

        games = [
            g for g in _retry(games_api.get_games, year=season, week=week_number, season_type="regular", classification="fbs")
            if g.completed and g.start_date is not None and g.start_date.date() >= first_game_date
        ]
        by_id = {g.id: g for g in games}
        plays = [p for p in _retry(plays_api.get_plays, year=season, week=week_number, season_type="regular",
                                   classification="fbs") if p.game_id in by_id]
        plays_by_game = defaultdict(list)
        for p in plays:
            plays_by_game[p.game_id].append(p)

        week_points: dict[int, float] = defaultdict(float)
        n_plays = n_credits = 0
        seen_play_ids: set[str] = set()
        for game_id, g in by_id.items():
            home = _team_row(db, universe, teams, g.home_team, g.home_conference, g.home_classification)
            away = _team_row(db, universe, teams, g.away_team, g.away_conference, g.away_classification)
            game_row = Game(universe_id=universe.id, week_id=week.id, home_team_id=home.id, away_team_id=away.id,
                            home_score=g.home_points, away_score=g.away_points, status="final",
                            kickoff_at=g.start_date.astimezone(datetime.timezone.utc).replace(tzinfo=None))
            db.add(game_row)
            db.flush()
            game_plays = plays_by_game.get(game_id, [])
            result = process_plays(game_plays, lookup=lookup, fg_lookup=fg_lookup, resolver=resolver,
                                   defense_shares=shares, team_profile=profile)
            row_of: dict[str, Play] = {}
            credited_plays = {str(c.play.id) for c in result.play_level_credits}
            for p in game_plays:
                if (p.ppa is None and str(p.id) not in credited_plays) or str(p.id) in seen_play_ids:
                    continue
                seen_play_ids.add(str(p.id))
                off, dfn = teams.get(p.offense), teams.get(p.defense)
                row = Play(
                    game_id=game_row.id, source="cfbd", external_play_id=str(p.id), drive_number=p.drive_number,
                    play_number=p.play_number, period=p.period, clock=str(p.clock) if p.clock else None, down=p.down,
                    distance=p.distance, yard_line=p.yardline, yards_to_goal=p.yards_to_goal,
                    offense_team_id=off.id if off else None, defense_team_id=dfn.id if dfn else None,
                    play_type=p.play_type, play_text=p.play_text, yards_gained=p.yards_gained, ppa=p.ppa,
                    scoring=bool(p.scoring))
                db.add(row)
                row_of[str(p.id)] = row
                n_plays += 1
            db.flush()
            for credit in result.play_level_credits:
                row = row_of.get(str(credit.play.id))
                summary["credit_total"] += abs(credit.points)
                if row is None:
                    continue
                asset_id = (group_asset.get((credit.team, credit.position_group)) if credit.position_group
                            else asset_for_player(credit.player_name, credit.team, credit.role))
                if asset_id is None:
                    summary["credit_unmatched"] += abs(credit.points)   # a name/team we can't tie to a current player
                    if credit.team not in fbs_names:
                        summary["unmatched_non_fbs_team"] += abs(credit.points)   # an FCS opponent: no assets for it
                    else:
                        summary["unmatched_fbs_names"][(credit.player_name, credit.team)] += abs(credit.points)
                    continue
                db.add(PlayCredit(play_id=row.id, draftable_asset_id=asset_id, role=credit.role, credit_points=credit.points))
                week_points[asset_id] += credit.points
                n_credits += 1
        db.flush()
        for asset_id, points in week_points.items():
            db.add(WeeklyPlayerScore(draftable_asset_id=asset_id, week_id=week.id, points=points))
        summary["weeks"][week_number] = {"games": len(by_id), "plays": n_plays, "credits": n_credits, "scored": len(week_points)}
    db.flush()
    return summary
