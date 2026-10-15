import datetime
import random
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, assert_owns_team, current_user, require_commissioner_mode
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.schemas import (
    ConferenceCreateRequest, ConferenceOut, ConferenceUpdateRequest,
    DivisionCreateRequest, DivisionOut, DivisionUpdateRequest,
    LeagueAlignmentRequest, LeagueCreateRequest, LeagueOut, LeagueRenameRequest, LeagueSeasonOut, LeagueSeasonSummary, LeagueSettingsRequest, RolloverRequest,
    FuturePicksOut, KeeperPlayerOut, KeepersOut, KeepersRequest,
    LeagueTeamsSetupRequest, LeagueTeamsSetupResponse, RosterSlotOut,
    SandboxLeagueRequest, SetupStructureRequest, StandingsRow, TeamAlignmentItem, TeamAssignOut,
    TeamAssignRequest, TeamInviteOut, TeamInviteRequest, TeamOut,
    TeamSetupResult, WeekOut,
)
from fantasy_sports.platform import calendar, lineups, scoring, season_rollover
from fantasy_sports.platform.conferences import service as conferences
from fantasy_sports.platform.db.models.core import (
    AuctionDraft, Conference, Division, Draft, DraftableAsset, KeeperCandidate, KeeperSubmission, League, LeagueSeason, LeagueUniverse, LineupEntry,
    RecruitingDraft, RecruitingSettings, RosterEntry, RosterSlotDefinition,
    Team, Universe, User, Week, WeeklyMatchup,
)
from fantasy_sports.platform.defense_modes import DEFENSE_MODES, roster_size
from fantasy_sports.platform.league_features import draft_state, is_college, roster_spots
from fantasy_sports.platform.league_lifecycle import delete_league as purge_league
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.nfl_reference import seed_nfl_reference_data
from fantasy_sports.platform.team_tiers import PROGRAM_SIZES
from fantasy_sports.platform.db.session import get_db

router = APIRouter(prefix="/leagues", tags=["leagues"])


@router.post("", response_model=LeagueOut)
def create_league(
    payload: LeagueCreateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    if payload.commissioner_user_id != user.id:
        raise HTTPException(403, "You can only create a league with yourself as commissioner")
    has_nhl = "nhl" in payload.universe_codes
    if has_nhl:
        series_length_days = 7 if payload.series_length_days is None else payload.series_length_days
        if series_length_days not in (5, 7, 10):
            raise HTTPException(400, "NHL series length must be 5, 7, or 10 days")
    else:
        if payload.series_length_days is not None:
            raise HTTPException(400, "series_length_days is only available for NHL leagues")
        series_length_days = None
    if "nfl" in payload.universe_codes:
        # NFL reference data (universe + roster slots) is additive and idempotent;
        # add it on demand so a fresh database needs no seeding script first.
        try:
            seed_nfl_reference_data(db)
        except RuntimeError as e:
            raise HTTPException(400, f"NFL reference data conflict: {e}")
    if "nhl" in payload.universe_codes:
        # NHL reference data is also safe to create on demand.  Real teams and
        # players remain a separate ingestion concern; this establishes the
        # universe and its six-position lineup so league creation can proceed.
        try:
            seed_hockey_reference_data(db)
        except RuntimeError as e:
            raise HTTPException(400, f"NHL reference data conflict: {e}")
    universes = db.query(Universe).filter(Universe.code.in_(payload.universe_codes)).all()
    if len(universes) != len(payload.universe_codes):
        found = {u.code for u in universes}
        missing = set(payload.universe_codes) - found
        raise HTTPException(400, f"Unknown universe code(s): {sorted(missing)}")

    for code, mode in payload.defense_modes.items():
        if mode not in DEFENSE_MODES:
            raise HTTPException(400, f"Invalid defense_mode '{mode}' for universe '{code}'")
    unsupported = [
        u.code for u in universes
        if payload.defense_modes.get(u.code) == "individual" and not u.supports_individual_defense
    ]
    if unsupported:
        raise HTTPException(400, f"Universe(s) {sorted(unsupported)} don't support individual defense")
    no_team_mode = [
        u.code for u in universes
        if payload.defense_modes.get(u.code) == "team"
        and not db.query(RosterSlotDefinition).filter_by(universe_id=u.id, defense_mode="team").first()
    ]
    if no_team_mode:
        raise HTTPException(400, f"Universe(s) {sorted(no_team_mode)} don't offer whole-team defense")

    if payload.program_size_mode not in ("team_choice", "dynamic"):
        raise HTTPException(400, f"Invalid program_size_mode '{payload.program_size_mode}'")
    if payload.program_size_mode == "dynamic":
        raise HTTPException(
            400,
            "'dynamic' program_size_mode isn't implemented yet (it needs multi-season/season-rollover "
            "support this platform doesn't have) - use 'team_choice' for now",
        )

    if payload.draft_type not in ("recruit", "snake"):
        raise HTTPException(400, f"Invalid draft_type '{payload.draft_type}'")

    first = universes[0]
    draft_rounds = payload.draft_rounds
    spots = roster_size(db, first.id, payload.defense_modes.get(first.code, "group"))
    if draft_rounds is None:
        draft_rounds = spots or 20
    elif spots and draft_rounds != spots and not any(u.level == "college" for u in universes):
        # Outside college leagues the draft fills every roster spot, so it has exactly one round per spot.
        raise HTTPException(400, f"Draft rounds must equal the roster size ({spots}) so every roster spot is filled")
    league = League(
        name=payload.name,
        commissioner_user_id=payload.commissioner_user_id,
        size=payload.size,
        draft_rounds=draft_rounds,
        program_size_mode=payload.program_size_mode,
        draft_type=payload.draft_type,
        draft_scheduled_at=payload.draft_scheduled_at,
        series_length_days=series_length_days,
    )
    db.add(league)
    db.flush()  # assigns league.id without committing yet

    for universe in universes:
        db.add(LeagueUniverse(
            league_id=league.id, universe_id=universe.id,
            defense_mode=payload.defense_modes.get(universe.code, "group"),
        ))

    if any(u.level == "college" for u in universes):
        conferences.create_baseline_conferences(db, league.id)
    else:
        if "nhl" in payload.universe_codes:
            names = ["Eastern Conference", "Western Conference"]
        elif "nfl" in payload.universe_codes:
            names = ["AFC", "NFC"]
        elif "mlb" in payload.universe_codes:
            names = ["American League", "National League"]
        else:
            names = ["Eastern Conference", "Western Conference"]
        for name in names:
            db.add(Conference(league_id=league.id, program_size="large", name=name))
        db.flush()

    if payload.draft_type == "recruit":
        if payload.recruiting_period_seconds < 1:
            raise HTTPException(400, "Recruiting period length must be at least 1 second")
        if payload.recruiting_buffer_seconds < 0:
            raise HTTPException(400, "Buffer between periods can't be negative")
        db.add(RecruitingSettings(
            league_id=league.id, period_seconds=payload.recruiting_period_seconds,
            buffer_seconds=payload.recruiting_buffer_seconds))

    db.commit()
    db.refresh(league)
    return league


@router.get("/mine", response_model=list[LeagueOut])
def my_leagues(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Leagues the caller commissions or owns a team in, newest first."""
    owned = {t.league_id for t in db.query(Team).filter(Team.owner_user_id == user.id)}
    rows = db.query(League).filter(
        (League.commissioner_user_id == user.id) | (League.id.in_(owned or {0}))
    ).order_by(League.id.desc()).all()
    return rows


class DashboardLeague(BaseModel):
    league_id: int
    name: str
    sports: list[str]           # universe codes: ncaaf / nfl / nhl ...
    draft_type: str
    size: int
    teams_filled: int
    is_commissioner: bool
    my_team_ids: list[int]
    my_team_names: list[str]
    draft_state: str            # not_started / in_progress / complete
    draft_scheduled_at: datetime.datetime | None


def _draft_state(db: Session, league: League) -> str:
    return draft_state(db, league.id)


@router.get("/mine/dashboard", response_model=list[DashboardLeague])
def my_dashboard(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Every league the caller commissions or has a team in, across all sports, with the facts the site dashboard
    shows: sport(s), how full the league is, the caller's team(s), and where its draft stands."""
    my_teams = db.query(Team).filter(Team.owner_user_id == user.id).order_by(Team.id).all()
    leagues = db.query(League).filter(
        (League.commissioner_user_id == user.id) | (League.id.in_({t.league_id for t in my_teams} or {0}))
    ).order_by(League.id.desc()).all()
    ids = [lg.id for lg in leagues] or [0]
    sports: dict[int, list[str]] = defaultdict(list)
    for league_id, code in (
        db.query(LeagueUniverse.league_id, Universe.code)
        .join(Universe, Universe.id == LeagueUniverse.universe_id)
        .filter(LeagueUniverse.league_id.in_(ids)).order_by(Universe.code)
    ):
        sports[league_id].append(code)
    filled: dict[int, int] = defaultdict(int)
    for (league_id,) in db.query(Team.league_id).filter(Team.league_id.in_(ids)):
        filled[league_id] += 1
    mine: dict[int, list[Team]] = defaultdict(list)
    for t in my_teams:
        mine[t.league_id].append(t)
    return [
        DashboardLeague(
            league_id=lg.id, name=lg.name, sports=sports[lg.id], draft_type=lg.draft_type, size=lg.size,
            teams_filled=filled[lg.id], is_commissioner=lg.commissioner_user_id == user.id,
            my_team_ids=[t.id for t in mine[lg.id]], my_team_names=[t.name for t in mine[lg.id]],
            draft_state=_draft_state(db, lg), draft_scheduled_at=lg.draft_scheduled_at,
        )
        for lg in leagues
    ]


@router.post("/sandbox", response_model=LeagueOut)
def create_sandbox_league(
    payload: SandboxLeagueRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """COMMISSIONER MODE (premium): creates a recruiting-draft league in which
    EVERY team is AI-owned and the caller is only its commissioner - for testing
    and for running a league without playing in it. Teams are spread evenly over
    the three program sizes (12 teams -> 4 Large / 4 Moderate / 4 Small), one
    conference per size."""
    require_commissioner_mode(db, user)
    universe = db.query(Universe).filter(Universe.code == "ncaaf").first()
    if universe is None:
        raise HTTPException(400, "No college football universe is set up")
    if payload.size < 3 or payload.draft_rounds < 1 or payload.recruiting_period_seconds < 1:
        raise HTTPException(400, "League size must be at least 3, with at least 1 roster spot and a positive week length")
    league = League(
        name=payload.name, commissioner_user_id=user.id, size=payload.size, draft_rounds=payload.draft_rounds,
        program_size_mode="team_choice", draft_type="recruit", draft_scheduled_at=None,
    )
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=universe.id, defense_mode="group"))
    conferences.create_baseline_conferences(db, league.id)
    db.add(RecruitingSettings(league_id=league.id, period_seconds=payload.recruiting_period_seconds, buffer_seconds=20))
    db.flush()
    fill_with_ai_teams(league.id, db, user)   # commits
    db.refresh(league)
    return league


@router.get("/{league_id}/roster-slots", response_model=list[RosterSlotOut])
def league_roster_slots(league_id: int, db: Session = Depends(get_db)):
    """The lineup slots this league fields each week, starters first."""
    if db.get(League, league_id) is None:
        raise HTTPException(404, "League not found")
    try:
        slots = lineups.league_slots(db, league_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return [
        RosterSlotOut(slot_code=s.slot_code, eligible_positions=s.eligible_positions,
                      slot_count=s.slot_count, is_starter=s.is_starter)
        for s in slots
    ]


@router.get("/{league_id}/weeks", response_model=list[WeekOut])
def league_weeks(league_id: int, db: Session = Depends(get_db)):
    """The real-calendar weeks a lineup can be set for: the newest season's."""
    universe_ids = [lu.universe_id for lu in db.query(LeagueUniverse).filter_by(league_id=league_id)]
    if not universe_ids:
        raise HTTPException(404, "League not found or has no universe")
    weeks = scoring.season_weeks(db, league_id)
    scored = scoring.scored_week_ids(db, [w.id for w in weeks])
    return [WeekOut(id=w.id, season_year=w.season_year, week_number=w.week_number, start_date=w.start_date,
                    locked=scoring.is_locked(w), scored=w.id in scored) for w in weeks]


@router.post("/{league_id}/lineups/auto")
def auto_set_ai_lineups(
    league_id: int, week_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: sets every AI-owned team's best lineup for the week
    - what the AI teams do on their own, run on demand. Human-owned teams are
    left alone."""
    assert_commissioner(db, user, league_id)
    week = db.get(Week, week_id)
    universe_ids = {lu.universe_id for lu in db.query(LeagueUniverse).filter_by(league_id=league_id)}
    if week is None or week.universe_id not in universe_ids:
        raise HTTPException(400, "That week isn't part of this league's universe")
    done = []
    for team in db.query(Team).filter_by(league_id=league_id, is_ai_owned=True).order_by(Team.id):
        pairs = lineups.build_lineup(db, team, lineups.best_lineup(db, team))
        lineups.save_lineup(db, team, week_id, pairs)
        done.append({"team_id": team.id, "players": len(pairs)})
    db.commit()
    return {"week_id": week_id, "teams": done}


@router.get("/{league_id}", response_model=LeagueOut)
def get_league(league_id: int, db: Session = Depends(get_db)):
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if season_rollover.process_keeper_deadline(db, league):
        db.commit()
    if not is_college(db, league.id):
        spots = roster_spots(db, league.id)
        if spots and league.draft_rounds != spots:
            league.draft_rounds = spots
            db.commit()
            db.refresh(league)
    return league


@router.patch("/{league_id}", response_model=LeagueOut)
def rename_league(
    league_id: int, payload: LeagueRenameRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: renames the league."""
    league = assert_commissioner(db, user, league_id)
    new_name = payload.name.strip()
    if not new_name:
        raise HTTPException(400, "League name cannot be empty")
    league.name = new_name
    db.commit()
    db.refresh(league)
    return league


@router.patch("/{league_id}/settings", response_model=LeagueOut)
def update_league_settings(
    league_id: int, payload: LeagueSettingsRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: changes the league's settings. Anyone can read them through GET /leagues/{id}."""
    league = assert_commissioner(db, user, league_id)
    sent = payload.model_fields_set
    if payload.league_format is not None:
        league.league_format = payload.league_format
    if payload.keeper_draft_style is not None:
        league.keeper_draft_style = payload.keeper_draft_style
    if payload.pick_trade_seasons is not None:
        league.pick_trade_seasons = payload.pick_trade_seasons
    if "keeper_limit" in sent:
        league.keeper_limit = payload.keeper_limit
    if "keeper_deadline" in sent:
        if league.offseason and league.league_format == "keeper" and not league.keepers_finalized and payload.keeper_deadline is None:
            raise HTTPException(409, "Keeper selection is open, so it needs a deadline")
        league.keeper_deadline = payload.keeper_deadline
    db.commit()
    db.refresh(league)
    return league


@router.post("/{league_id}/rollover", response_model=LeagueOut)
def roll_over_season(
    league_id: int, payload: RolloverRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: archives the finished season and opens the offseason (see platform/season_rollover.py)."""
    league = assert_commissioner(db, user, league_id)
    try:
        season_rollover.roll_over(db, league, payload.offseason_draft_rounds, payload.refill_rounds,
                                  champion_team_id=payload.champion_team_id)
    except season_rollover.RolloverError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    db.commit()
    db.refresh(league)
    return league


def _keepers_out(db: Session, league: League, team: Team) -> KeepersOut:
    cands = (db.query(KeeperCandidate).filter_by(team_id=team.id, season_number=league.season_number)
             .order_by(KeeperCandidate.roster_order).all())
    assets = {a.id: a for a in db.query(DraftableAsset).filter(DraftableAsset.id.in_([c.draftable_asset_id for c in cands] or [0]))}
    submitted = db.query(KeeperSubmission).filter_by(team_id=team.id, season_number=league.season_number).first() is not None
    return KeepersOut(
        team_id=team.id, season_number=league.season_number, finalized=league.keepers_finalized,
        deadline=league.keeper_deadline, cap=season_rollover.keeper_cap(db, league), submitted=submitted,
        players=[KeeperPlayerOut(draftable_asset_id=c.draftable_asset_id,
                                 display_name=draftable_asset_display_name(assets[c.draftable_asset_id]),
                                 position=draftable_asset_position(assets[c.draftable_asset_id]),
                                 games_played=c.games_played, kept=c.kept) for c in cands])


@router.get("/{league_id}/teams/{team_id}/keepers", response_model=KeepersOut)
def get_keepers(league_id: int, team_id: int, db: Session = Depends(get_db)):
    """A Keeper team's candidates, games played last season, and which ones it keeps. Closes keepers if the deadline passed."""
    league = db.get(League, league_id)
    team = db.get(Team, team_id)
    if league is None or team is None or team.league_id != league_id:
        raise HTTPException(404, "Team not found in this league")
    if season_rollover.process_keeper_deadline(db, league):
        db.commit()
    return _keepers_out(db, league, team)


@router.put("/{league_id}/teams/{team_id}/keepers", response_model=KeepersOut)
def set_keepers(
    league_id: int, team_id: int, payload: KeepersRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """The team's owner picks which players to keep; the rest are released when keepers close."""
    league = db.get(League, league_id)
    team = db.get(Team, team_id)
    if league is None or team is None or team.league_id != league_id:
        raise HTTPException(404, "Team not found in this league")
    assert_owns_team(db, user, team_id)
    try:
        season_rollover.submit_keepers(db, league, team, payload.draftable_asset_ids)
    except season_rollover.RolloverError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    db.commit()
    return _keepers_out(db, league, team)


@router.get("/{league_id}/teams/{team_id}/future-picks", response_model=FuturePicksOut)
def get_future_picks(league_id: int, team_id: int, db: Session = Depends(get_db)):
    """A Future Draft Pick Redraft team's picks in the next draft (shown on its roster page below the injured list)."""
    league = db.get(League, league_id)
    team = db.get(Team, team_id)
    if league is None or team is None or team.league_id != league_id:
        raise HTTPException(404, "Team not found in this league")
    if league.league_format != "future_pick_redraft":
        raise HTTPException(400, "Draft picks are part of Future Draft Pick Redraft leagues")
    return season_rollover.future_picks(db, league, team_id)


@router.post("/{league_id}/keepers/finalize", response_model=LeagueOut)
def finalize_keepers(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner action: closes keeper selection now. Teams that haven't picked are trimmed automatically."""
    league = assert_commissioner(db, user, league_id)
    if not league.offseason or league.keepers_finalized:
        raise HTTPException(409, "There is no open keeper selection to close")
    season_rollover.finalize_keepers(db, league)
    db.commit()
    db.refresh(league)
    return league


@router.get("/{league_id}/seasons", response_model=list[LeagueSeasonSummary])
def list_seasons(league_id: int, db: Session = Depends(get_db)):
    """League history: every archived season, newest first, with its final standings."""
    rows = db.query(LeagueSeason).filter_by(league_id=league_id).order_by(LeagueSeason.season_number.desc()).all()
    return [LeagueSeasonSummary(season_number=r.season_number, league_format=r.league_format, archived_at=r.archived_at,
                                champion_team_id=r.champion_team_id, standings=r.data.get("standings", [])) for r in rows]


@router.get("/{league_id}/seasons/{season_number}", response_model=LeagueSeasonOut)
def get_season(league_id: int, season_number: int, db: Session = Depends(get_db)):
    r = db.query(LeagueSeason).filter_by(league_id=league_id, season_number=season_number).one_or_none()
    if r is None:
        raise HTTPException(404, "That season isn't in the league's history")
    return LeagueSeasonOut(season_number=r.season_number, league_format=r.league_format, archived_at=r.archived_at,
                           champion_team_id=r.champion_team_id, standings=r.data.get("standings", []), data=r.data)


@router.delete("/{league_id}")
def delete_league(
    league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: deletes the league and all associated child records."""
    assert_commissioner(db, user, league_id)
    purge_league(db, league_id)
    db.commit()
    return {"status": "deleted", "league_id": league_id}


@router.get("/{league_id}/teams", response_model=list[TeamOut])
def get_league_teams(league_id: int, db: Session = Depends(get_db)):
    """Team-name resolution for any screen that shows picks/lineups/matchups
    by team - those only carry team_id, not a display name."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    teams = db.query(Team).filter(Team.league_id == league_id).order_by(Team.id).all()
    if not teams:
        raise HTTPException(404, "League not found or has no teams")
    user_map = {u.id: u for u in db.query(User)}
    divs_map = {d.id: d.name for d in db.query(Division).filter(Division.league_id == league_id)}
    confs_map = {c.id: c.name for c in db.query(Conference).filter(Conference.league_id == league_id)}
    results = []
    for t in teams:
        u = user_map.get(t.owner_user_id) if t.owner_user_id else None
        if t.is_ai_owned or not t.owner_user_id:
            is_claimed = False
            invite_status = "ai"
            invite_url = None
        elif t.owner_user_id == league.commissioner_user_id:
            is_claimed = True
            invite_status = "claimed"
            invite_url = None
        elif u and u.password_hash is not None:
            is_claimed = True
            invite_status = "claimed"
            invite_url = None
        else:
            is_claimed = False
            invite_status = "pending_claim"
            invite_url = f"/static/join-league.html?league_id={league.id}&team_id={t.id}&email={u.email if u else ''}"

        results.append(TeamOut(
            id=t.id,
            league_id=t.league_id,
            name=t.name,
            owner_user_id=t.owner_user_id,
            is_ai_owned=t.is_ai_owned,
            program_size=t.program_size,
            conference_id=t.conference_id,
            division_id=t.division_id,
            conference_name=confs_map.get(t.conference_id),
            division_name=divs_map.get(t.division_id),
            season_goal=t.season_goal,
            owner_name=u.display_name if u else None,
            owner_email=u.email if u else None,
            is_claimed=is_claimed,
            invite_status=invite_status,
            invite_url=invite_url,
        ))
    return results


AI_TEAM_NAMES = [
    "Ridgeline Rams", "Harbor City Hawks", "Prairie Thunder", "Summit State Stags",
    "Iron Valley Bears", "Gulf Coast Gators", "Cascade Wolves", "Delta Ravens",
    "Bluegrass Bandits", "Mesa Scorpions", "Lakeshore Lynx", "Timberline Tigers",
    "Capitol Comets", "Sunbelt Stallions", "Northern Pines", "Redrock Raptors",
]


@router.post("/{league_id}/ai-teams", response_model=list[TeamOut])
def fill_with_ai_teams(league_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Commissioner action: adds AI-owned teams until the league reaches its
    size, spreading them across the program-size tiers as evenly as possible
    given the teams already there (a 12-team league with the commissioner in
    a Large slot ends up 4 large / 4 moderate / 4 small). Each AI team goes
    into its tier's least-populated conference like any other team."""
    league = assert_commissioner(db, user, league_id)
    teams = db.query(Team).filter(Team.league_id == league_id).all()
    tier_counts = {size: 0 for size in PROGRAM_SIZES}
    for t in teams:
        tier_counts[t.program_size] += 1
    used_names = {t.name for t in teams}
    spare_names = iter(n for n in AI_TEAM_NAMES if n not in used_names)

    created = []
    while len(teams) + len(created) < league.size:
        size = min(PROGRAM_SIZES, key=lambda s: (tier_counts[s], PROGRAM_SIZES.index(s)))
        conference = conferences.assign_team_to_conference(db, league_id, size)
        name = next(spare_names, None) or f"AI Team {len(teams) + len(created) + 1}"
        team = Team(
            league_id=league_id, name=name, is_ai_owned=True, program_size=size,
            conference_id=conference.id,
        )
        db.add(team)
        db.flush()  # so the next assignment sees this team's conference
        tier_counts[size] += 1
        created.append(team)
    db.commit()
    return created


@router.get("/defaults/ai-team-names", response_model=list[str])
def get_default_ai_team_names():
    return AI_TEAM_NAMES


@router.post("/{league_id}/teams-setup", response_model=LeagueTeamsSetupResponse)
def setup_league_teams(
    league_id: int, payload: LeagueTeamsSetupRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: sets up all teams for the league in the league creation wizard.
    For each team, indicates if the team is AI or assigns an email to the team, and generates
    invitations for human managers."""
    league = assert_commissioner(db, user, league_id)

    if len(payload.teams) != league.size:
        raise HTTPException(
            400, f"Expected exactly {league.size} teams for this league, but received {len(payload.teams)}"
        )

    draft = db.query(Draft).filter(Draft.league_id == league_id).first()
    if draft and draft.status != "pending":
        raise HTTPException(400, "Draft has already started; teams cannot be reconfigured")

    # If teams already exist in this league (e.g. commissioner re-configuring before draft),
    # replace them
    existing_teams = db.query(Team).filter(Team.league_id == league_id).all()
    if existing_teams:
        team_ids = [t.id for t in existing_teams]
        db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids)).delete(synchronize_session=False)
        db.query(LineupEntry).filter(LineupEntry.team_id.in_(team_ids)).delete(synchronize_session=False)
        for t in existing_teams:
            db.delete(t)
        db.flush()

    results: list[TeamSetupResult] = []
    has_commissioner = False

    for item in payload.teams:
        team_name = item.name.strip()
        if not team_name:
            raise HTTPException(400, "Every team must have a name")

        prog_size = item.program_size if item.program_size in PROGRAM_SIZES else "large"
        conference = conferences.assign_team_to_conference(db, league.id, prog_size)

        if item.is_commissioner:
            has_commissioner = True
            team = Team(
                league_id=league.id,
                name=team_name,
                owner_user_id=user.id,
                is_ai_owned=False,
                program_size=prog_size,
                conference_id=conference.id,
            )
            db.add(team)
            db.flush()
            results.append(TeamSetupResult(
                id=team.id,
                name=team.name,
                is_ai_owned=False,
                owner_user_id=user.id,
                email=user.email,
                program_size=prog_size,
                invite_url=None,
            ))
        elif item.is_ai:
            team = Team(
                league_id=league.id,
                name=team_name,
                owner_user_id=None,
                is_ai_owned=True,
                program_size=prog_size,
                conference_id=conference.id,
            )
            db.add(team)
            db.flush()
            results.append(TeamSetupResult(
                id=team.id,
                name=team.name,
                is_ai_owned=True,
                owner_user_id=None,
                email=None,
                program_size=prog_size,
                invite_url=None,
            ))
        else:
            email_val = (item.email or "").strip().lower()
            if not email_val or "@" not in email_val or "." not in email_val:
                raise HTTPException(400, f"Please provide a valid email address for team '{team_name}'")

            target_user = db.query(User).filter(func.lower(User.email) == email_val).first()
            if target_user is None:
                target_user = User(
                    email=email_val,
                    display_name=email_val.split("@")[0],
                    password_hash=None,
                )
                db.add(target_user)
                db.flush()

            team = Team(
                league_id=league.id,
                name=team_name,
                owner_user_id=target_user.id,
                is_ai_owned=False,
                program_size=prog_size,
                conference_id=conference.id,
            )
            db.add(team)
            db.flush()
            invite_url = f"/static/join-league.html?league_id={league.id}&team_id={team.id}&email={email_val}"
            results.append(TeamSetupResult(
                id=team.id,
                name=team.name,
                is_ai_owned=False,
                owner_user_id=target_user.id,
                email=email_val,
                program_size=prog_size,
                invite_url=invite_url,
            ))

    if not has_commissioner:
        raise HTTPException(400, "One of the teams must be marked as the commissioner's team")

    db.commit()
    return LeagueTeamsSetupResponse(league_id=league.id, teams=results)


@router.post("/{league_id}/teams/{team_id}/invite", response_model=TeamInviteOut)
def invite_team_manager(
    league_id: int, team_id: int, payload: TeamInviteRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: assigns an email to a team and returns the invite link."""
    league = assert_commissioner(db, user, league_id)
    team = db.get(Team, team_id)
    if team is None or team.league_id != league.id:
        raise HTTPException(404, "Team not found in this league")

    email_val = payload.email.strip().lower()
    if "@" not in email_val or "." not in email_val:
        raise HTTPException(400, "Please provide a valid email address")

    target_user = db.query(User).filter(func.lower(User.email) == email_val).first()
    if target_user is None:
        target_user = User(
            email=email_val,
            display_name=email_val.split("@")[0],
            password_hash=None,
        )
        db.add(target_user)
        db.flush()

    team.owner_user_id = target_user.id
    team.is_ai_owned = False
    db.commit()

    invite_url = f"/static/join-league.html?league_id={league.id}&team_id={team.id}&email={email_val}"
    return TeamInviteOut(
        team_id=team.id,
        league_id=league.id,
        email=email_val,
        invite_url=invite_url,
        status="invited",
    )


@router.post("/{league_id}/teams/{team_id}/assign", response_model=TeamAssignOut)
def assign_team_manager(
    league_id: int,
    team_id: int,
    payload: TeamAssignRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Commissioner action: assigns an AI team (or reassigns a team) to a specific user or email,
    or converts a team back to AI. Allowed at any point in the league lifecycle."""
    league = assert_commissioner(db, user, league_id)
    team = db.get(Team, team_id)
    if team is None or team.league_id != league.id:
        raise HTTPException(404, "Team not found in this league")

    if payload.is_ai:
        team.owner_user_id = None
        team.is_ai_owned = True
        if payload.team_name and payload.team_name.strip():
            team.name = payload.team_name.strip()
        db.commit()
        db.refresh(team)
        return TeamAssignOut(
            team_id=team.id,
            league_id=league.id,
            team_name=team.name,
            owner_user_id=None,
            owner_name="AI",
            owner_email=None,
            is_ai_owned=True,
            invite_url=None,
            status="converted_to_ai",
        )

    target_user: User | None = None
    if payload.user_id is not None:
        target_user = db.get(User, payload.user_id)
        if target_user is None:
            raise HTTPException(404, f"User ID {payload.user_id} not found")
    elif payload.email and payload.email.strip():
        email_val = payload.email.strip().lower()
        if "@" not in email_val or "." not in email_val:
            raise HTTPException(400, "Please provide a valid email address")
        target_user = db.query(User).filter(func.lower(User.email) == email_val).first()
        if target_user is None:
            target_user = User(
                email=email_val,
                display_name=email_val.split("@")[0],
                password_hash=None,
            )
            db.add(target_user)
            db.flush()
    else:
        raise HTTPException(400, "Either user_id or a valid email address is required")

    team.owner_user_id = target_user.id
    team.is_ai_owned = False
    if payload.team_name and payload.team_name.strip():
        team.name = payload.team_name.strip()
    db.commit()
    db.refresh(team)

    invite_url = f"/static/join-league.html?league_id={league.id}&team_id={team.id}&email={target_user.email}"
    return TeamAssignOut(
        team_id=team.id,
        league_id=league.id,
        team_name=team.name,
        owner_user_id=target_user.id,
        owner_name=target_user.display_name,
        owner_email=target_user.email,
        is_ai_owned=False,
        invite_url=invite_url,
        status="assigned",
    )


def are_conferences_editable(db: Session, league: League) -> tuple[bool, str]:
    """
    Conferences and divisions can be edited before and after the season.
    They cannot be modified during the active season while games are in progress.
    """
    has_nhl = db.query(LeagueUniverse).join(Universe, Universe.id == LeagueUniverse.universe_id).filter(
        LeagueUniverse.league_id == league.id, Universe.code == "nhl"
    ).count() > 0

    if has_nhl:
        from fantasy_sports.platform.db.models.core import SeriesDailyGame, LeagueSeries
        games = (
            db.query(SeriesDailyGame)
            .join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
            .filter(LeagueSeries.league_id == league.id)
            .all()
        )
        if not games:
            return True, ""
        final_count = sum(1 for g in games if g.final)
        if final_count == 0 or final_count == len(games):
            return True, ""
        return False, "Conferences and divisions cannot be modified while season series are in progress."

    # Weekly leagues (CFB, NFL, etc.)
    weeks = scoring.season_weeks(db, league.id)
    if not weeks:
        return True, ""
    scored = scoring.scored_week_ids(db, [w.id for w in weeks])
    if len(scored) == 0:
        return True, ""
    if len(scored) >= len(weeks):
        return True, ""
    return False, "Conferences and divisions cannot be modified while regular season games are in progress."


@router.get("/{league_id}/conferences", response_model=list[ConferenceOut])
def get_conferences(league_id: int, db: Session = Depends(get_db)):
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    rows = db.query(Conference).filter(Conference.league_id == league_id).order_by(Conference.id).all()
    if not rows:
        raise HTTPException(404, "League not found or has no conferences")

    div_rows = db.query(Division).filter(Division.league_id == league_id).order_by(Division.id).all()

    team_counts = defaultdict(int)
    div_team_counts = defaultdict(int)
    for team in db.query(Team).filter(Team.league_id == league_id):
        if team.conference_id is not None:
            team_counts[team.conference_id] += 1
        if team.division_id is not None:
            div_team_counts[team.division_id] += 1

    divs_by_conf = defaultdict(list)
    for d in div_rows:
        divs_by_conf[d.conference_id].append(
            DivisionOut(
                id=d.id,
                conference_id=d.conference_id,
                name=d.name,
                team_count=div_team_counts[d.id],
            )
        )

    editable, _ = are_conferences_editable(db, league)
    return [
        ConferenceOut(
            id=c.id,
            program_size=c.program_size,
            name=c.name,
            team_count=team_counts[c.id],
            editable=editable,
            divisions=divs_by_conf[c.id],
        )
        for c in rows
    ]


@router.post("/{league_id}/conferences", response_model=ConferenceOut)
def create_conference(
    league_id: int, payload: ConferenceCreateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: adds a new conference to the league."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Conference name cannot be empty")

    existing = db.query(Conference).filter(
        Conference.league_id == league_id, func.lower(Conference.name) == func.lower(name)
    ).first()
    if existing is not None:
        raise HTTPException(400, f"A conference named '{name}' already exists in this league")

    conf = Conference(league_id=league_id, name=name, program_size="large")
    db.add(conf)
    db.commit()
    db.refresh(conf)
    return ConferenceOut(id=conf.id, program_size=conf.program_size, name=conf.name, team_count=0, editable=True, divisions=[])


@router.patch("/{league_id}/conferences/{conference_id}", response_model=ConferenceOut)
def update_conference(
    league_id: int, conference_id: int, payload: ConferenceUpdateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: renames a conference."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)
    conf = db.get(Conference, conference_id)
    if conf is None or conf.league_id != league_id:
        raise HTTPException(404, "Conference not found in this league")
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Conference name cannot be empty")

    dup = db.query(Conference).filter(
        Conference.league_id == league_id, Conference.id != conference_id, func.lower(Conference.name) == func.lower(name)
    ).first()
    if dup is not None:
        raise HTTPException(400, f"A conference named '{name}' already exists in this league")

    conf.name = name
    db.commit()
    db.refresh(conf)
    team_count = db.query(Team).filter(Team.league_id == league_id, Team.conference_id == conf.id).count()

    div_rows = db.query(Division).filter(Division.conference_id == conf.id).order_by(Division.id).all()
    div_team_counts = defaultdict(int)
    for team in db.query(Team).filter(Team.league_id == league_id, Team.division_id.isnot(None)):
        div_team_counts[team.division_id] += 1

    divisions = [
        DivisionOut(id=d.id, conference_id=d.conference_id, name=d.name, team_count=div_team_counts[d.id])
        for d in div_rows
    ]
    return ConferenceOut(id=conf.id, program_size=conf.program_size, name=conf.name, team_count=team_count, editable=True, divisions=divisions)


@router.delete("/{league_id}/conferences/{conference_id}")
def delete_conference(
    league_id: int, conference_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: deletes a conference and its divisions."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)
    conf = db.get(Conference, conference_id)
    if conf is None or conf.league_id != league_id:
        raise HTTPException(404, "Conference not found in this league")

    all_confs = db.query(Conference).filter(Conference.league_id == league_id).all()
    if len(all_confs) <= 1:
        raise HTTPException(400, "A league must have at least one conference")

    # Reassign teams in this conference to the first remaining conference
    other_conf = [c for c in all_confs if c.id != conference_id][0]
    for team in db.query(Team).filter(Team.league_id == league_id, Team.conference_id == conference_id):
        team.conference_id = other_conf.id
        team.division_id = None

    db.query(Division).filter(Division.conference_id == conference_id).delete(synchronize_session=False)
    db.delete(conf)
    db.commit()
    return {"status": "deleted", "conference_id": conference_id}


@router.post("/{league_id}/conferences/{conference_id}/divisions", response_model=DivisionOut)
def create_division(
    league_id: int,
    conference_id: int,
    payload: DivisionCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Commissioner action: adds a new division under the specified conference."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)

    conf = db.get(Conference, conference_id)
    if conf is None or conf.league_id != league_id:
        raise HTTPException(404, "Conference not found in this league")

    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Division name cannot be empty")

    dup = db.query(Division).filter(
        Division.league_id == league_id,
        Division.conference_id == conference_id,
        func.lower(Division.name) == func.lower(name),
    ).first()
    if dup is not None:
        raise HTTPException(400, f"A division named '{name}' already exists in this conference")

    div = Division(league_id=league_id, conference_id=conference_id, name=name)
    db.add(div)
    db.commit()
    db.refresh(div)
    return DivisionOut(id=div.id, conference_id=div.conference_id, name=div.name, team_count=0)


@router.patch("/{league_id}/divisions/{division_id}", response_model=DivisionOut)
def update_division(
    league_id: int,
    division_id: int,
    payload: DivisionUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Commissioner action: renames a division."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)

    div = db.get(Division, division_id)
    if div is None or div.league_id != league_id:
        raise HTTPException(404, "Division not found in this league")

    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Division name cannot be empty")

    dup = db.query(Division).filter(
        Division.league_id == league_id,
        Division.conference_id == div.conference_id,
        Division.id != division_id,
        func.lower(Division.name) == func.lower(name),
    ).first()
    if dup is not None:
        raise HTTPException(400, f"A division named '{name}' already exists in this conference")

    div.name = name
    db.commit()
    db.refresh(div)
    team_count = db.query(Team).filter(Team.league_id == league_id, Team.division_id == div.id).count()
    return DivisionOut(id=div.id, conference_id=div.conference_id, name=div.name, team_count=team_count)


@router.delete("/{league_id}/divisions/{division_id}")
def delete_division(
    league_id: int,
    division_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Commissioner action: deletes a division."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)

    div = db.get(Division, division_id)
    if div is None or div.league_id != league_id:
        raise HTTPException(404, "Division not found in this league")

    for team in db.query(Team).filter(Team.league_id == league_id, Team.division_id == division_id):
        team.division_id = None

    db.delete(div)
    db.commit()
    return {"status": "deleted", "division_id": division_id}


def _randomize_league_teams(db: Session, league_id: int, confs: list[Conference], divs: list[Division]) -> None:
    teams = db.query(Team).filter(Team.league_id == league_id).all()
    if not teams or not confs:
        return

    shuffled_teams = list(teams)
    random.shuffle(shuffled_teams)

    num_confs = len(confs)
    conf_buckets: list[list[Team]] = [[] for _ in range(num_confs)]
    for i, t in enumerate(shuffled_teams):
        conf_buckets[i % num_confs].append(t)

    divs_by_conf = defaultdict(list)
    for d in divs:
        divs_by_conf[d.conference_id].append(d)

    for c_idx, conf in enumerate(confs):
        c_teams = conf_buckets[c_idx]
        c_divs = divs_by_conf[conf.id]
        if not c_divs:
            for t in c_teams:
                t.conference_id = conf.id
                t.division_id = None
        else:
            num_divs = len(c_divs)
            for j, t in enumerate(c_teams):
                target_div = c_divs[j % num_divs]
                t.conference_id = conf.id
                t.division_id = target_div.id


@router.post("/{league_id}/conferences/setup-structure", response_model=list[ConferenceOut])
def setup_conference_structure(
    league_id: int,
    payload: SetupStructureRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Commissioner action: configure how many conferences and how many divisions within each conference."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)

    num_confs = payload.conference_count
    divs_per_conf = payload.divisions_per_conference

    # Clear existing teams' conference/division links
    for team in db.query(Team).filter(Team.league_id == league_id):
        team.conference_id = None
        team.division_id = None
    db.flush()

    # Delete existing divisions and conferences
    db.query(Division).filter(Division.league_id == league_id).delete(synchronize_session=False)
    db.query(Conference).filter(Conference.league_id == league_id).delete(synchronize_session=False)
    db.flush()

    default_conf_names_2 = ["Eastern Conference", "Western Conference"]
    created_confs = []
    created_divs = []

    for c_idx in range(num_confs):
        conf_input = payload.conferences[c_idx] if (payload.conferences and c_idx < len(payload.conferences)) else None
        if conf_input and conf_input.name and conf_input.name.strip():
            conf_name = conf_input.name.strip()
        elif num_confs == 2:
            conf_name = default_conf_names_2[c_idx]
        elif num_confs == 1:
            conf_name = "Conference 1"
        else:
            conf_name = f"Conference {c_idx + 1}"

        conf = Conference(league_id=league_id, name=conf_name, program_size="large")
        db.add(conf)
        db.flush()
        created_confs.append(conf)

        for d_idx in range(divs_per_conf):
            div_input = (conf_input.divisions[d_idx] if (conf_input and conf_input.divisions and d_idx < len(conf_input.divisions)) else None)
            if div_input and div_input.name and div_input.name.strip():
                div_name = div_input.name.strip()
            elif num_confs == 2 and divs_per_conf == 2:
                if c_idx == 0:
                    div_name = "Atlantic Division" if d_idx == 0 else "Metropolitan Division"
                else:
                    div_name = "Central Division" if d_idx == 0 else "Pacific Division"
            else:
                div_letter = chr(65 + d_idx) if d_idx < 26 else f"{d_idx + 1}"
                div_name = f"Division {c_idx + 1}{div_letter}" if num_confs > 1 else f"Division {div_letter}"

            div = Division(league_id=league_id, conference_id=conf.id, name=div_name)
            db.add(div)
            db.flush()
            created_divs.append(div)

    if payload.randomize_teams:
        _randomize_league_teams(db, league_id, created_confs, created_divs)

    db.commit()
    return get_conferences(league_id, db)


@router.post("/{league_id}/conferences/randomize")
def randomize_league_alignment(
    league_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Commissioner action: randomly assigns teams to each conference and division as evenly as possible."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)

    confs = db.query(Conference).filter(Conference.league_id == league_id).order_by(Conference.id).all()
    if not confs:
        raise HTTPException(400, "League has no conferences to assign teams to")
    divs = db.query(Division).filter(Division.league_id == league_id).order_by(Division.id).all()

    _randomize_league_teams(db, league_id, confs, divs)
    db.commit()
    return {"status": "success", "message": "Teams randomized evenly", "teams": get_league_teams(league_id, db)}


@router.post("/{league_id}/conferences/align-teams", response_model=list[TeamOut])
def align_league_teams(
    league_id: int, payload: LeagueAlignmentRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Commissioner action: realigns teams across conferences and divisions."""
    league = assert_commissioner(db, user, league_id)
    editable, reason = are_conferences_editable(db, league)
    if not editable:
        raise HTTPException(400, reason)

    valid_conf_ids = {c.id for c in db.query(Conference).filter(Conference.league_id == league_id)}
    valid_divs = {d.id: d for d in db.query(Division).filter(Division.league_id == league_id)}
    teams_by_id = {t.id: t for t in db.query(Team).filter(Team.league_id == league_id)}

    for item in payload.assignments:
        if item.team_id not in teams_by_id:
            raise HTTPException(400, f"Team id {item.team_id} does not belong to this league")

        team = teams_by_id[item.team_id]
        if item.division_id is not None:
            if item.division_id not in valid_divs:
                raise HTTPException(400, f"Division id {item.division_id} does not belong to this league")
            div = valid_divs[item.division_id]
            team.division_id = div.id
            team.conference_id = div.conference_id
        else:
            if item.conference_id is not None and item.conference_id not in valid_conf_ids:
                raise HTTPException(400, f"Conference id {item.conference_id} does not belong to this league")
            team.conference_id = item.conference_id
            team.division_id = None

    db.commit()
    return get_league_teams(league_id, db)


@router.post("/{league_id}/conferences/expand", response_model=list[ConferenceOut])
def expand_conferences(league_id: int, db: Session = Depends(get_db)):
    """Adds one new conference per program-size tier (commissioner action) -
    see platform/conferences/service.py::expand_conferences for why it's
    always a full tier-set at a time, never a single conference."""
    league = db.get(League, league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    try:
        new_conferences = conferences.expand_conferences(db, league_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return [
        ConferenceOut(id=c.id, program_size=c.program_size, name=c.name, team_count=0, editable=True)
        for c in new_conferences
    ]


@router.get("/{league_id}/standings", response_model=list[StandingsRow])
def get_standings(league_id: int, db: Session = Depends(get_db)):
    """Derived live from lineups and player scores (platform/scoring.py) - nothing stored, so a
    retroactive lineup change is reflected immediately."""
    if not db.query(Team).filter(Team.league_id == league_id).count():
        raise HTTPException(404, "League not found or has no teams")
    return [StandingsRow(**r) for r in scoring.standings(db, league_id)]
