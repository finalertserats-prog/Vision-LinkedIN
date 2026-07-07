"""Tests for the Phase-4 observability lane (BRD §17, NFR-08).

Covers the three new ops modules, fully hermetic (BRD §18 — no real network, no
real SMTP, no real model calls):

  * ``/healthz`` returns 200 when the service is healthy and 503 when the DB or a
    token is bad — driven against an in-memory SQLite DB and a fixed clock.
  * ``record_run`` opens and closes a ``runs`` row, persisting its stats, and
    records a ``failed`` run even when the wrapped block raises.
  * ``canary`` detects a failing/unreachable health endpoint with ``httpx``
    mocked, and ``main`` alerts on failure (mock sender) but stays silent in
    ``dry_run``.

AAA (Arrange → Act → Assert), one behaviour per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vision.config import VisionEnv, get_settings
from vision.db.base import Base
from vision.db import models  # noqa: F401 — register tables on Base.metadata
from vision.db.models import OAuthToken, Run, Source
from vision.ops import canary as canary_mod
from vision.ops.canary import CanaryResult, canary
from vision.ops.health import build_health_router, health_status
from vision.ops.run_record import (
    RUN_STATUS_FAILED,
    RUN_STATUS_OK,
    RUN_STATUS_PARTIAL,
    record_run,
)

# A fixed reference "now" so every expiry-window comparison is deterministic.
_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


# --- Hermetic engine + session-factory helpers -----------------------------


def _memory_engine() -> Engine:
    """Build a fresh in-memory SQLite engine with the full schema.

    A shared single connection (``StaticPool`` + ``check_same_thread=False``) is
    used so the in-memory DB survives across the multiple sessions that
    ``record_run`` and the health router open.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    return engine


def _factory_for(engine: Engine):
    """Return a session-factory (zero-arg → commit-on-success context manager)."""
    maker = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @contextmanager
    def factory() -> Iterator[Session]:
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return factory


def _seed_token(
    engine: Engine,
    *,
    access_expires_at: datetime,
    refresh_expires_at: datetime,
    member_urn: str = "urn:li:person:TEST",
) -> None:
    """Insert one LinkedIn OAuth row with the given (non-secret) expiries."""
    with _factory_for(engine)() as session:
        session.add(
            OAuthToken(
                provider="linkedin",
                member_urn=member_urn,
                access_expires_at=access_expires_at,
                refresh_expires_at=refresh_expires_at,
            )
        )


def _client_for(engine: Engine) -> TestClient:
    """Mount the health router on a bare app with a fixed clock and return a client."""
    app = FastAPI()
    app.include_router(
        build_health_router(session_factory=_factory_for(engine), clock=lambda: _NOW)
    )
    return TestClient(app)


# ===========================================================================
# /healthz — readiness contract
# ===========================================================================


def test_healthz_returns_200_when_healthy() -> None:
    # Arrange: a DB with a valid, non-expired token and a freshly-fetched feed.
    engine = _memory_engine()
    _seed_token(
        engine,
        access_expires_at=_NOW + timedelta(days=30),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    with _factory_for(engine)() as session:
        session.add(
            Source(
                name="STAT News", lane="hc", kind="rss", url="https://x.test/feed",
                enabled=True, last_ok_at=_NOW - timedelta(hours=1),
            )
        )

    # Act.
    response = _client_for(engine).get("/healthz")

    # Assert: ready → 200, and the body reports every sub-check healthy.
    assert response.status_code == 200
    body = response.json()
    assert body["db"]["ok"] is True
    assert body["tokens"]["ok"] is True
    assert body["feeds_ok"] is True


def test_healthz_returns_503_when_token_expired() -> None:
    # Arrange: the DB answers, but the only access token has already expired.
    engine = _memory_engine()
    _seed_token(
        engine,
        access_expires_at=_NOW - timedelta(days=1),  # expired
        refresh_expires_at=_NOW + timedelta(days=300),
    )

    # Act.
    response = _client_for(engine).get("/healthz")

    # Assert: fail-closed readiness → 503, with the token sub-check flagged.
    assert response.status_code == 503
    body = response.json()
    assert body["db"]["ok"] is True
    assert body["tokens"]["ok"] is False


def test_healthz_returns_503_when_no_tokens_stored() -> None:
    # Arrange: DB is fine but there is no credential to publish with.
    engine = _memory_engine()

    # Act.
    response = _client_for(engine).get("/healthz")

    # Assert: no usable credential → not ready.
    assert response.status_code == 503
    assert response.json()["tokens"]["ok"] is False


def test_healthz_returns_503_when_db_unavailable() -> None:
    # Arrange: a session factory that cannot open a session (database down).
    @contextmanager
    def _broken_factory() -> Iterator[Session]:
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover — unreachable, present to type as a generator

    app = FastAPI()
    app.include_router(
        build_health_router(session_factory=_broken_factory, clock=lambda: _NOW)
    )

    # Act.
    response = TestClient(app).get("/healthz")

    # Assert: the probe fails closed to 503 rather than raising a 500.
    assert response.status_code == 503
    assert response.json()["db"]["ok"] is False


def test_health_status_reports_db_error_without_raising() -> None:
    # Arrange: a session whose execute raises (simulated DB fault).
    engine = _memory_engine()
    with _factory_for(engine)() as session:
        session.execute = Mock(side_effect=RuntimeError("connection reset"))  # type: ignore[method-assign]

        # Act: health_status must REPORT the failure, not propagate it.
        report = health_status(session, now=_NOW)

    # Assert.
    assert report.db.ok is False
    assert report.ok is False


# ===========================================================================
# record_run — runs row lifecycle + stats
# ===========================================================================


def test_record_run_opens_and_closes_row_with_stats() -> None:
    # Arrange: an in-memory DB and its session factory.
    engine = _memory_engine()
    factory = _factory_for(engine)

    # Act: run a successful block that records some stats.
    with record_run(factory) as handle:
        handle.stats.incr("items_ingested", 5)
        handle.stats.record_model_version("generate", "gemini-x")
        handle.stats.record_token_usage("gemini-x", 1200)

    # Assert: exactly one runs row, closed 'ok', with the stats persisted.
    with factory() as session:
        runs = session.scalars(select(Run)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == RUN_STATUS_OK
    assert run.stats["counts"]["items_ingested"] == 5
    assert run.stats["model_versions"]["generate"] == "gemini-x"
    assert run.stats["token_usage"]["gemini-x"] == 1200
    assert "total_seconds" in run.stats["timings"]


def test_record_run_marks_partial_when_degraded() -> None:
    # Arrange.
    engine = _memory_engine()
    factory = _factory_for(engine)

    # Act: a run that completes but flagged itself degraded.
    with record_run(factory) as handle:
        handle.stats.mark_partial()

    # Assert: the terminal status is 'partial', not 'ok'.
    with factory() as session:
        run = session.scalars(select(Run)).one()
    assert run.status == RUN_STATUS_PARTIAL


def test_record_run_records_failed_when_block_raises() -> None:
    # Arrange.
    engine = _memory_engine()
    factory = _factory_for(engine)

    # Act: the wrapped block raises — the error must propagate...
    with pytest.raises(ValueError):
        with record_run(factory) as handle:
            handle.stats.incr("items_ingested", 2)
            raise ValueError("pipeline blew up")

    # Assert: ...yet the run is durably recorded as 'failed' (written on an
    # independent session, so a pipeline rollback could not have erased it).
    with factory() as session:
        run = session.scalars(select(Run)).one()
    assert run.status == RUN_STATUS_FAILED
    assert "total_seconds" in run.stats["timings"]


# ===========================================================================
# canary — external prober + failure alerting
# ===========================================================================


def test_canary_passes_on_200() -> None:
    # Arrange: a fake getter returning HTTP 200.
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return httpx.Response(status_code=200)

    # Act.
    result = canary("http://svc/healthz", http_get=fake_get)

    # Assert.
    assert result.ok is True
    assert result.status_code == 200


def test_canary_detects_failing_health_endpoint() -> None:
    # Arrange: the endpoint reports 'not ready' (503).
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return httpx.Response(status_code=503)

    # Act.
    result = canary("http://svc/healthz", http_get=fake_get)

    # Assert: a non-200 is a fail carrying the code.
    assert result.ok is False
    assert result.status_code == 503


def test_canary_detects_unreachable_endpoint() -> None:
    # Arrange: the service is down — the transport raises.
    def fake_get(url: str, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    # Act.
    result = canary("http://svc/healthz", http_get=fake_get)

    # Assert: unreachable → fail with no HTTP code.
    assert result.ok is False
    assert result.status_code is None


def test_main_alerts_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: force a failing probe, a LIVE env, and a mock sender.
    get_settings.cache_clear()
    live = get_settings().model_copy(update={"vision_env": VisionEnv.LIVE})
    sender = Mock()
    sender.send.return_value = True
    monkeypatch.setattr(canary_mod, "get_settings", lambda: live)
    monkeypatch.setattr(canary_mod, "get_sender", lambda settings: sender)
    monkeypatch.setattr(
        canary_mod, "canary", lambda url: CanaryResult(False, 503, "healthz returned HTTP 503")
    )

    # Act.
    exit_code = canary_mod.main()

    # Assert: non-zero exit AND exactly one alert email attempted.
    assert exit_code == 1
    sender.send.assert_called_once()


def test_main_suppresses_alert_in_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: a failing probe but DRY_RUN mode (must stay side-effect free).
    get_settings.cache_clear()
    dry = get_settings().model_copy(update={"vision_env": VisionEnv.DRY_RUN})
    sender = Mock()
    monkeypatch.setattr(canary_mod, "get_settings", lambda: dry)
    monkeypatch.setattr(canary_mod, "get_sender", lambda settings: sender)
    monkeypatch.setattr(
        canary_mod, "canary", lambda url: CanaryResult(False, None, "transport error")
    )

    # Act.
    exit_code = canary_mod.main()

    # Assert: still non-zero, but NO email sent in dry_run.
    assert exit_code == 1
    sender.send.assert_not_called()
