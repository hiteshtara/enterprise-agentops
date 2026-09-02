import os

from sqlalchemy import create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    sessionmaker,
)

DEFAULT_DATABASE_URL = "sqlite:///./agentops.db"

DATABASE_URL_ENV_VAR = "AGENTOPS_DATABASE_URL"


class Base(DeclarativeBase):
    pass


def resolve_database_url() -> str:
    """Return the configured database URL, falling back to the local default."""
    return os.environ.get(
        DATABASE_URL_ENV_VAR,
        DEFAULT_DATABASE_URL,
    )


class Database:
    """Owns the engine and session factory for a single database URL.

    Instances are injected into the stores so that tests can point at an
    isolated database without patching module-level globals.
    """

    def __init__(
        self,
        url: str | None = None,
    ) -> None:
        self.url = url or resolve_database_url()

        connect_args = {}

        if self.url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        self.engine = create_engine(
            self.url,
            connect_args=connect_args,
        )

        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )

    def session(self) -> Session:
        return self.session_factory()

    def create_all(self) -> None:
        """Create any missing tables. Idempotent."""
        from app import db_models  # noqa: F401  -- registers mappers on Base

        Base.metadata.create_all(bind=self.engine)

    def dispose(self) -> None:
        self.engine.dispose()


_default_database: Database | None = None


def get_database() -> Database:
    """Return the process-wide default Database, creating it on first use.

    Created lazily so that importing app modules has no side effects.
    """
    global _default_database

    if _default_database is None:
        _default_database = Database()

    return _default_database
