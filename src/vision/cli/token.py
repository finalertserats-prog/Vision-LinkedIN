"""``vision-token`` entry point (daily cron, BRD §10.2 / §15.3).

WHY this module exists: it is the thin CLI wiring that drives the REAL token
lifecycle job (:func:`vision.publish.token_refresh.refresh_if_needed`). Phase 0
shipped a scaffold; this now calls the genuine refresh routine — which refreshes
any LinkedIn access token inside its expiry window, re-encrypts and atomically
stores the result under a per-account lock, and emails a re-auth alert (keeping
stored state intact) when a refresh can no longer succeed. It rebuilds none of
that; it only opens a session, supplies the clock, and maps the outcomes to a
cron exit code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from vision.config import get_settings
from vision.db.session import get_session
from vision.logging_setup import configure_logging, get_logger
from vision.publish.token_refresh import (
    STATUS_DEAD_LETTERED,
    STATUS_REAUTH_ALERTED,
    refresh_if_needed,
)

logger = get_logger("vision.cli.token")


def main() -> int:
    """Refresh near-expiry LinkedIn tokens; signal if any account needs re-auth.

    Opens a transactional session (token replacements + audit rows commit atomically
    on success / roll back on error), refreshes what is due, and returns a process
    exit code: ``0`` when every account is healthy, ``1`` when at least one needs the
    owner to reconnect / was dead-lettered, OR when an unforeseen fault fails the job
    closed — so the cron wrapper can alert without the job itself crash-looping or
    dumping a secret-bearing traceback (this is the most secret-sensitive cron).
    """
    # Correlation id lets an operator find the sanitized failure in the logs from a
    # cron alert without us ever logging the provider's raw error text. Minted before
    # ANY startup work so even a config/secret-load failure can reference it.
    correlation_id = uuid4().hex

    try:
        # Startup (logging + config/secret parsing) is INSIDE the boundary: for the
        # most secret-sensitive job, a bad env/secret must fail closed with a
        # sanitized log, never escape as a raw (possibly secret-bearing) traceback.
        configure_logging()
        settings = get_settings()
        logger.info("vision-token invoked", extra={"env": settings.vision_env.value})
        with get_session() as session:
            outcomes = refresh_if_needed(session, datetime.now(timezone.utc), settings=settings)
    except Exception as exc:  # noqa: BLE001 — cron boundary: degrade, never crash-loop
        # Fail-closed (§22.9) mirroring ``daily.main``, but hardened for the token
        # job's sensitivity: log ONLY the exception class + correlation id. We do NOT
        # use ``logger.exception``/``exc_info`` — a traceback or ``str(exc)`` can
        # carry provider/token text and must never reach the logs. ``get_session``
        # already rolled back, so no token state is left half-written.
        logger.error(
            "vision-token crashed unexpectedly (fail-closed)",
            extra={"error_type": exc.__class__.__name__, "correlation_id": correlation_id},
        )
        return 1

    # Summarise without leaking anything sensitive (status counts only).
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    logger.info("vision-token complete", extra={"counts": counts})

    needs_attention = any(
        outcome.status in (STATUS_REAUTH_ALERTED, STATUS_DEAD_LETTERED) for outcome in outcomes
    )
    return 1 if needs_attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
