"""
Pydantic request/response models. Response models set from_attributes=True
so they read directly off the SQLAlchemy ORM objects returned by db/models.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Leagues ----------------------------------------------------------------

class LeagueCreateRequest(BaseModel):
    name: str
    commissioner_user_id: int
    universe_codes: list[str]  # e.g. ["ncaaf"], or ["ncaaf", "nfl"] for Dual Universe
    size: int = 12
    # None (the default) = the roster size the universe defines for the league's defense mode
    draft_rounds: int | None = None
    # universe_code -> "group"/"individual", e.g. {"nfl": "individual"}.
    # Any universe_code not present here defaults to "group". Rejected at
    # creation if a universe doesn't support the requested mode (see
    # Universe.supports_individual_defense).
    defense_modes: dict[str, str] = {}
    # "team_choice" (default) or "dynamic". "dynamic" is rejected at
    # creation for now - see League.program_size_mode.
    program_size_mode: str = "team_choice"
    # "recruit" (a series of recruiting periods where any team can bid on any
    # player - shown to teams as the "recruiting draft") or "snake" (default - a traditional snake draft run at a
    # scheduled time). Drives which screen the create-league wizard sends a
    # newly created team to next.
    draft_type: str = "snake"
    # When the draft starts: the snake draft's scheduled time, or when the
    # first recruiting period opens.
    draft_scheduled_at: datetime.datetime | None = None
    # Only used when draft_type="recruit": how long each recruiting period
    # lasts, in seconds (the draft runs one period per roster spot).
    recruiting_period_seconds: int = 86400
    # The pause between periods (results screen, no bidding). 20 seconds by
    # design; only settable so tests don't have to wait it out.
    recruiting_buffer_seconds: int = 20
    # NHL only: consecutive daily games against the same opponent.
    # None lets the API apply the NHL default without changing other sports.
    series_length_days: int | None = None


class LeagueOut(ORMModel):
    id: int
    name: str
    commissioner_user_id: int
    size: int
    draft_rounds: int
    salary_cap_enabled: bool
    program_size_mode: str
    draft_type: str
    draft_scheduled_at: datetime.datetime | None
    series_length_days: int | None


class ConferenceOut(BaseModel):
    id: int
    program_size: str
    name: str
    team_count: int


class StandingsRow(BaseModel):
    team_id: int
    team_name: str
    wins: int
    losses: int
    ties: int = 0
    points_for: float
    points_against: float


# --- Teams / rosters ----------------------------------------------------------

class TeamCreateRequest(BaseModel):
    league_id: int
    name: str
    owner_user_id: int | None = None
    is_ai_owned: bool = False
    program_size: str = "small"  # "large"/"moderate"/"small" - see platform/team_tiers.py


class TeamOut(ORMModel):
    id: int
    league_id: int
    name: str
    owner_user_id: int | None
    is_ai_owned: bool
    program_size: str
    conference_id: int | None
    season_goal: str


class RosterEntryOut(BaseModel):
    draftable_asset_id: int
    asset_type: str
    display_name: str  # player full_name, or "<Real Team> <Group>" for defense groups
    position: str | None = None      # QB/RB/WR/TE/PK, or DL/LB/DB for a defense group
    real_team: str | None = None     # the school
    last_season_points: float | None = None  # last season, ESPN standard scoring (None for defense groups / no stat line)
    acquired_via: str | None = None


class RosterSlotOut(BaseModel):
    slot_code: str
    eligible_positions: list[str]
    slot_count: int
    is_starter: bool


class WeekOut(BaseModel):
    id: int
    season_year: int
    week_number: int
    start_date: datetime.date | None = None
    locked: bool = False       # the week has started, so only a commissioner-mode commissioner can change lineups
    scored: bool = False       # the week's real games are in


class StarterScoreOut(BaseModel):
    slot_code: str
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    played: bool
    points: float


class TeamWeekBreakdownOut(BaseModel):
    team_id: int
    team_name: str
    week_id: int
    total_points: float        # fantasy points (the fitted scale applied to raw_credit)
    raw_credit: float          # the starters' summed EPA credit
    starters: list[StarterScoreOut]


class MatchupResultOut(BaseModel):
    matchup_id: int | None      # None for a conference-championship game (derived from the standings)
    championship: bool = False
    week_id: int
    team_a_id: int
    team_a_name: str
    team_b_id: int
    team_b_name: str
    scored: bool
    team_a_score: float | None
    team_b_score: float | None
    winner_team_id: int | None


class ScheduleRequest(BaseModel):
    weeks: int = 9         # the premade regular season is weeks 1-9
    replace: bool = False


class SandboxLeagueRequest(BaseModel):
    name: str = "Commissioner Mode League"
    size: int = 12
    draft_rounds: int = 20
    recruiting_period_seconds: int = 86400


class LineupSetRequest(BaseModel):
    week_id: int
    # slot_code -> draftable_asset_id, or a list of ids for a slot that holds several
    # players, e.g. {"QB": 501, "RB": [502, 503], "BENCH": [504, 505]}
    assignments: dict[str, int | list[int]]


class LineupEntryOut(BaseModel):
    week_id: int
    roster_slot_id: int
    slot_code: str
    draftable_asset_id: int
    display_name: str


# --- Draft --------------------------------------------------------------------

class DraftStartRequest(BaseModel):
    league_id: int


class DraftOut(ORMModel):
    id: int
    league_id: int
    status: str
    started_at: datetime.datetime | None
    completed_at: datetime.datetime | None


class DraftPickOut(BaseModel):
    id: int
    round: int
    pick_number: int
    team_id: int
    draftable_asset_id: int | None
    display_name: str | None
    picked_at: datetime.datetime | None
    is_autopick: bool


class MakePickRequest(BaseModel):
    draftable_asset_id: int


class DraftBoardAsset(BaseModel):
    draftable_asset_id: int
    asset_type: str
    display_name: str
    position: str | None = None  # player position, or "DL"/"LB"/"DB" for defense groups


# --- Auction draft --------------------------------------------------------------

class AuctionStartRequest(BaseModel):
    league_id: int
    round_seconds: int = 30


class AuctionDraftOut(ORMModel):
    id: int
    league_id: int
    status: str
    round_seconds: int
    nomination_order: list[int]
    next_nominator_index: int
    started_at: datetime.datetime | None
    completed_at: datetime.datetime | None


class AuctionBudgetOut(BaseModel):
    team_id: int
    starting_budget: int
    remaining_budget: int
    assets_won: int


class NominateRequest(BaseModel):
    draftable_asset_id: int
    nominated_by_team_id: int


class AuctionRoundOut(BaseModel):
    id: int
    round_number: int
    draftable_asset_id: int
    display_name: str
    nominated_by_team_id: int
    opens_at: datetime.datetime
    closes_at: datetime.datetime
    seconds_remaining: float
    status: str
    winning_team_id: int | None
    winning_bid_amount: int | None
    was_tie_broken_randomly: bool


class PlaceBidRequest(BaseModel):
    team_id: int
    amount: int


class AuctionBidOut(BaseModel):
    team_id: int
    amount: int
    submitted_at: datetime.datetime


# --- Scoring --------------------------------------------------------------------

class PlayCreditDetail(BaseModel):
    play_id: int
    role: str
    credit_points: float
    play_text: str | None


class WeeklyScoreBreakdown(BaseModel):
    draftable_asset_id: int
    display_name: str
    week_id: int
    total_points: float
    play_credits: list[PlayCreditDetail]


class TeamWeeklyScoreOut(BaseModel):
    team_id: int
    week_id: int
    total_points: float


class MatchupOut(ORMModel):
    id: int
    league_id: int
    week_id: int
    team_a_id: int
    team_b_id: int
    team_a_score: float | None
    team_b_score: float | None
    winner_team_id: int | None


class SeriesScheduleCreateRequest(BaseModel):
    start_date: datetime.date
    cycles: int = 1
    replace: bool = False


class SeriesDailyGameOut(ORMModel):
    id: int
    series_id: int
    calendar_date: datetime.date
    is_matchup_day: bool
    home_team_id: int
    away_team_id: int
    status: str


class LeagueSeriesOut(ORMModel):
    id: int
    league_id: int
    team_a_id: int
    team_b_id: int
    start_date: datetime.date
    end_date: datetime.date
    total_days: int
    status: str
    daily_games: list[SeriesDailyGameOut]


class SeriesScheduleOut(BaseModel):
    league_id: int
    series_length_days: int
    series: list[LeagueSeriesOut]


class TeamSeriesGameOut(BaseModel):
    daily_game_id: int
    calendar_date: datetime.date
    is_matchup_day: bool
    home_away: str
    status: str


class TeamSeriesOut(BaseModel):
    series_id: int
    opponent_id: int
    opponent_name: str
    start_date: datetime.date
    end_date: datetime.date
    total_days: int
    status: str
    games: list[TeamSeriesGameOut]


class TeamSeriesScheduleOut(BaseModel):
    team_id: int
    league_id: int
    series: list[TeamSeriesOut]


class PeckingOrderSetRequest(BaseModel):
    """group ("C"/"W"/"D"/"G") -> Player ids, highest priority first. Groups left out are untouched."""
    assignments: dict[str, list[int]]


class PeckingOrderPlayerOut(BaseModel):
    player_id: int
    draftable_asset_id: int
    display_name: str
    position: str
    priority: int | None  # None = on the roster but unranked


class PeckingOrderGroupOut(BaseModel):
    position_group: str
    max_depth: int
    starters: int
    ranked: list[PeckingOrderPlayerOut]
    unranked: list[PeckingOrderPlayerOut]


class PeckingOrderOut(BaseModel):
    team_id: int
    groups: list[PeckingOrderGroupOut]


class PeckingOrderResolveRequest(BaseModel):
    scheduled_player_ids: list[int]
    confirmed_goalie_ids: list[int] = []


class PeckingOrderVacancyOut(BaseModel):
    position_group: str
    slot: int
    reason: str


class PeckingOrderResolvedOut(BaseModel):
    team_id: int
    starters: dict[str, list[PeckingOrderPlayerOut]]
    vacancies: list[PeckingOrderVacancyOut]


# --- NHL series standings, rivalries, Game Score ----------------------------

class SeriesStandingOut(BaseModel):
    rank: int
    team_id: int
    team_name: str
    wins: int
    losses: int
    ties: int
    goals_for: int
    goals_against: int
    goal_diff: int
    h2h_wins: int


class SeriesStandingsOut(BaseModel):
    league_id: int
    as_of: datetime.date | None
    tiebreak_order: str
    standings: list[SeriesStandingOut]


class RivalryOut(BaseModel):
    league_id: int
    team_low_id: int
    team_high_id: int
    series_low_wins: int
    series_high_wins: int
    series_ties: int
    daily_low_wins: int
    daily_high_wins: int
    daily_ties: int
    goals_low: int
    goals_high: int
    net_goal_diff_low: int
    intensity: float


class RivalriesOut(BaseModel):
    league_id: int
    as_of: datetime.date | None
    rivalries: list[RivalryOut]


class GameScoreOut(BaseModel):
    daily_game_id: int
    series_id: int
    calendar_date: datetime.date
    home_team_id: int
    away_team_id: int
    status: str
    score: int
    margin: float
    cutoff: float
    rivalry: float
    urgency: float
    standings_as_of: datetime.date


class GameScoresOut(BaseModel):
    league_id: int
    weights: dict[str, float]
    games: list[GameScoreOut]


# --- Progressive simulator (FS-025) -----------------------------------------

class SimStateOut(BaseModel):
    """The viewer's scoreboard. Everything but the lifecycle fields is null while the viewer is behind the veil."""
    contract_version: str
    daily_game_id: int
    published: bool
    status: str  # SCHEDULED / LIVE / FINAL
    veiled: bool
    reveal_mode: str | None
    periods: int | None
    period_seconds: int | None
    window_seconds: int | None
    period: int | None
    seconds_remaining: int | None
    home_score: int | None
    away_score: int | None
    home_shots: int | None
    away_shots: int | None
    shown_seq: int
    cursor_seq: int
    has_more: bool
    is_complete: bool
    can_skip: bool
    provenance: dict | None


class SimEventOut(ORMModel):
    seq: int
    kind: str
    period: int
    game_second: int
    team_side: str | None
    home_score: int
    away_score: int
    home_shots: int
    away_shots: int
    headline: str
    detail: dict


class SimEventsOut(BaseModel):
    contract_version: str
    daily_game_id: int
    status: str
    after_seq: int
    next_seq: int  # pass as after_seq to continue; equals after_seq when nothing new
    available_through: int
    has_more: bool
    events: list[SimEventOut]


class SimProgressRequest(BaseModel):
    cursor_seq: int


class SimFinalOut(BaseModel):
    contract_version: str
    daily_game_id: int
    status: str
    home_score: int
    away_score: int
    home_shots: int
    away_shots: int
    periods: int
    event_count: int
    scoring_events: list[SimEventOut]
    provenance: dict
