"""
Seeds a realistic demo league for local prototype work: real CFBD rosters
(not fake names), a full 240-pick snake draft drawing from the same
player/defense-group pool, and three weeks of real games run through the
actual attribution pipeline (sports/football/college/pipeline.py) - so the
weekly score breakdown and standings screens have real, explainable
numbers to render, not placeholders.

Idempotent in the sense that matters for local dev: it always resets the
configured DATABASE_URL to a clean, freshly-seeded state (drop_all +
create_all), so it's safe to re-run whenever the local DB needs resetting
during design iteration. It is NOT incremental/additive - each run starts
from zero.

Uses the .venv environment (not .venv-api) - it needs both cfbd (pydantic
v1) and SQLAlchemy, and SQLAlchemy has no pydantic dependency at all so it
installs cleanly alongside cfbd; FastAPI/pydantic v2 stay isolated in
.venv-api. See requirements.txt.

Usage (from the repo root, .venv activated):
    python scripts/seed_demo_league.py
"""

import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cfbd
from dotenv import load_dotenv

from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import (
    DefenseGroup,
    DraftableAsset,
    Draft,
    DraftPick,
    Game,
    League,
    LeagueUniverse,
    LineupEntry,
    Player,
    RealTeam,
    RosterEntry,
    RosterSlotDefinition,
    Team,
    Universe,
    User,
    Week,
    WeeklyMatchup,
    WeeklyTeamScore,
)
from fantasy_sports.sports.football.models import Play, PlayCredit, WeeklyPlayerScore
from fantasy_sports.sports.football.college.pipeline import process_plays
from fantasy_sports.sports.football.shared.attribution import YacEpaLookup, FieldGoalEpLookup

YEAR = 2024
WEEKS = [1, 2, 3]
# 16, not 10: defense groups are the binding constraint, not offensive
# depth. Each real team supplies exactly 3 (DL/LB/DB), and a 12-team
# league needs 36 to guarantee every team can get one of each - 10 teams
# (30 groups) proved too shallow in practice (found by seeding, then
# checking a roster: some teams got zero LB groups because the round-8 LB
# pool ran out mid-snake-draft). 16 teams -> 48 groups, comfortable margin.
DEMO_REAL_TEAMS = [
    "Georgia", "Clemson", "Alabama", "Ohio State", "Texas",
    "Oregon", "Penn State", "Michigan", "Tennessee", "Florida State",
    "LSU", "Notre Dame", "USC", "Miami", "Oklahoma", "Wisconsin",
]
OFFENSE_POSITIONS = {"QB", "RB", "WR", "TE", "PK"}
LEAGUE_SIZE = 12
DRAFT_ROUNDS = 20
RNG = random.Random(42)


def get_api_client() -> cfbd.ApiClient:
    load_dotenv()
    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        raise SystemExit("CFBD_API_KEY not set - see .env.example at the repo root.")
    return cfbd.ApiClient(cfbd.Configuration(access_token=api_key))


def call_with_retry(func, *args, max_retries=5, **kwargs):
    """This script makes a lot of CFBD calls (10 team rosters + up to 10
    games/plays calls per week x 3 weeks), and re-running it during
    development hits CFBD's rate limit in practice, not just in theory.
    Backs off and retries on 429 rather than requiring a manual re-run."""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except cfbd.exceptions.ApiException as e:
            if e.status != 429 or attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"    rate limited, waiting {wait}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)


def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def seed_reference_data(db, client) -> dict:
    """Universe, real teams/rosters, defense groups, draftable assets,
    roster slot definitions. Everything a draft needs to exist before it
    can run."""
    teams_api = cfbd.TeamsApi(client)

    ncaaf = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
    db.add(ncaaf)
    db.flush()

    all_cfbd_teams = {t.school: t for t in call_with_retry(teams_api.get_teams, year=YEAR)}

    real_teams: dict[str, RealTeam] = {}
    assets_by_position: dict[str, list[int]] = defaultdict(list)  # position -> [draftable_asset.id]
    player_lookup: dict[tuple[str, str], int] = {}  # (full_name, real_team_name) -> draftable_asset.id
    defense_group_lookup: dict[tuple[str, str], int] = {}  # (real_team_name, group) -> draftable_asset.id

    for school in DEMO_REAL_TEAMS:
        info = all_cfbd_teams.get(school)
        real_team = RealTeam(
            universe_id=ncaaf.id, name=school,
            conference=info.conference if info else None,
            classification=info.classification if info else "fbs",
            external_ref=str(info.id) if info else None,
        )
        db.add(real_team)
        db.flush()
        real_teams[school] = real_team

        for group in ("DL", "LB", "DB"):
            dg = DefenseGroup(real_team_id=real_team.id, position_group=group)
            db.add(dg)
            db.flush()
            asset = DraftableAsset(universe_id=ncaaf.id, asset_type="defense_group", defense_group_id=dg.id)
            db.add(asset)
            db.flush()
            assets_by_position[group].append(asset.id)
            defense_group_lookup[(school, group)] = asset.id

        roster = call_with_retry(teams_api.get_roster, year=YEAR, team=school)
        for p in roster:
            if p.position not in OFFENSE_POSITIONS or not p.first_name or not p.last_name:
                continue
            full_name = f"{p.first_name} {p.last_name}"
            player = Player(
                universe_id=ncaaf.id, real_team_id=real_team.id, full_name=full_name,
                position=p.position, external_ref=str(p.id),
            )
            db.add(player)
            db.flush()
            asset = DraftableAsset(universe_id=ncaaf.id, asset_type="player", player_id=player.id)
            db.add(asset)
            db.flush()
            assets_by_position[p.position].append(asset.id)
            player_lookup[(full_name, school)] = asset.id

        print(f"  seeded {school}: roster pulled, defense groups created")

    db.commit()

    slot_defs = [
        ("QB", ["QB"], 1, True), ("RB", ["RB"], 2, True), ("WR", ["WR"], 2, True),
        ("TE", ["TE"], 1, True), ("FLEX", ["RB", "WR", "TE"], 1, True),
        ("DL", ["DL"], 1, True), ("LB", ["LB"], 1, True), ("DB", ["DB"], 1, True),
        # eligible_positions is "PK" (not "K") to match CFBD's own roster
        # position code - Player.position is seeded as "PK", so an
        # eligible_positions=["K"] slot would never match a real player.
        ("K", ["PK"], 1, True),
        ("BENCH", ["QB", "RB", "WR", "TE", "PK", "DL", "LB", "DB"], 10, False),
    ]
    slot_by_code = {}
    for code, elig, count, is_starter in slot_defs:
        slot = RosterSlotDefinition(
            universe_id=ncaaf.id, slot_code=code, eligible_positions=elig,
            slot_count=count, is_starter=is_starter,
        )
        db.add(slot)
        db.flush()
        slot_by_code[code] = slot
    db.commit()

    return {
        "ncaaf": ncaaf, "real_teams": real_teams, "assets_by_position": assets_by_position,
        "player_lookup": player_lookup, "defense_group_lookup": defense_group_lookup,
        "slot_by_code": slot_by_code,
    }


def seed_league_and_teams(db, ref) -> tuple[League, list[Team]]:
    commissioner = User(email="commissioner@example.com", display_name="Commissioner")
    db.add(commissioner)
    db.flush()

    league = League(
        name="Demo League", commissioner_user_id=commissioner.id,
        size=LEAGUE_SIZE, draft_rounds=DRAFT_ROUNDS,
    )
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=ref["ncaaf"].id))

    teams = []
    for i in range(1, LEAGUE_SIZE + 1):
        is_ai = i > 4  # 4 human-owned, 8 AI-owned
        owner = None
        if not is_ai:
            owner = User(email=f"manager{i}@example.com", display_name=f"Manager {i}")
            db.add(owner)
            db.flush()
        team = Team(
            league_id=league.id, name=f"Demo Team {i}",
            owner_user_id=owner.id if owner else None,
            is_ai_owned=is_ai,
            personality_profile={"aggression": round(RNG.uniform(0.2, 0.9), 2)} if is_ai else None,
        )
        db.add(team)
        db.flush()
        teams.append(team)

    db.commit()
    return league, teams


ROUND_TEMPLATE = {
    1: "QB", 2: "RB", 3: "RB", 4: "WR", 5: "WR", 6: "TE",
    7: "DL", 8: "LB", 9: "DB", 10: "WR",
}
BENCH_CYCLE = ["RB", "WR", "QB", "TE", "PK", "WR", "RB", "DL", "LB", "DB"]


def run_structured_draft(db, league: League, teams: list[Team], ref) -> Draft:
    """Fills all `size * draft_rounds` picks in snake order, drawing from
    the same pool for players and defense groups. Not randomized pick-by-
    pick strategy - a fixed per-round position template applied to every
    team, so every team ends up with a legal-ish starting lineup by
    construction (this is seed data, not a test of draft strategy)."""
    pools = {pos: list(ids) for pos, ids in ref["assets_by_position"].items()}
    for ids in pools.values():
        RNG.shuffle(ids)

    draft = Draft(league_id=league.id, status="completed")
    db.add(draft)
    db.flush()

    def take(position: str) -> int | None:
        pool = pools.get(position)
        if pool:
            return pool.pop()
        # fall back to any non-empty pool if that position ran out
        for other_pool in pools.values():
            if other_pool:
                return other_pool.pop()
        return None

    draft_start = datetime.now(timezone.utc) - timedelta(minutes=league.draft_rounds * len(teams) * 1.5)

    pick_number = 1
    for round_number in range(1, league.draft_rounds + 1):
        order = teams if round_number % 2 == 1 else list(reversed(teams))
        wanted = ROUND_TEMPLATE.get(round_number) or BENCH_CYCLE[round_number % len(BENCH_CYCLE)]
        for team in order:
            asset_id = take(wanted)
            db.add(DraftPick(
                draft_id=draft.id, round=round_number, pick_number=pick_number,
                team_id=team.id, draftable_asset_id=asset_id, is_autopick=True,
                picked_at=draft_start + timedelta(minutes=pick_number * 1.5),
            ))
            if asset_id is not None:
                db.add(RosterEntry(team_id=team.id, draftable_asset_id=asset_id, acquired_via="draft"))
            pick_number += 1

    db.commit()
    print(f"  draft completed: {pick_number - 1} picks across {league.draft_rounds} rounds")
    return draft


def seed_weeks_games_and_scores(db, ref):
    """Discovers each week's *unique* games across the 10 demo real teams
    first (a team filter on CFBD returns the whole game's plays, both
    sides - pulling plays per-team would double-process any game between
    two demo teams), then processes each game's plays exactly once."""
    client = get_api_client()
    plays_api = cfbd.PlaysApi(client)
    games_api = cfbd.GamesApi(client)
    lookup = YacEpaLookup()
    fg_lookup = FieldGoalEpLookup()

    weeks_by_number = {}
    # Defensive: never trust an upstream API to hand back perfectly unique
    # play ids. Found one real collision already (a postseason "week 1"
    # bug, now filtered by season_type) and a second, unexplained one
    # (possibly a rescheduled-game id reissue - 2024 had hurricane-related
    # makeup games). Rather than chase every possible cause, just refuse
    # to insert a play id twice.
    seen_play_ids: set[str] = set()

    for week_number in WEEKS:
        week = Week(universe_id=ref["ncaaf"].id, season_year=YEAR, week_number=week_number)
        db.add(week)
        db.flush()
        weeks_by_number[week_number] = week

        week_games: dict[int, cfbd.Game] = {}
        for school in DEMO_REAL_TEAMS:
            # season_type matters here: bowl/postseason games get their own
            # "week 1" numbering, so omitting this pulls unrelated December
            # postseason games in alongside the real week-1 regular season
            # game (found by a real Clemson "week 1" collision: Georgia
            # 8/31 regular season AND a Texas bowl game from 12/21, both
            # tagged week=1 under different season types).
            for g in call_with_retry(
                games_api.get_games, year=YEAR, week=week_number, team=school, season_type="regular"
            ):
                week_games[g.id] = g

        week_player_credits: dict[int, float] = defaultdict(float)  # draftable_asset.id -> points
        # Belt-and-suspenders alongside seen_play_ids: if CFBD ever hands
        # back two different game ids for what's really the same matchup
        # (a rescheduled-game id reissue would look like this), dedupe by
        # team pairing too - a play-id dedup alone would still double-
        # count that game's credits into week_player_credits even though
        # the duplicate Play rows themselves got skipped.
        seen_matchups: set[frozenset] = set()

        for g in week_games.values():
            matchup_key = frozenset({g.home_team, g.away_team})
            if matchup_key in seen_matchups:
                continue
            seen_matchups.add(matchup_key)

            home = _ensure_real_team(db, ref, g.home_team, g.home_conference, g.home_classification)
            away = _ensure_real_team(db, ref, g.away_team, g.away_conference, g.away_classification)
            game_row = Game(
                universe_id=ref["ncaaf"].id, week_id=week.id,
                home_team_id=home.id, away_team_id=away.id,
                home_score=g.home_points, away_score=g.away_points, status="final",
            )
            db.add(game_row)
            db.flush()

            plays = call_with_retry(
                plays_api.get_plays, year=YEAR, week=week_number, season_type="regular", team=g.home_team
            )
            result = process_plays(plays, lookup=lookup, fg_lookup=fg_lookup)

            # Insert raw Play rows first and flush so they get real ids,
            # then use result.play_level_credits (per-play detail, not
            # just the aggregate credits/defense_credits dicts) to create
            # matching PlayCredit rows - this is what the weekly score
            # breakdown screen actually reads (GET /players/{id}/weekly-score).
            local_play_by_cfbd_id: dict[str, Play] = {}
            for play in plays:
                if play.ppa is None:
                    continue
                play_id_str = str(play.id)
                if play_id_str in seen_play_ids:
                    continue
                seen_play_ids.add(play_id_str)
                offense_team = ref["real_teams"].get(play.offense)
                defense_team = ref["real_teams"].get(play.defense)
                play_row = Play(
                    game_id=game_row.id,
                    source="cfbd", external_play_id=play_id_str,
                    drive_number=play.drive_number, play_number=play.play_number,
                    period=play.period, clock=str(play.clock) if play.clock else None,
                    down=play.down, distance=play.distance, yard_line=play.yardline,
                    yards_to_goal=play.yards_to_goal,
                    offense_team_id=offense_team.id if offense_team else None,
                    defense_team_id=defense_team.id if defense_team else None,
                    play_type=play.play_type, play_text=play.play_text,
                    yards_gained=play.yards_gained, ppa=play.ppa, scoring=bool(play.scoring),
                )
                db.add(play_row)
                local_play_by_cfbd_id[play_id_str] = play_row
            db.flush()  # assigns .id to every Play row just added

            for credit in result.play_level_credits:
                play_row = local_play_by_cfbd_id.get(str(credit.play.id))
                if play_row is None:
                    continue  # this exact play id was deduped elsewhere - no local row to link to

                if credit.position_group:  # defense-group credit
                    asset_id = ref["defense_group_lookup"].get((credit.team, credit.position_group))
                else:  # player credit
                    asset_id = ref["player_lookup"].get((credit.player_name, credit.team))
                if asset_id is None:
                    continue  # name/team didn't match a seeded asset - can't attribute

                db.add(PlayCredit(
                    play_id=play_row.id, draftable_asset_id=asset_id,
                    role=credit.role, credit_points=credit.points,
                ))
                week_player_credits[asset_id] += credit.points

        db.flush()
        for asset_id, points in week_player_credits.items():
            db.add(WeeklyPlayerScore(draftable_asset_id=asset_id, week_id=week.id, points=points))

        db.commit()
        print(f"  week {week_number}: {len(week_games)} games, "
              f"{len(week_player_credits)} players/groups scored")

    return weeks_by_number


def _ensure_real_team(db, ref, name, conference, classification) -> RealTeam:
    if name in ref["real_teams"]:
        return ref["real_teams"][name]
    team = RealTeam(
        universe_id=ref["ncaaf"].id, name=name, conference=conference,
        classification=str(classification) if classification else None,
    )
    db.add(team)
    db.flush()
    ref["real_teams"][name] = team
    return team


def set_lineups(db, ref, teams: list[Team], weeks: dict[int, Week]):
    slot_by_code = ref["slot_by_code"]
    starter_slots = ["QB", "RB", "WR", "TE", "FLEX", "DL", "LB", "DB", "K"]

    for team in teams:
        entries = db.query(RosterEntry).filter(RosterEntry.team_id == team.id).all()
        by_position: dict[str, list[int]] = defaultdict(list)
        for e in entries:
            asset = e.draftable_asset
            pos = asset.player.position if asset.asset_type == "player" else asset.defense_group.position_group
            by_position[pos].append(asset.id)

        for week in weeks.values():
            used: set[int] = set()
            for slot_code in starter_slots:
                slot = slot_by_code[slot_code]
                for _ in range(slot.slot_count):  # e.g. RB needs 2 starters, not 1
                    candidate = None
                    for pos in slot.eligible_positions:
                        available = [a for a in by_position.get(pos, []) if a not in used]
                        if available:
                            candidate = available[0]
                            break
                    if candidate is None:
                        break  # not enough depth at this slot - leave remaining spot(s) unset
                    used.add(candidate)
                    db.add(LineupEntry(
                        team_id=team.id, week_id=week.id,
                        draftable_asset_id=candidate, roster_slot_id=slot.id,
                    ))
    db.commit()
    print("  lineups set for all teams/weeks")


def compute_matchups_and_standings(db, teams: list[Team], league: League, weeks: dict[int, Week]):
    for week in weeks.values():
        pairs = [(teams[i], teams[i + 1]) for i in range(0, len(teams), 2)]
        for team_a, team_b in pairs:
            score_a = _team_week_score(db, team_a.id, week.id)
            score_b = _team_week_score(db, team_b.id, week.id)
            winner_id = team_a.id if score_a >= score_b else team_b.id
            db.add(WeeklyMatchup(
                league_id=league.id, week_id=week.id,
                team_a_id=team_a.id, team_b_id=team_b.id,
                team_a_score=score_a, team_b_score=score_b, winner_team_id=winner_id,
            ))
            db.add(WeeklyTeamScore(team_id=team_a.id, week_id=week.id, total_points=score_a))
            db.add(WeeklyTeamScore(team_id=team_b.id, week_id=week.id, total_points=score_b))
    db.commit()
    print("  matchups + standings-feeding scores computed")


def _team_week_score(db, team_id: int, week_id: int) -> float:
    lineup_asset_ids = [
        e.draftable_asset_id
        for e in db.query(LineupEntry).filter(LineupEntry.team_id == team_id, LineupEntry.week_id == week_id)
    ]
    if not lineup_asset_ids:
        return 0.0
    scores = db.query(WeeklyPlayerScore).filter(
        WeeklyPlayerScore.draftable_asset_id.in_(lineup_asset_ids),
        WeeklyPlayerScore.week_id == week_id,
    ).all()
    return sum(s.points for s in scores)


def main():
    print("Resetting database...")
    reset_db()

    db = SessionLocal()
    client = get_api_client()

    print("Seeding reference data (universe, real teams, rosters, defense groups)...")
    ref = seed_reference_data(db, client)

    print("Seeding league and 12 teams...")
    league, teams = seed_league_and_teams(db, ref)

    print("Running structured draft...")
    run_structured_draft(db, league, teams, ref)

    print(f"Seeding {len(WEEKS)} weeks of real games/plays through the attribution pipeline...")
    weeks = seed_weeks_games_and_scores(db, ref)

    print("Setting lineups for all teams/weeks...")
    set_lineups(db, ref, teams, weeks)

    print("Computing matchups and team-week scores...")
    compute_matchups_and_standings(db, teams, league, weeks)

    league_id, team_ids = league.id, [t.id for t in teams]
    db.close()
    print(f"\nSeed complete: league_id={league_id}, team_ids={team_ids}. "
          f"Run scripts/seed_demo_league.py again any time to reset.")


if __name__ == "__main__":
    main()
