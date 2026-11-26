"""Database engine and sessions. SQLite for tests and the CLI; Postgres arrives with Compose."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.models import Base


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///") and database_url != "sqlite:///:memory:":
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url)


def create_schema(engine: Engine) -> None:
    """Create missing tables. Alembic migrations replace this in Milestone 5."""
    Base.metadata.create_all(engine)


def session_factory(database_url: str) -> sessionmaker[Session]:
    engine = make_engine(database_url)
    create_schema(engine)
    return sessionmaker(engine, expire_on_commit=False)
