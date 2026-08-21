"""
Engine/session setup. Defaults to a local SQLite file - fits "internal
first, one real user" with zero setup. Swap to Postgres later by setting
DATABASE_URL; nothing in the schema uses Postgres-only types, so it's a
config change, not a rewrite.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./fantasy.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
