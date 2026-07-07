"""Health/readiness reporting for Project VISION (BRD §17, NFR-08).

WHY this module exists: NFR-08 requires an observable service — an operator (and
an automated canary, see :mod:`vision.ops.canary`) must be able to ask VISION
"are you healthy?" and get a fast, truthful, secret-free answer. This module
provides:

  * :func:`health_status` — a pure, side-effect-free readiness probe that reads
    the DB and reports four sub-checks: ``db``, ``tokens`` (access/refresh
    expiry), ``last_run`` and ``feeds_ok``.
  * :func:`build_health_router` — a mountable FastAPI ``APIRouter`` exposing
    ``GET /healthz`` that returns **200** when ready and **503** when not
    (fail-closed readiness), with the status JSON as the body. It is designed to
    be ``app.include_router``-ed onto the existing approval web app
    (:mod:`vision.approval.web`).

SECURITY (threat model / §22): the report contains only NON-secret metadata —
expiry *timestamps*, counts, statuses. It never contains a token value, a member
URN, a secret, or a stack trace. Every check catches its own failure and REPORTS
it (fail-closed) rather than raising, so a probe never leaks an internal error to
the caller and a broken dependency reads as "not ready", never as a 500.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from vision.db.models import OAuthToken, Run, Source
from vision.db.session import get_session

_log = logging.getLogger(__name__)

# Only LinkedIn tokens exist today; keying on the provider keeps the check
# correct if a second provider is ever added (config over code).
_PROVIDER = "linkedin"

# How stale an enabled feed's last successful fetch may be before ``feeds_ok``
# flips to False. Default 48h mirrors the ingest recency window; env-overridable
# so an operator can tighten/loosen it without a code change (config over code).
_DEFAULT_FEED_STALE_HOURS = 48.0
_FEED_STALE_ENV = "VISION_FEED_STALE_HOURS"

# A session factory is any zero-arg callable returning a context manager that
# yields a Session (commit-on-success). Prod uses ``get_session``; tests inject an
# in-memory-DB-backed factory of the same shape (same contract as approval.web).
SessionFactory = Callable[[], AbstractContextManager[Session]]

# A clock is injected so the readiness decision is deterministic under test; prod
# uses wall-clock UTC.
Clock = Callable[[], datetime]


def _as_utc(moment: datetime | None) -> datetime | None:
    """Return a timezone-aware UTC datetime (or None), assuming UTC if naive.

    Stored expiries are tz-aware, but a naive value (an older row / a SQLite
    round-trip edge) is coerced to UTC rather than raising, so every comparison
    below is unambiguous and never blows up on a missing ``tzinfo``.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    """Serialise a datetime to an ISO-8601 string (or None) for the JSON body."""
    return moment.isoformat() if moment is not None else None


def _feed_stale_window() -> timedelta:
    """Resolve the feed-staleness window from the environment (config over code).

    A malformed value falls back to the safe default rather than crashing a probe
    that must always answer.
    """
    raw = os.environ.get(_FEED_STALE_ENV)
    if raw is None:
        return timedelta(hours=_DEFAULT_FEED_STALE_HOURS)
    try:
        return timedelta(hours=float(raw))
    except ValueError:
        _log.warning("ignoring non-numeric %s=%r; using default", _FEED_STALE_ENV, raw)
        return timedelta(hours=_DEFAULT_FEED_STALE_HOURS)


# ---------------------------------------------------------------------------
# Immutable sub-check results. Frozen dataclasses (not raw dicts) give callers
# type-checked fields and make the report tamper-proof once built (immutability
# principle); ``to_dict`` renders the JSON-safe shape for the HTTP body.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DbHealth:
    """Whether the database answered a trivial ``SELECT 1``."""

    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class TokensHealth:
    """Aggregate OAuth-token readiness (access/refresh expiry), secret-free.

    ``ok`` is fail-closed: there must be at least one stored credential and every
    stored credential must have a **non-null, still-future** access AND refresh
    expiry. A missing or already-passed expiry means "not ready to publish".
    """

    count: int
    access_expires_at: datetime | None  # earliest access expiry across accounts
    refresh_expires_at: datetime | None  # earliest refresh expiry across accounts
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "access_expires_at": _iso(self.access_expires_at),
            "refresh_expires_at": _iso(self.refresh_expires_at),
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LastRunHealth:
    """Status of the most recent pipeline run (diagnostic, not a readiness gate)."""

    run_id: str | None
    status: str | None
    at: datetime | None
    ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "at": _iso(self.at),
            "ok": self.ok,
        }


@dataclass(frozen=True)
class HealthReport:
    """The full readiness report: ``{db, tokens, last_run, feeds_ok}`` (NFR-08).

    ``ok`` — which drives the 200/503 — is deliberately gated on ONLY ``db`` and
    ``tokens``: those are the two conditions under which the service genuinely
    cannot do its job (no database, or no usable publish credential). ``feeds_ok``
    and ``last_run`` are reported as diagnostics but do NOT fail the readiness
    probe, so a single stale feed or a prior partial run cannot flap the probe and
    trigger a needless restart (the VPS memory-overload incident taught us to
    avoid restart storms).
    """

    db: DbHealth
    tokens: TokensHealth
    last_run: LastRunHealth
    feeds_ok: bool

    @property
    def ok(self) -> bool:
        """Overall readiness: DB reachable AND at least one usable credential."""
        return self.db.ok and self.tokens.ok

    def to_dict(self) -> dict[str, Any]:
        """Render the JSON-serialisable body (datetimes → ISO strings)."""
        return {
            "status": "ok" if self.ok else "degraded",
            "db": self.db.to_dict(),
            "tokens": self.tokens.to_dict(),
            "last_run": self.last_run.to_dict(),
            "feeds_ok": self.feeds_ok,
        }


def _check_db(session: Session) -> DbHealth:
    """Probe the database with a trivial query; report, never raise.

    ``SELECT 1`` is the cheapest proof the connection is alive on both SQLite and
    Postgres. Any failure is caught (health must REPORT, not raise) and the class
    name — never the exception args, which could carry a DSN — is logged.
    """
    try:
        session.execute(sql_text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — a readiness probe reports, never raises
        _log.warning("healthz DB check failed: %s", type(exc).__name__)
        return DbHealth(ok=False, detail="database unreachable")
    return DbHealth(ok=True, detail="database reachable")


def _check_tokens(session: Session, now: datetime) -> TokensHealth:
    """Aggregate OAuth-token readiness without exposing any token value.

    Reports the *earliest* access/refresh expiry across accounts (the soonest
    thing that will break) and a fail-closed ``ok``: at least one credential, and
    no credential with a missing or already-expired access/refresh token.
    """
    try:
        tokens = session.scalars(
            select(OAuthToken).where(OAuthToken.provider == _PROVIDER)
        ).all()
    except Exception as exc:  # noqa: BLE001 — report, never raise
        _log.warning("healthz token check failed: %s", type(exc).__name__)
        return TokensHealth(0, None, None, ok=False, detail="token store unreadable")

    if not tokens:
        # No credential means the publisher cannot post — fail closed.
        return TokensHealth(0, None, None, ok=False, detail="no oauth tokens stored")

    access_expiries = [_as_utc(t.access_expires_at) for t in tokens]
    refresh_expiries = [_as_utc(t.refresh_expires_at) for t in tokens]

    # A credential is usable only if BOTH expiries are known and still in the
    # future. Any unknown (None) or past expiry makes the whole set not-ready.
    all_valid = all(
        a is not None and a > now and r is not None and r > now
        for a, r in zip(access_expiries, refresh_expiries)
    )
    earliest_access = min((a for a in access_expiries if a is not None), default=None)
    earliest_refresh = min((r for r in refresh_expiries if r is not None), default=None)
    detail = "all tokens valid" if all_valid else "a token is missing or expired"
    return TokensHealth(
        count=len(tokens),
        access_expires_at=earliest_access,
        refresh_expires_at=earliest_refresh,
        ok=all_valid,
        detail=detail,
    )


def _check_last_run(session: Session) -> LastRunHealth:
    """Report the most recent run's status (diagnostic only).

    ``ok`` is True when the latest run did not end in ``failed``; it never gates
    the 200/503 (see :class:`HealthReport`). Absence of any run reads as not-ok
    but, again, is only diagnostic.
    """
    try:
        run = session.scalars(
            select(Run).order_by(Run.created_at.desc()).limit(1)
        ).first()
    except Exception as exc:  # noqa: BLE001 — report, never raise
        _log.warning("healthz last-run check failed: %s", type(exc).__name__)
        return LastRunHealth(run_id=None, status=None, at=None, ok=False)

    if run is None:
        return LastRunHealth(run_id=None, status=None, at=None, ok=False)
    return LastRunHealth(
        run_id=str(run.id),
        status=run.status,
        at=_as_utc(run.updated_at),
        ok=run.status != "failed",
    )


def _check_feeds(session: Session, now: datetime, stale_window: timedelta) -> bool:
    """Return True when every *enabled* source fetched within the staleness window.

    Diagnostic signal: an enabled feed that has never succeeded (``last_ok_at`` is
    NULL) or that last succeeded outside the window is treated as not-ok. With no
    enabled sources at all there is nothing ingesting, which also reads as not-ok.
    """
    try:
        sources = session.scalars(
            select(Source).where(Source.enabled.is_(True))
        ).all()
    except Exception as exc:  # noqa: BLE001 — report, never raise
        _log.warning("healthz feeds check failed: %s", type(exc).__name__)
        return False

    if not sources:
        return False
    cutoff = now - stale_window
    return all(
        (last_ok := _as_utc(s.last_ok_at)) is not None and last_ok >= cutoff
        for s in sources
    )


def health_status(session: Session, *, now: datetime | None = None) -> HealthReport:
    """Build the readiness report for VISION (BRD §17, NFR-08).

    Pure and side-effect-free: it only READS the database and returns a
    :class:`HealthReport` whose fields are exactly ``db``, ``tokens``
    (access/refresh expiry), ``last_run`` and ``feeds_ok``. ``now`` is injectable
    for deterministic tests; production omits it and gets wall-clock UTC. No check
    raises — each degrades to a "not ok" sub-result so the probe always answers.
    """
    reference = _as_utc(now) or datetime.now(timezone.utc)
    return HealthReport(
        db=_check_db(session),
        tokens=_check_tokens(session, reference),
        last_run=_check_last_run(session),
        feeds_ok=_check_feeds(session, reference, _feed_stale_window()),
    )


def build_health_router(
    *,
    session_factory: SessionFactory | None = None,
    clock: Clock | None = None,
) -> APIRouter:
    """Build a mountable ``GET /healthz`` router (200 ready / 503 not-ready).

    Dependencies are injected so the router is trivially testable against an
    in-memory database and a fixed clock, and so production wiring stays
    declarative. Mount it with ``app.include_router(build_health_router())`` on
    the approval web app. The route ALWAYS returns a JSON body; it fails closed to
    503 if a session cannot even be opened (fail-closed readiness).
    """
    factory: SessionFactory = session_factory or get_session
    tick: Clock = clock or (lambda: datetime.now(timezone.utc))
    router = APIRouter()

    @router.get("/healthz")
    def healthz() -> JSONResponse:
        """Readiness probe: 200 when DB + credentials are ready, else 503."""
        try:
            with factory() as session:
                report = health_status(session, now=tick())
        except Exception as exc:  # noqa: BLE001 — probe must answer, not 500
            # Could not even acquire a session → treat as database-down, 503.
            _log.error("healthz could not open a session: %s", type(exc).__name__)
            return JSONResponse(
                {
                    "status": "degraded",
                    "db": {"ok": False, "detail": "session unavailable"},
                },
                status_code=503,
            )
        return JSONResponse(report.to_dict(), status_code=200 if report.ok else 503)

    return router
