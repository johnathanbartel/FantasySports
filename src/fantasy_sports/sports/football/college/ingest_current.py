"""
Brings the app's college-football universe up to a CURRENT season from CFBD:

  * sync_teams_and_rosters - every FBS team for the season, and its current
    roster: QB/RB/WR/TE/PK as individual players, DL/LB/DB as team position
    groups (the same rule scripts/seed_demo_league.py used). A player already in
    the database keeps his row (and asset id) and is updated to his current team
    and position; a player no longer on an FBS roster is marked inactive so he
    drops out of every league pool (his history is kept).
  * sync_weeks - the season's regular-season calendar as Week rows, so lineups
    can be set for the real weeks.
  * sync_season_points - last completed season's full stat lines scored with
    ESPN standard scoring (season_scoring.py), the numbers the draft board shows
    and the AI values players by.

CFBD's key has a small monthly call quota, so everything is done in bulk: one
call for every roster, one for the calendar, one per conference for stats.
"""

from __future__ import annotations

import time
from collections import defaultdict

import cfbd
from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    DefenseGroup,
    DraftableAsset,
    Player,
    RealTeam,
    Universe,
    Week,
)
from fantasy_sports.sports.football.college.season_scoring import SCORING_SYSTEM, espn_standard_points
from fantasy_sports.sports.football.models import PlayerRecruitRating, PlayerSeasonPoints

OFFENSE_POSITIONS = {"QB", "RB", "WR", "TE", "PK"}
DEFENSE_GROUPS = ("DL", "LB", "DB")
CONFERENCE_ABBREVIATION = {
    "ACC": "ACC", "American Athletic": "AAC", "Big 12": "B12", "Big Ten": "B1G", "Conference USA": "CUSA",
    "FBS Independents": "Ind", "Mid-American": "MAC", "Mountain West": "MWC", "Pac-12": "PAC", "SEC": "SEC",
    "Sun Belt": "SBC",
}
MIN_ROWS_PER_CONFERENCE = 200   # fewer than this means the abbreviation was wrong, not that the league is quiet


def _retry(call, *args, **kwargs):
    for attempt in range(4):
        try:
            return call(*args, **kwargs)
        except cfbd.exceptions.ApiException as e:
            if attempt == 3 or e.status not in (429, 500, 502, 503, 504):
                raise
            time.sleep(3 * (attempt + 1))


def _universe(db: Session) -> Universe:
    universe = db.query(Universe).filter_by(code="ncaaf").first()
    if universe is None:
        raise RuntimeError("No 'ncaaf' universe - run scripts/seed_demo_league.py first")
    return universe


def sync_teams_and_rosters(db: Session, client: cfbd.ApiClient, season: int) -> dict:
    universe = _universe(db)
    teams_api = cfbd.TeamsApi(client)
    fbs = _retry(teams_api.get_fbs_teams, year=season)

    by_ref = {t.external_ref: t for t in db.query(RealTeam).filter_by(universe_id=universe.id) if t.external_ref}
    by_name = {t.name: t for t in db.query(RealTeam).filter_by(universe_id=universe.id)}
    team_of: dict[str, RealTeam] = {}
    created_teams = 0
    for t in fbs:
        row = by_ref.get(str(t.id)) or by_name.get(t.school)
        if row is None:
            row = RealTeam(universe_id=universe.id, name=t.school)
            db.add(row)
            created_teams += 1
        row.name, row.conference, row.classification, row.external_ref = t.school, t.conference, "fbs", str(t.id)
        team_of[t.school] = row
    db.flush()

    roster = _retry(teams_api.get_roster, year=season)          # every team's roster in one call
    players = {p.external_ref: p for p in db.query(Player).filter_by(universe_id=universe.id) if p.external_ref}
    asset_of = {a.player_id: a for a in db.query(DraftableAsset).filter(DraftableAsset.player_id.isnot(None))}
    seen: set[str] = set()
    roster_info: dict[str, tuple] = {}     # player ref -> (class year, CFBD recruit ids)
    created = moved = 0
    for p in roster:
        if p.team not in team_of or p.position not in OFFENSE_POSITIONS or not p.first_name or not p.last_name or not p.id:
            continue
        ref = str(p.id)
        if ref in seen:
            continue
        seen.add(ref)
        roster_info[ref] = (p.year, list(p.recruit_ids or []))
        full_name = f"{p.first_name} {p.last_name}".strip()
        team = team_of[p.team]
        row = players.get(ref)
        if row is None:
            row = Player(universe_id=universe.id, real_team_id=team.id, full_name=full_name,
                         position=p.position, external_ref=ref, active=True)
            db.add(row)
            db.flush()
            created += 1
        else:
            moved += row.real_team_id != team.id
            row.real_team_id, row.full_name, row.position, row.active = team.id, full_name, p.position, True
        if row.id not in asset_of:
            db.add(DraftableAsset(universe_id=universe.id, asset_type="player", player_id=row.id))
    retired = 0
    for ref, row in players.items():
        if ref not in seen and row.active:
            row.active = False
            retired += 1

    groups = {(g.real_team_id, g.position_group): g for g in db.query(DefenseGroup)}
    group_assets = {a.defense_group_id for a in db.query(DraftableAsset).filter(DraftableAsset.defense_group_id.isnot(None))}
    new_groups = 0
    for team in team_of.values():
        for position_group in DEFENSE_GROUPS:
            group = groups.get((team.id, position_group))
            if group is None:
                group = DefenseGroup(real_team_id=team.id, position_group=position_group)
                db.add(group)
                db.flush()
                new_groups += 1
            if group.id not in group_assets:
                db.add(DraftableAsset(universe_id=universe.id, asset_type="defense_group", defense_group_id=group.id))
    db.flush()
    return {"fbs_teams": len(fbs), "new_teams": created_teams, "players_current": len(seen), "new_players": created,
            "changed_team": moved, "retired": retired, "new_defense_groups": new_groups, "roster_info": roster_info}


def sync_weeks(db: Session, client: cfbd.ApiClient, season: int) -> int:
    universe = _universe(db)
    calendar = _retry(cfbd.GamesApi(client).get_calendar, year=season)
    have = {(w.season_year, w.week_number): w for w in db.query(Week).filter_by(universe_id=universe.id)}
    added = 0
    for w in calendar:
        if getattr(w.season_type, "value", w.season_type) != "regular":
            continue
        row = have.get((season, w.week))
        if row is None:
            row = Week(universe_id=universe.id, season_year=season, week_number=w.week, is_playoffs=False)
            db.add(row)
            added += 1
        row.start_date, row.end_date = w.start_date.date(), w.end_date.date()
    db.flush()
    return added


def sync_season_points(db: Session, client: cfbd.ApiClient, season: int) -> dict:
    """ESPN-standard points for every current player who has a stat line for `season`."""
    universe = _universe(db)
    fbs = _retry(cfbd.TeamsApi(client).get_fbs_teams, year=season)
    # The stats endpoint filters by conference ABBREVIATION (B1G, B12, AAC...); a full name such as
    # "Big Ten" silently returns almost nothing. (cfbd's own get_conferences() can't be used - the
    # client fails to parse its response.)
    conferences = sorted({t.conference for t in fbs if t.conference})
    missing = [c for c in conferences if c not in CONFERENCE_ABBREVIATION]
    if missing:
        raise RuntimeError(f"No known CFBD abbreviation for conference(s) {missing} - add them to CONFERENCE_ABBREVIATION")
    players = {p.external_ref: p for p in db.query(Player).filter_by(universe_id=universe.id) if p.external_ref}
    db.query(PlayerSeasonPoints).filter_by(season=season, scoring_system=SCORING_SYSTEM).delete()
    stats_api = cfbd.StatsApi(client)
    stored: set[int] = set()
    rows_read = 0
    for conference in conferences:
        rows = _retry(stats_api.get_player_season_stats, year=season, conference=CONFERENCE_ABBREVIATION[conference])
        rows_read += len(rows)
        print(f"  {conference}: {len(rows)} stat rows")
        if len(rows) < MIN_ROWS_PER_CONFERENCE:
            raise RuntimeError(f"{conference} returned only {len(rows)} stat rows - wrong abbreviation?")
        lines: dict[str, dict] = defaultdict(dict)
        for r in rows:
            try:
                lines[str(r.player_id)][(r.category, r.stat_type)] = float(str(r.stat).replace(",", ""))
            except ValueError:
                pass
        for ref, stat_line in lines.items():
            player = players.get(ref)
            if player is None or player.id in stored:
                continue
            points, breakdown = espn_standard_points(stat_line)
            db.add(PlayerSeasonPoints(player_id=player.id, season=season, scoring_system=SCORING_SYSTEM,
                                      points=points, breakdown=breakdown))
            stored.add(player.id)
    db.flush()
    active = sum(1 for p in players.values() if p.active)
    active_with = db.query(PlayerSeasonPoints).join(Player, Player.id == PlayerSeasonPoints.player_id).filter(
        PlayerSeasonPoints.season == season, Player.active.is_(True)).count()
    return {"conferences": len(conferences), "stat_rows": rows_read, "players_scored": len(stored),
            "active_players": active, "active_with_points": active_with}


def sync_recruit_ratings(db: Session, client: cfbd.ApiClient, season: int, roster_info: dict) -> dict:
    """Recruiting stars/rating and class year for every current player. One CFBD call
    per high-school class still on rosters (season-4 .. season). A recruit is linked to
    a player by CFBD's athlete id (the same id as the roster's player id), falling back
    to the roster's recruit ids."""
    universe = _universe(db)
    api = cfbd.RecruitingApi(client)
    by_athlete: dict[str, object] = {}
    by_recruit: dict[str, object] = {}
    for year in range(season - 4, season + 1):
        for r in _retry(api.get_recruits, year=year, classification="HighSchool"):
            for table, key in ((by_athlete, r.athlete_id), (by_recruit, r.id)):
                if key is None:
                    continue
                prev = table.get(str(key))
                if prev is None or (r.rating or 0) > (prev.rating or 0):
                    table[str(key)] = r
    have = {r.player_id: r for r in db.query(PlayerRecruitRating)}
    players = {p.external_ref: p for p in db.query(Player).filter_by(universe_id=universe.id, active=True) if p.external_ref}
    rated = 0
    for ref, player in players.items():
        class_year, recruit_ids = roster_info.get(ref, (None, []))
        rec = by_athlete.get(ref) or next((by_recruit[i] for i in map(str, recruit_ids) if i in by_recruit), None)
        row = have.get(player.id)
        if row is None:
            row = PlayerRecruitRating(player_id=player.id)
            db.add(row)
        row.class_year = class_year
        row.stars = rec.stars if rec is not None else None
        row.rating = rec.rating if rec is not None else None
        rated += rec is not None
    db.flush()
    return {"players": len(players), "rated": rated}

