"""Database engine, Alembic migrations, and sessions (Postgres in Compose, SQLite elsewhere)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///") and database_url != "sqlite:///:memory:":
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url, pool_pre_ping=True)


def migrate(engine: Engine) -> None:
    """Bring the database up to the latest Alembic revision."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "api" / "migrations"))
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def session_factory(database_url: str) -> sessionmaker[Session]:
    engine = make_engine(database_url)
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)
