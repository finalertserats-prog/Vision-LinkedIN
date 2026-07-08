"""Shared pytest fixtures.

Provides a hermetic, in-memory SQLite session with all tables created, so model
tests run without touching any real database or external system (BRD §18: mock
external deps, tests are part of done).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vision.db.base import Base

# Import models for their registration side effect so Base.metadata is complete
# before create_all runs.
from vision.db import models  # noqa: F401
from vision.db.models import Item, Run, Source


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so ``@pytest.mark.integration`` isn't a warning.

    ``integration`` flags the tests that spawn the real bundled ffmpeg (the video
    assembly render), so they can be selected/deselected without editing pyproject.
    """
    config.addinivalue_line(
        "markers", "integration: real end-to-end render (spawns the bundled ffmpeg)"
    )


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


# ---------------------------------------------------------------------------
# Phase-1 pipeline fixture.
#
# WHY this lives in conftest (per the integration-test spec): the end-to-end test
# needs a small, deterministic corpus seeded across BOTH content lanes (hc + ai)
# in the hermetic in-memory SQLite DB. Keeping the seeding here makes the seeded
# corpus a reusable, single-source-of-truth artefact and keeps the test body
# focused on behaviour (Arrange stays tiny).
# ---------------------------------------------------------------------------

# A fixed reference "now" so recency scoring is fully deterministic across runs.
PHASE1_NOW: datetime = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SeededItem:
    """A seeded ``Item`` paired with the exact grounded number/label a card must show.

    ``number`` and ``label`` are the *source of truth* for what the deterministic
    card renderer is allowed to display for this item (BRD §13.6 — every rendered
    figure traces to a grounded source item). Frozen so the corpus can't drift
    mid-test.
    """

    item: Item  # the persisted ORM row (carries the real UUID id)
    number: str  # the exact fixture figure, e.g. "18%" — must appear verbatim on a card
    label: str  # the card label describing that figure


@dataclass(frozen=True)
class Phase1Fixture:
    """The seeded corpus handed to the integration test."""

    session: Session  # the live in-memory session (already populated)
    run: Run  # the parent run row tying items to a daily execution
    seeded: list[SeededItem]  # every seeded item + its grounded figure


# Declarative corpus: two lanes, each with two recent, distinct signals. Every
# item carries a concrete figure so the informative-card path has real,
# traceable numbers to render (config-shaped test data, not inline magic).
_CORPUS: tuple[dict[str, object], ...] = (
    {
        "source_name": "STAT News",
        "lane": "hc",
        "authority_weight": 0.95,
        "title": "Hospital cuts claim denials with revenue-cycle automation",
        "url": "https://example.test/hc/denials-automation",
        "summary": "A 200-bed hospital reduced claim denials by 18% after deploying RCM automation.",
        "hours_ago": 6,
        "number": "18%",
        "label": "Claim-denial reduction",
    },
    {
        "source_name": "Fierce Healthcare",
        "lane": "hc",
        "authority_weight": 0.85,
        "title": "Health system reports faster prior-authorisation turnaround",
        "url": "https://example.test/hc/prior-auth-turnaround",
        "summary": "Prior-authorisation decisions returned in 24 hours on average, down from 72.",
        "hours_ago": 20,
        "number": "24h",
        "label": "Prior-auth turnaround",
    },
    {
        "source_name": "Import AI",
        "lane": "ai",
        "authority_weight": 0.85,
        "title": "Open model matches prior systems on clinical-note summarisation",
        "url": "https://example.test/ai/clinical-notes-model",
        "summary": "An open model cleared 223 evaluation cases on note summarisation this quarter.",
        "hours_ago": 10,
        "number": "223",
        "label": "Eval cases cleared",
    },
    {
        "source_name": "arXiv cs.CL",
        "lane": "ai",
        "authority_weight": 0.80,
        "title": "Benchmark shows accuracy gains on medical question answering",
        "url": "https://example.test/ai/medical-qa-benchmark",
        "summary": "Reported medical-QA accuracy rose to 87% on the shared benchmark.",
        "hours_ago": 30,
        "number": "87%",
        "label": "Medical-QA accuracy",
    },
)


@pytest.fixture
def phase1_fixture(db_session: Session) -> Phase1Fixture:
    """Seed both lanes into in-memory SQLite and return the corpus + session.

    Builds one ``Run`` and one ``Source`` per corpus entry (respecting the FK
    order), then an ``Item`` per entry with a recent ``published_at`` relative to
    ``PHASE1_NOW`` so every item survives the recency filter. Returns a
    ``Phase1Fixture`` the test uses to drive curate -> synthesise -> render.
    """
    run = Run(status="ok", notes="phase-1 integration fixture")
    db_session.add(run)
    db_session.flush()  # assign the run PK before items reference it

    seeded: list[SeededItem] = []
    for entry in _CORPUS:
        source = Source(
            name=str(entry["source_name"]),
            lane=str(entry["lane"]),
            kind="rss",
            url=f"{entry['url']}#feed",  # a distinct feed URL per source
            authority_weight=float(entry["authority_weight"]),
            enabled=True,
        )
        db_session.add(source)
        db_session.flush()  # assign the source PK for the item FK

        item = Item(
            source_id=source.id,
            run_id=run.id,
            lane=str(entry["lane"]),
            title=str(entry["title"]),
            url=str(entry["url"]),
            summary=str(entry["summary"]),
            # Recent publish time keeps the item inside the default 48h window.
            published_at=PHASE1_NOW - timedelta(hours=int(entry["hours_ago"])),
        )
        db_session.add(item)
        db_session.flush()  # assign the item PK so its UUID is usable as provenance
        seeded.append(
            SeededItem(item=item, number=str(entry["number"]), label=str(entry["label"]))
        )

    return Phase1Fixture(session=db_session, run=run, seeded=seeded)
