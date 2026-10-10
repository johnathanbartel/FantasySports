"""
Engine/session setup. Defaults to a local SQLite file - fits "internal
first, one real user" with zero setup. Swap to Postgres later by setting
DATABASE_URL; nothing in the schema uses Postgres-only types, so it's a
config change, not a rewrite.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DATABASE_URL:
    if os.environ.get("VERCEL"):
        raise RuntimeError(
            "DATABASE_URL (or POSTGRES_URL) is required on Vercel; "
            "SQLite files are not persistent in Vercel Functions."
        )
    DATABASE_URL = "sqlite:///./fantasy.db"
elif DATABASE_URL.startswith("postgres://"):
    # SQLAlchemy 2 expects the canonical dialect name. Some hosted Postgres
    # integrations still publish the legacy URI scheme.
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://", "postgresql+psycopg2://", 1
    )
elif DATABASE_URL.startswith("postgresql+psycopg://"):
    # Neon's Vercel integration currently emits a Psycopg 3 URL. The API
    # environment uses the stable psycopg2 binary driver, so normalize the
    # dialect explicitly instead of depending on an undeclared driver.
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql+psycopg://", "postgresql+psycopg2://", 1
    )
elif DATABASE_URL.startswith("postgresql://"):
    # SQLAlchemy 2.1 defaults plain PostgreSQL URLs to Psycopg 3. Pin the
    # declared driver so hosted URLs work consistently across SQLAlchemy 2.x.
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://", "postgresql+psycopg2://", 1
    )

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
