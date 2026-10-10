"""Offline verification for the NHL series foundation (FS-020).

Uses a temporary SQLite database to verify the Alembic upgrade/downgrade,
NHL league-setting API validation, ORM relationships, constraints, and
duplicate daily-game protection. No external services are called.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_series_schema.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    # Keep the auto-cleaned throwaway directory under the repository so a
    # Windows sandbox can open it. The extensionless file also cannot be
    # mistaken for one of the repository's protected *.db databases.
    with tempfile.TemporaryDirectory(prefix="fs020-series-", dir=ROOT) as temp_dir:
        db_path = Path(temp_dir) / "series-verification"
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

        from alembic import command
        from alembic.config import Config
        from fastapi import HTTPException
        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.exc import IntegrityError

        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))

        # Migrate to the predecessor, seed one pre-existing NHL league, and
        # prove the new revision both backfills and completely downgrades.
        command.upgrade(config, "b7c3e9a1d4f8")
        legacy_engine = create_engine(os.environ["DATABASE_URL"])
        with legacy_engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users (id, email, display_name) "
                "VALUES (1, 'legacy@example.com', 'Legacy Commissioner')"
            ))
            connection.execute(text(
                "INSERT INTO universes "
                "(id, sport, level, code, display_name, supports_individual_defense) "
                "VALUES (1, 'hockey', 'pro', 'nhl', 'National Hockey League', 0)"
            ))
            connection.execute(text(
                "INSERT INTO leagues "
                "(id, name, commissioner_user_id, size, draft_rounds, salary_cap_enabled, "
                "program_size_mode, draft_type) "
                "VALUES (1, 'Existing NHL League', 1, 12, 20, 0, 'team_choice', 'snake')"
            ))
            connection.execute(text(
                "INSERT INTO league_universes (league_id, universe_id, defense_mode) "
                "VALUES (1, 1, 'group')"
            ))
        legacy_engine.dispose()

        command.upgrade(config, "head")
        migrated_engine = create_engine(os.environ["DATABASE_URL"])
        with migrated_engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT series_length_days FROM leagues WHERE id = 1"
            )) == 7
        assert {"league_series", "series_daily_games"}.issubset(inspect(migrated_engine).get_table_names())
        migrated_engine.dispose()

        command.downgrade(config, "b7c3e9a1d4f8")
        downgraded_engine = create_engine(os.environ["DATABASE_URL"])
        downgraded_inspector = inspect(downgraded_engine)
        assert "league_series" not in downgraded_inspector.get_table_names()
        assert "series_daily_games" not in downgraded_inspector.get_table_names()
        assert "series_length_days" not in {
            column["name"] for column in downgraded_inspector.get_columns("leagues")
        }
        downgraded_engine.dispose()
        command.upgrade(config, "head")
        print("Migration upgrade/downgrade round-trip: passed")

        # Imports must follow DATABASE_URL setup so the app's engine points at
        # this temporary database rather than a developer database.
        from fantasy_sports.api.routers.leagues import create_league as create_league_route
        from fantasy_sports.api.schemas import LeagueCreateRequest, LeagueOut
        from fantasy_sports.platform.db.models.core import (
            League,
            LeagueSeries,
            SeriesDailyGame,
            Team,
            Universe,
            User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        db = SessionLocal()
        commissioner = User(email="series@example.com", display_name="Series Commissioner")
        ncaaf = Universe(
            sport="football",
            level="college",
            code="ncaaf",
            display_name="College Football",
            supports_individual_defense=False,
        )
        db.add_all([commissioner, ncaaf])
        db.commit()
        commissioner_id = commissioner.id
        print("API verification reference data: seeded")

        def create_league(name: str, universe: str, **settings: int) -> LeagueOut:
            payload = LeagueCreateRequest.model_validate(
                {
                    "name": name,
                    "commissioner_user_id": commissioner_id,
                    "universe_codes": [universe],
                    "size": 2,
                    "draft_rounds": 1,
                    **settings,
                }
            )
            return LeagueOut.model_validate(create_league_route(payload, db, commissioner))

        default_response = create_league("Default Series", "nhl")
        assert default_response.series_length_days == 7
        print("Default NHL series setting: passed")

        for length in (5, 10):
            response = create_league(
                f"{length}-Day Series", "nhl", series_length_days=length
            )
            assert response.series_length_days == length

        try:
            create_league("Invalid Series", "nhl", series_length_days=6)
            raise AssertionError("invalid NHL series length was accepted")
        except HTTPException as exc:
            assert exc.status_code == 400

        football_response = create_league("Weekly Football", "ncaaf")
        assert football_response.series_length_days is None
        try:
            create_league("Invalid Football Series", "ncaaf", series_length_days=7)
            raise AssertionError("non-NHL series length was accepted")
        except HTTPException as exc:
            assert exc.status_code == 400
        print("League creation defaults and validation: passed")

        league = db.get(League, default_response.id)
        team_a = Team(
            league_id=league.id,
            name="Team A",
            owner_user_id=commissioner_id,
            is_ai_owned=False,
        )
        team_b = Team(league_id=league.id, name="Team B", is_ai_owned=True)
        db.add_all([team_a, team_b])
        db.flush()

        start_date = datetime.date(2026, 10, 12)
        series = LeagueSeries(
            league_id=league.id,
            team_a_id=team_a.id,
            team_b_id=team_b.id,
            start_date=start_date,
            end_date=start_date + datetime.timedelta(days=6),
            total_days=7,
        )
        db.add(series)
        db.flush()
        daily_game = SeriesDailyGame(
            series_id=series.id,
            calendar_date=start_date,
            home_team_id=team_a.id,
            away_team_id=team_b.id,
        )
        db.add(daily_game)
        db.commit()
        db.refresh(series)
        db.refresh(daily_game)
        assert series.daily_games == [daily_game]
        assert league.series == [series]
        assert series.created_at is not None and daily_game.created_at is not None
        assert daily_game.current_period == 0 and daily_game.seconds_remaining == 1200

        duplicate = SeriesDailyGame(
            series_id=series.id,
            calendar_date=start_date,
            home_team_id=team_a.id,
            away_team_id=team_b.id,
        )
        db.add(duplicate)
        try:
            db.commit()
            raise AssertionError("duplicate series/team/date game was accepted")
        except IntegrityError:
            db.rollback()
        finally:
            db.close()
        print("Series ORM round-trip and duplicate protection: passed")
        print("FS-020 NHL series foundation verification passed")
        engine.dispose()


if __name__ == "__main__":
    main()
