"""Shared pytest fixtures.

Provides a hermetic, in-memory SQLite session with all tables created, so model
tests run without touching any real database or external system (BRD §18: mock
external deps, tests are part of done).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vision.db.base import Base

# Import models for their registration side effect so Base.metadata is complete
# before create_all runs.
from vision.db import models  # noqa: F401


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Yield a fresh in-memory SQLite session with the full schema created.

    WHY these specific engine args:
      * ``sqlite://`` (memory) — no file artefacts, fast, isolated per test.
      * ``StaticPool`` + ``check_same_thread=False`` — keep a single shared
        connection so the in-memory DB (which lives only as long as its
        connection) survives across the session's operations.
    Each test gets a brand-new engine, so tests are fully independent (no shared
    mutable state, per testing rules).
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # Build the entire schema from the ORM metadata for this test's engine.
    Base.metadata.create_all(bind=engine)

    TestingSession = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    session = TestingSession()
    try:
        yield session
    finally:
        # Tear down cleanly so no state leaks between tests.
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
