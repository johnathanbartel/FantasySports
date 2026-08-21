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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    # Auth mechanism TBD - this is a hook, not an implementation.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class Universe(Base):
    __tablename__ = "universes"

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[str] = mapped_column(String(30))  # "football", "baseball", ...
    level: Mapped[str] = mapped_column(String(30))  # "college", "pro"
    code: Mapped[str] = mapped_column(String(20), unique=True)  # "ncaaf", "nfl"
    display_name: Mapped[str] = mapped_column(String(60))


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    commissioner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    size: Mapped[int] = mapped_column(Integer, default=12)
    draft_rounds: Mapped[int] = mapped_column(Integer, default=20)
    # Placeholder only - no salary-cap logic is implemented anywhere yet.
    salary_cap_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    universes: Mapped[list["LeagueUniverse"]] = relationship(back_populates="league")
    teams: Mapped[list["Team"]] = relationship(back_populates="league")


class LeagueUniverse(Base):
    """A normal league has one row here; a Dual Universe league has two."""

    __tablename__ = "league_universes"
    __table_args__ = (UniqueConstraint("league_id", "universe_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))

    league: Mapped["League"] = relationship(back_populates="universes")
    universe: Mapped["Universe"] = relationship()


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        CheckConstraint(
            "(owner_user_id IS NOT NULL AND is_ai_owned = 0) OR "
            "(owner_user_id IS NULL AND is_ai_owned = 1)",
            name="ck_team_owner_xor_ai",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    name: Mapped[str] = mapped_column(String(80))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_ai_owned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Trait system design TBD - just a JSON hook for now, e.g.
    # {"aggression": 0.8, "reactive_to_trends": true}.
    personality_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    league: Mapped["League"] = relationship(back_populates="teams")


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

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"))
    slot_code: Mapped[str] = mapped_column(String(20))  # "QB","RB","FLEX","DL","BENCH",...
    eligible_positions: Mapped[list] = mapped_column(JSON)  # e.g. ["RB","WR","TE"] for FLEX
    slot_count: Mapped[int] = mapped_column(Integer)
    is_starter: Mapped[bool] = mapped_column(Boolean, default=True)


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
