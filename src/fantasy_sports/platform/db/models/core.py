"""
Sport-agnostic core schema: users, leagues, teams, rosters, draft, and the
real-world calendar/matchup mechanics that every sport on the platform
shares. Football-specific tables (raw play-by-play, credit attribution
output) live in db/models/football.py - see that module's docstring for
why the split.

"universe" = a specific league context (college football, NFL, ...), not
just "sport" - football and baseball are sports, but college football and
NFL are two different universes of the same sport. A league can belong to
more than one universe (Dual Universe mode) via league_universes; the
crossover mechanics themselves aren't built yet, this just avoids the
schema blocking it later.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.team_tiers import SEASON_GOAL_BY_PROGRAM_SIZE


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    # Auth mechanism TBD - this is a hook, not an implementation.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class UserSession(Base):
    """Dev-grade login session: an opaque bearer token that identifies a
    user. There is no password check yet (users.password_hash is only a hook),
    so anyone who can POST /sessions can become any user - this closes the
    "team_id in the request body" trust gap (acting as a team you don't own),
    not real authentication. Swap the login step for real credentials later
    without touching the ownership checks in api/auth.py."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class Universe(Base):
    __tablename__ = "universes"

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(30))  # "football", "baseball", ...
    level: Mapped[str] = mapped_column(String(30))  # "college", "pro"
    code: Mapped[str] = mapped_column(String(20), unique=True)  # "ncaaf", "nfl"
    display_name: Mapped[str] = mapped_column(String(60))
    # Whether this universe's data source identifies individual defenders on
    # a play (nflfastR: sack_player_name, interception_player_name, tackle
    # columns, ...). CFBD has none of that for NCAAF, so "individual" is a
    # league_universes.defense_mode option only where this is True.
    supports_individual_defense: Mapped[bool] = mapped_column(Boolean, default=False)


class League(Base):
    __tablename__ = "leagues"

    __table_args__ = (
        CheckConstraint(
            "program_size_mode IN ('team_choice', 'dynamic')", name="ck_league_program_size_mode"
        ),
        CheckConstraint("draft_type IN ('recruit', 'snake')", name="ck_league_draft_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    commissioner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    size: Mapped[int] = mapped_column(Integer, default=12)
    draft_rounds: Mapped[int] = mapped_column(Integer, default=20)
    # Placeholder only - no salary-cap logic is implemented anywhere yet.
    salary_cap_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # "team_choice": teams pick their own program_size (see Team below).
    # "dynamic": every team starts "small" and is promoted/relegated by
    # season performance - stubbed only. The column/CHECK accepts it so a
    # future migration isn't needed to add the value, but the API layer
    # (create_league) currently rejects actually setting it, since dynamic
    # mode needs a multi-season/season-rollover concept this schema doesn't
    # have yet.
    program_size_mode: Mapped[str] = mapped_column(String(20), default="team_choice")
    # "recruit": a timed sealed-bid auction (AuctionDraft) - user-facing name
    # is "recruiting period", started on demand once the commissioner is
    # ready. "snake": a traditional position-by-position snake draft
    # (Draft/DraftPick), run at a scheduled time instead. Set once at league
    # creation (the create-league wizard) and used there to decide which
    # screen a newly created team lands on next.
    draft_type: Mapped[str] = mapped_column(String(20), default="snake")
    # Only meaningful for draft_type="snake" - lets the roster page tell a
    # team when to expect the live draft. Nullable because a commissioner
    # may not have picked a time yet, and because "recruit" leagues start
    # their recruiting period on demand instead of at a fixed time.
    draft_scheduled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    universes: Mapped[list["LeagueUniverse"]] = relationship(back_populates="league")
    teams: Mapped[list["Team"]] = relationship(back_populates="league")


class LeagueUniverse(Base):
    """A normal league has one row here; a Dual Universe league has two."""

    __tablename__ = "league_universes"
    __table_args__ = (
        UniqueConstraint("league_id", "universe_id"),
        CheckConstraint("defense_mode IN ('group', 'individual')", name="ck_league_universe_defense_mode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    # A league's choice, per universe, of whether defense is drafted/scored
    # as DL/LB/DB position groups or as individual defenders. "individual"
    # is only a legal choice when universe.supports_individual_defense is
    # True - enforced at the API layer (create_league), not by a DB
    # constraint, since that check spans two tables.
    defense_mode: Mapped[str] = mapped_column(String(20), default="group")

    league: Mapped["League"] = relationship(back_populates="universes")
    universe: Mapped["Universe"] = relationship()


class Conference(Base):
    """Groups a league's teams by program_size tier. Every league starts
    with one conference per tier (a baseline of len(PROGRAM_SIZES), i.e. 3
    today); growing conference count is only ever done one full tier-set at
    a time (platform/conferences/service.py::expand_conferences), so a
    league's total conference count is always a multiple of the number of
    tiers in use.

    Per-league tier subsetting (a league that only uses 2 of the 3 tiers,
    so conferences would only ever grow 2 at a time) isn't implemented yet
    - every league currently gets all 3 tiers."""

    __tablename__ = "conferences"
    __table_args__ = (
        CheckConstraint("program_size IN ('large', 'moderate', 'small')", name="ck_conference_program_size"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    program_size: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80))

    league: Mapped["League"] = relationship()


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        CheckConstraint(
            "(owner_user_id IS NOT NULL AND is_ai_owned = 0) OR "
            "(owner_user_id IS NULL AND is_ai_owned = 1)",
            name="ck_team_owner_xor_ai",
        ),
        CheckConstraint(
            "program_size IN ('large', 'moderate', 'small')", name="ck_team_program_size"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    name: Mapped[str] = mapped_column(String(80))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_ai_owned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Sets this team's starting auction-draft budget (see
    # auction.constants.STARTING_BUDGET_BY_PROGRAM_SIZE), its conference
    # (see conference_id below), and its season_goal. Default "small"
    # matches the eventual dynamic-mode rule (stubbed only - see
    # League.program_size_mode) that a program starts small and has to
    # build up over multiple seasons.
    program_size: Mapped[str] = mapped_column(String(20), default="small")
    # Assigned at team creation (api/routers/teams.py::create_team) by
    # platform/conferences/service.py::assign_team_to_conference, to the
    # least-populated conference matching program_size. Nullable because
    # teams inserted directly (as every team is today - there's no
    # create-team endpoint's callers yet outside scripts/tests) predate
    # this column and never get one.
    conference_id: Mapped[int | None] = mapped_column(ForeignKey("conferences.id"), nullable=True)
    # Trait system design TBD - just a JSON hook for now, e.g.
    # {"aggression": 0.8, "reactive_to_trends": true}.
    personality_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    league: Mapped["League"] = relationship(back_populates="teams")
    conference: Mapped["Conference | None"] = relationship()

    @property
    def season_goal(self) -> str:
        """Fixed per-tier goal label (see platform/team_tiers.py) - purely
        informational for now, not fed into scoring. The commissioner's
        stated intent is that reaching/missing it could move next season's
        budget or tier; that needs season-rollover, which doesn't exist
        yet."""
        return SEASON_GOAL_BY_PROGRAM_SIZE[self.program_size]


class RealTeam(Base):
    """A real-world team (Georgia, Kansas City Chiefs, ...), not a fantasy team."""

    __tablename__ = "real_teams"
    __table_args__ = (UniqueConstraint("universe_id", "external_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    name: Mapped[str] = mapped_column(String(80))
    conference: Mapped[str | None] = mapped_column(String(60), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "fbs"/"fcs"
    external_ref: Mapped[str | None] = mapped_column(String(60), nullable=True)


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (UniqueConstraint("universe_id", "external_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    real_team_id: Mapped[int | None] = mapped_column(ForeignKey("real_teams.id"), nullable=True)
    full_name: Mapped[str] = mapped_column(String(100))
    position: Mapped[str] = mapped_column(String(10))  # QB/RB/WR/TE/K/...
    # Source id (CFBD player id / nflfastR gsis_id) for future ingestion joins.
    external_ref: Mapped[str | None] = mapped_column(String(60), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DefenseGroup(Base):
    """The actual draftable defensive asset - e.g. "Georgia DL". Defense is
    drafted as a position group, not individual defenders."""

    __tablename__ = "defense_groups"
    __table_args__ = (
        UniqueConstraint("real_team_id", "position_group"),
        CheckConstraint("position_group IN ('DL','LB','DB')", name="ck_defense_group_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    real_team_id: Mapped[int] = mapped_column(ForeignKey("real_teams.id"))
    position_group: Mapped[str] = mapped_column(String(2))  # "DL"/"LB"/"DB"

    real_team: Mapped["RealTeam"] = relationship()


class DraftableAsset(Base):
    """Single join target for anything that can be drafted/rostered/started -
    a player or a defense group. Keeps draft_picks/roster_entries/
    lineup_entries from each needing two nullable FKs, and lets the draft
    pool freely mix players and defense groups (see the DL/LB/DB same-pool
    recommendation in docs/data_contract.md)."""

    __tablename__ = "draftable_assets"
    __table_args__ = (
        CheckConstraint(
            "(asset_type = 'player' AND player_id IS NOT NULL AND defense_group_id IS NULL) OR "
            "(asset_type = 'defense_group' AND defense_group_id IS NOT NULL AND player_id IS NULL)",
            name="ck_draftable_asset_type_xor",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    asset_type: Mapped[str] = mapped_column(String(20))  # "player"/"defense_group"
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    defense_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("defense_groups.id"), nullable=True
    )

    player: Mapped["Player | None"] = relationship()
    defense_group: Mapped["DefenseGroup | None"] = relationship()


class RosterSlotDefinition(Base):
    """Defines roster *structure* per universe/sport as data, not hardcoded
    columns - a different sport gets its own rows here without touching
    this table's shape."""

    __tablename__ = "roster_slot_definitions"
    __table_args__ = (
        CheckConstraint(
            "defense_mode IS NULL OR defense_mode IN ('group', 'individual')",
            name="ck_roster_slot_definition_defense_mode",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    slot_code: Mapped[str] = mapped_column(String(20))  # "QB","RB","FLEX","DL","BENCH",...
    eligible_positions: Mapped[list] = mapped_column(JSON)  # e.g. ["RB","WR","TE"] for FLEX
    slot_count: Mapped[int] = mapped_column(Integer)
    is_starter: Mapped[bool] = mapped_column(Boolean, default=True)
    # NULL applies to every league on this universe regardless of
    # defense_mode (all offensive slots, BENCH). Only set on slot rows that
    # are specific to one defense_mode - e.g. a universe offering both
    # modes has one set of DL/LB/DB slots tagged "group" and a different
    # set of individual-position slots tagged "individual"; a league only
    # sees the slots matching its own league_universes.defense_mode plus
    # every NULL row.
    defense_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/in_progress/complete
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class DraftPick(Base):
    """Pick slots (round, pick_number, team_id) are pre-generated in snake
    order when the draft is created; draftable_asset_id is filled in as
    picks are made."""

    __tablename__ = "draft_picks"
    __table_args__ = (UniqueConstraint("draft_id", "pick_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    round: Mapped[int] = mapped_column(Integer)
    pick_number: Mapped[int] = mapped_column(Integer)  # overall pick, 1-indexed
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("draftable_assets.id"), nullable=True
    )
    picked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    is_autopick: Mapped[bool] = mapped_column(Boolean, default=False)

    draftable_asset: Mapped["DraftableAsset | None"] = relationship()


class AuctionDraft(Base):
    """A timed, sealed-bid auction draft. Teams take turns nominating a
    draftable asset; everyone gets one hidden bid within a fixed time
    window (round_seconds); the highest bid wins and is debited from that
    team's budget - see platform/auction/engine.py for the round-resolution
    rules this table supports.

    Separate from Draft/DraftPick (snake-shaped: fixed team-per-slot picks
    pre-generated up front) because an auction has no fixed pick order and
    price is a first-class concept neither of those tables has room for.
    """

    __tablename__ = "auction_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/in_progress/complete
    round_seconds: Mapped[int] = mapped_column(Integer, default=30)
    # Nomination turn order, as a list of team_id - e.g. [3, 1, 2]. Rotates
    # via next_nominator_index regardless of who wins each round's bid.
    nomination_order: Mapped[list] = mapped_column(JSON)
    next_nominator_index: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class AuctionTeamBudget(Base):
    """One row per team per auction draft - the budget the engine debits
    against. starting_budget is snapshotted from Team.program_size at
    auction start, so a program_size change mid-auction can't rewrite a
    budget that's already being spent."""

    __tablename__ = "auction_team_budgets"
    __table_args__ = (UniqueConstraint("auction_draft_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_draft_id: Mapped[int] = mapped_column(ForeignKey("auction_drafts.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    starting_budget: Mapped[int] = mapped_column(Integer)
    remaining_budget: Mapped[int] = mapped_column(Integer)
    assets_won: Mapped[int] = mapped_column(Integer, default=0)


class AuctionRound(Base):
    """One nominated asset up for bid. opens_at/closes_at is the round's
    timed sealed-bid window (opens_at + round_seconds); the engine rejects
    bids placed after closes_at even if the round hasn't been resolved yet.

    status: "open" (bidding window running) -> "resolved" (had at least one
    bid, winner recorded) or "unsold" (window closed with no bids at all -
    the asset returns to the board for a future nomination).
    """

    __tablename__ = "auction_rounds"
    __table_args__ = (UniqueConstraint("auction_draft_id", "round_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_draft_id: Mapped[int] = mapped_column(ForeignKey("auction_drafts.id"))
    round_number: Mapped[int] = mapped_column(Integer)  # sequential across the whole auction
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    nominated_by_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    opens_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    closes_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="open")
    winning_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    winning_bid_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True when the winner was chosen by random tie-break among two or more
    # equal top bids, rather than being the only top bidder.
    was_tie_broken_randomly: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class AuctionBid(Base):
    """A team's single sealed bid for a round - hidden from other teams
    until the round resolves (bid -> reveal -> debit -> next round). A team
    may overwrite its own bid as many times as it likes while the round is
    still open; only the latest amount counts."""

    __tablename__ = "auction_bids"
    __table_args__ = (UniqueConstraint("auction_round_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_round_id: Mapped[int] = mapped_column(ForeignKey("auction_rounds.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    amount: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class RosterEntry(Base):
    """The season-long 20-man roster a team owns."""

    __tablename__ = "roster_entries"
    __table_args__ = (UniqueConstraint("team_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    acquired_via: Mapped[str] = mapped_column(String(20), default="draft")
    acquired_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class Week(Base):
    """Maps a fantasy week to each universe's real calendar - needed
    independently for college/NFL, and for Dual Universe later."""

    __tablename__ = "weeks"
    __table_args__ = (UniqueConstraint("universe_id", "season_year", "week_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    season_year: Mapped[int] = mapped_column(Integer)
    week_number: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    end_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    is_playoffs: Mapped[bool] = mapped_column(Boolean, default=False)


class LineupEntry(Base):
    """The week-by-week starting lineup - separate from roster_entries so
    'set lineup' is its own weekly action distinct from roster ownership."""

    __tablename__ = "lineup_entries"
    __table_args__ = (UniqueConstraint("team_id", "week_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    roster_slot_id: Mapped[int] = mapped_column(ForeignKey("roster_slot_definitions.id"))

    draftable_asset: Mapped["DraftableAsset"] = relationship()
    roster_slot: Mapped["RosterSlotDefinition"] = relationship()


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("real_teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("real_teams.id"))
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kickoff_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")


class WeeklyMatchup(Base):
    __tablename__ = "weekly_matchups"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    team_a_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_b_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    team_a_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    team_b_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    winner_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)


class WeeklyTeamScore(Base):
    """Sum of a fantasy team's active lineup's player scores for a week -
    feeds weekly_matchups. Standings are NOT a stored table; they're
    derived on read from weekly_matchups (see docs/data_contract.md)."""

    __tablename__ = "weekly_team_scores"
    __table_args__ = (UniqueConstraint("team_id", "week_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    total_points: Mapped[float] = mapped_column(Float, default=0.0)


class RecruitingSettings(Base):
    """Commissioner-chosen settings for a league's recruiting draft. Kept in
    its own table (not columns on leagues) so they can be added to databases
    that predate them without an ALTER."""

    __tablename__ = "recruiting_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), unique=True)
    # How long each recruiting period lasts. The draft has one period per
    # roster spot (League.draft_rounds), so a 20-man roster means 20 periods.
    period_seconds: Mapped[int] = mapped_column(Integer)
    # The pause between periods: results are shown and no bids can be placed.
    buffer_seconds: Mapped[int] = mapped_column(Integer, default=20)


class RecruitingDraft(Base):
    """A league's recruiting draft (draft_type="recruit"): a series of
    fixed-length periods - the recruiting equivalent of rounds. During any
    period every team can bid on any player at any time; bids are processed
    only when a period ends (see platform/recruiting/engine.py). Separate from
    AuctionDraft, the one-player-at-a-time timed auction."""

    __tablename__ = "recruiting_drafts"
    __table_args__ = (UniqueConstraint("league_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open / closed
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    period_seconds: Mapped[int] = mapped_column(Integer)
    buffer_seconds: Mapped[int] = mapped_column(Integer, default=20)
    total_periods: Mapped[int] = mapped_column(Integer)
    # Periods whose bids have already been processed (0..total_periods).
    periods_processed: Mapped[int] = mapped_column(Integer, default=0)


class RecruitingThreshold(Base):
    """A player's hidden price for one recruiting draft. Never exposed by the
    API before the player is awarded."""

    __tablename__ = "recruiting_thresholds"
    __table_args__ = (UniqueConstraint("draft_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    threshold: Mapped[int] = mapped_column(Integer)
    is_elite: Mapped[bool] = mapped_column(Boolean, default=False)


class RecruitingBid(Base):
    """A team's standing bid on one player. Persists across periods (and can
    be raised, lowered or retracted) until the player is awarded or the draft
    ends."""

    __tablename__ = "recruiting_bids"
    __table_args__ = (UniqueConstraint("draft_id", "team_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    amount: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime)


class RecruitingAward(Base):
    """A player awarded to a team when a period's bids were processed.
    reason: "threshold" (the sum of bids met the hidden price) or "deadline"
    (the final period ended; the highest bidder takes the player regardless
    of price). price is what the winner bid, and is charged against budget."""

    __tablename__ = "recruiting_awards"
    __table_args__ = (UniqueConstraint("draft_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    period_number: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(20))
    was_tie_broken_randomly: Mapped[bool] = mapped_column(Boolean, default=False)
    awarded_at: Mapped[datetime.datetime] = mapped_column(DateTime)

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class RecruitingCharge(Base):
    """Money a team spends when a player is awarded. The money bid on a player
    goes to the player: EVERY team with a standing bid on him is charged its
    bid when he is awarded, whether or not it won him. So a team that bid $5
    on a player who went to someone else loses that $5. is_winner marks the
    team that got the player. A team's spend is the sum of its charges; bids
    on players who are never awarded are never charged."""

    __tablename__ = "recruiting_charges"
    __table_args__ = (UniqueConstraint("draft_id", "team_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    period_number: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    is_winner: Mapped[bool] = mapped_column(Boolean)


class RecruitingBidResult(Base):
    """What happened to one team's bid on one player when a period was
    processed - a snapshot taken at that moment, so it stays accurate after the
    bid is changed. completion_pct is the total of all (eligible) bids on the
    player divided by his hidden price, as a percent; it is shown to a team
    only in its own end-of-period results screen, never elsewhere.
    outcome: "won" / "lost" (someone else won him - the bid is still charged)
    / "pending" (his price wasn't met; the bid carries into the next period)."""

    __tablename__ = "recruiting_bid_results"
    __table_args__ = (UniqueConstraint("draft_id", "period_number", "team_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    period_number: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    bid_amount: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(10))
    completion_pct: Mapped[float] = mapped_column(Float)
    charged: Mapped[int] = mapped_column(Integer)


class RecruitingResultAck(Base):
    """A team has closed the results screen for a period. Until it has, the
    screen keeps coming back (even after a page reload)."""

    __tablename__ = "recruiting_result_acks"
    __table_args__ = (UniqueConstraint("draft_id", "team_id", "period_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("recruiting_drafts.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    period_number: Mapped[int] = mapped_column(Integer)


class UserEntitlement(Base):
    """A paid/premium feature switched on for a user (see platform/entitlements.py).
    Today's only feature is "commissioner_mode"."""

    __tablename__ = "user_entitlements"
    __table_args__ = (UniqueConstraint("user_id", "feature"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    feature: Mapped[str] = mapped_column(String(40))
    granted_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class FreeAgencyTransaction(Base):
    """A free-agency signing or release (see platform/free_agency.py). The
    league-wide Transactions log is these rows plus the recruiting awards.
    kind: "add" / "drop". reason: "user" (the owner did it), "commissioner"
    (a commissioner-mode commissioner acted for an AI team) or "ai_fill" (the
    AI's required fill of open roster spots). price is what the signing cost
    (0 for a drop)."""

    __tablename__ = "free_agency_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    kind: Mapped[str] = mapped_column(String(10))
    price: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)

    draftable_asset: Mapped["DraftableAsset"] = relationship()


class ScheduleSettings(Base):
    """The preseason schedule stage of a league (platform/schedule.py): "open" while teams
    may still request different opponents, "closed" once the commissioner has closed it
    (pending requests to AI teams are resolved then, everything else pending is declined).
    A league without a row is "open"."""

    __tablename__ = "schedule_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="open")
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class MatchupRequest(Base):
    """One team asking another to play it in a given week, optionally sweetened with in-game
    cash. status: "pending" / "accepted" / "declined" / "withdrawn". The cash only moves if the
    request is accepted (an accepted request's offer_amount is paid from the sender to the
    receiver - see free_agency.team_money)."""

    __tablename__ = "matchup_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    from_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    to_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    offer_amount: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
