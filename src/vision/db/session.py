"""Engine, session factory and helpers derived from settings.DATABASE_URL.

WHY this module exists: a single place to build the SQLAlchemy engine means the
SQLite-vs-Postgres differences (connect args, pooling) are handled once and the
rest of the app just asks for a session. Keeps the data layer DB-agnostic (§22).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from vision.config import get_settings
from vision.db.base import Base

# Import models so that ``Base.metadata`` is fully populated whenever the session
# module is imported. Without this, ``create_all`` / Alembic autogenerate could
# run against an empty metadata if models were never imported elsewhere.
from vision.db import models  # noqa: F401  (imported for side effect: table registration)


def _build_engine() -> Engine:
    """Construct the SQLAlchemy engine from configuration.

    SQLite needs ``check_same_thread=False`` so a connection can be shared across
    threads (FastAPI/uvicorn use a thread pool); this arg is invalid for other
    backends, so it is applied only for SQLite. ``future=True`` opts into 2.0
    semantics explicitly.
    """
    settings = get_settings()
    connect_args: dict[str, object] = {}
    if settings.is_sqlite:
        # Allow cross-thread use of the single SQLite connection.
        connect_args["check_same_thread"] = False
    return create_engine(settings.database_url, future=True, connect_args=connect_args)


# Module-level singletons: one engine + one session factory per process, mirroring
# the cached settings. ``expire_on_commit=False`` keeps objects usable after commit
# (avoids surprise lazy-load errors in request handlers).
engine: Engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a transactional session, committing on success and rolling back on error.

    Using a contextmanager guarantees the session is always closed and that a
    failed unit of work never leaves a half-applied transaction (fail-closed,
    NFR-04). Callers write:  ``with get_session() as s: ...``.
    """
    session = SessionLocal()
    try:
        yield session
        # Commit only if the caller's block completed without raising.
        session.commit()
    except Exception:
        # Any error rolls the whole unit of work back — no partial writes.
        session.rollback()
        raise
    finally:
        # Always release the connection back to the pool.
        session.close()


def create_all() -> None:
    """Create all tables for local/dev use (SQLite).

    Convenience for dev and tests only. Production schema changes go through
    Alembic migrations (see db/migrations), never through ``create_all``.
    """
    Base.metadata.create_all(bind=engine)
