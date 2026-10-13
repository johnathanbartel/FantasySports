import datetime
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, current_user, require_commissioner_mode
from fantasy_sports.api.schemas import (
    ConferenceOut, LeagueCreateRequest, LeagueOut, LeagueRenameRequest,
    LeagueTeamsSetupRequest, LeagueTeamsSetupResponse, RosterSlotOut,
    SandboxLeagueRequest, StandingsRow, TeamAssignOut, TeamAssignRequest,
    TeamInviteOut, TeamInviteRequest, TeamOut, TeamSetupResult, WeekOut,
)
from fantasy_sports.platform import calendar, lineups, scoring
from fantasy_sports.platform.conferences import service as conferences
from fantasy_sports.platform.db.models.core import (
    AuctionDraft, Conference, Draft, League, LeagueUniverse, LineupEntry,
    RecruitingDraft, RecruitingSettings, RosterEntry, RosterSlotDefinition,
    Team, Universe, User, Week, WeeklyMatchup,
)
from fantasy_sports.platform.defense_modes import DEFENSE_MODES, roster_size
from fantasy_sports.platform.league_features import draft_state
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

    conferences.create_baseline_conferences(db, league.id)

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
    teams = db.query(Team).filter(Team.league_id == league_id).order_by(Team.id).all()
    if not teams:
        raise HTTPException(404, "League not found or has no teams")
    return teams


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


@router.get("/{league_id}/conferences", response_model=list[ConferenceOut])
def get_conferences(league_id: int, db: Session = Depends(get_db)):
    rows = db.query(Conference).filter(Conference.league_id == league_id).order_by(Conference.id).all()
    if not rows:
        raise HTTPException(404, "League not found or has no conferences")
    team_counts = defaultdict(int)
    for team in db.query(Team).filter(Team.league_id == league_id, Team.conference_id.isnot(None)):
        team_counts[team.conference_id] += 1
    return [
        ConferenceOut(id=c.id, program_size=c.program_size, name=c.name, team_count=team_counts[c.id])
        for c in rows
    ]


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
        ConferenceOut(id=c.id, program_size=c.program_size, name=c.name, team_count=0)
        for c in new_conferences
    ]


@router.get("/{league_id}/standings", response_model=list[StandingsRow])
def get_standings(league_id: int, db: Session = Depends(get_db)):
    """Derived live from lineups and player scores (platform/scoring.py) - nothing stored, so a
    retroactive lineup change is reflected immediately."""
    if not db.query(Team).filter(Team.league_id == league_id).count():
        raise HTTPException(404, "League not found or has no teams")
    return [StandingsRow(**r) for r in scoring.standings(db, league_id)]
