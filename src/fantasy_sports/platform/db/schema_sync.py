"""
Utilities to ensure SQLite databases (such as copies of older seeds)
have all required tables and columns up to date with the latest models.
"""

from sqlalchemy import inspect, text
from fantasy_sports.platform.db.base import Base


def ensure_latest_schema(engine):
    """
    Creates any missing tables and ensures all columns exist on older SQLite databases.
    Safe and idempotent.
    """
    Base.metadata.create_all(bind=engine)

    if not str(engine.url).startswith("sqlite"):
        return

    with engine.begin() as conn:
        insp = inspect(conn)
        tables = set(insp.get_table_names())

        if "leagues" in tables:
            cols = {c["name"] for c in insp.get_columns("leagues")}
            if "series_length_days" not in cols:
                conn.execute(text("ALTER TABLE leagues ADD COLUMN series_length_days INTEGER"))
            if "league_mode" not in cols:
                conn.execute(text("ALTER TABLE leagues ADD COLUMN league_mode VARCHAR(20) DEFAULT 'standard' NOT NULL"))

            for name, ddl in (("league_type", "VARCHAR(4) DEFAULT 'FREE' NOT NULL"), ("annual_dues_cents", "INTEGER"),
                              ("trade_deadline", "DATETIME")):
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE leagues ADD COLUMN {name} {ddl}"))

        if "trades" in tables:
            cols = {c["name"] for c in insp.get_columns("trades")}
            if "failure_reason" not in cols:
                conn.execute(text("ALTER TABLE trades ADD COLUMN failure_reason VARCHAR(500)"))

        if "simulator_games" in tables:
            cols = {c["name"] for c in insp.get_columns("simulator_games")}
            if "published_periods" not in cols:
                conn.execute(text("ALTER TABLE simulator_games ADD COLUMN published_periods INTEGER"))

        if "players" in tables:
            cols = {c["name"] for c in insp.get_columns("players")}
            for name, ddl in (("injury_status", "VARCHAR(10)"), ("injury_note", "VARCHAR(200)"),
                              ("injury_updated_at", "DATETIME"), ("birth_date", "DATE")):
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE players ADD COLUMN {name} {ddl}"))

        if "roster_entries" in tables:
            cols = {c["name"] for c in insp.get_columns("roster_entries")}
            if "on_injured_list" not in cols:
                conn.execute(text("ALTER TABLE roster_entries ADD COLUMN on_injured_list BOOLEAN DEFAULT 0 NOT NULL"))

        if "series_daily_games" in tables:
            cols = {c["name"] for c in insp.get_columns("series_daily_games")}
            if "calendar_date_end" not in cols:
                conn.execute(text("ALTER TABLE series_daily_games ADD COLUMN calendar_date_end DATE"))
            if "window_index" not in cols:
                conn.execute(text("ALTER TABLE series_daily_games ADD COLUMN window_index INTEGER"))

        if "teams" in tables:
            cols = {c["name"] for c in insp.get_columns("teams")}
            if "eliminated_season" not in cols:
                conn.execute(text("ALTER TABLE teams ADD COLUMN eliminated_season INTEGER"))
            if "trade_plan" not in cols:
                conn.execute(text("ALTER TABLE teams ADD COLUMN trade_plan VARCHAR(12) DEFAULT 'win_now' NOT NULL"))
            if "division_id" not in cols:
                conn.execute(text("ALTER TABLE teams ADD COLUMN division_id INTEGER REFERENCES divisions(id)"))

        if "drafts" in tables:
            cols = {c["name"] for c in insp.get_columns("drafts")}
            draft_cols = [
                ("pick_seconds", "INTEGER"),
                ("overnight_start_minute", "INTEGER"),
                ("overnight_end_minute", "INTEGER"),
                ("utc_offset_minutes", "INTEGER DEFAULT 0 NOT NULL"),
                ("paused", "BOOLEAN DEFAULT 0 NOT NULL"),
                ("paused_at", "DATETIME"),
                ("remaining_seconds", "INTEGER"),
                ("pick_started_at", "DATETIME"),
                ("deadline_at", "DATETIME"),
            ]
            for cname, ctype in draft_cols:
                if cname not in cols:
                    conn.execute(text(f"ALTER TABLE drafts ADD COLUMN {cname} {ctype}"))
