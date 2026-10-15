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

        if "simulator_games" in tables:
            cols = {c["name"] for c in insp.get_columns("simulator_games")}
            if "published_periods" not in cols:
                conn.execute(text("ALTER TABLE simulator_games ADD COLUMN published_periods INTEGER"))

        if "series_daily_games" in tables:
            cols = {c["name"] for c in insp.get_columns("series_daily_games")}
            if "calendar_date_end" not in cols:
                conn.execute(text("ALTER TABLE series_daily_games ADD COLUMN calendar_date_end DATE"))
            if "window_index" not in cols:
                conn.execute(text("ALTER TABLE series_daily_games ADD COLUMN window_index INTEGER"))

        if "teams" in tables:
            cols = {c["name"] for c in insp.get_columns("teams")}
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
