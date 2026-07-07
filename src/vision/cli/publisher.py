"""``vision-publisher`` entry point (poller, every ~5 min, BRD §10.2).

WHY this module exists: it is the thin CLI wiring that drives the REAL publish
worker (:class:`vision.publish.worker.LinkedInPublisher`). Phase 0 shipped a
scaffold; this now calls the genuine ``poll_and_publish`` (publish every approved
& due draft, at most once) plus ``reap_stuck`` (alert on any draft stranded in
``queued`` past its lease) — reusing the worker's full §15.4 error matrix,
idempotency, and token handling. It rebuilds none of that; it only opens a
session, hands the worker the clock, and maps the outcome to a cron exit code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from vision.config import get_settings
from vision.db.session import get_session
from vision.logging_setup import configure_logging, get_logger
from vision.publish.worker import LinkedInPublisher

logger = get_logger("vision.cli.publisher")


def main() -> int:
    """Publish every ``approved && due`` draft, then alert on any stuck draft.

    Returns a process exit code (0 = clean run, 1 = fail-closed) so the cron wrapper
    can alert on a non-zero exit. The worker owns its own error handling per draft
    (one bad draft never aborts the batch); this boundary guarantees two things a
    5-minute poller cannot live without: the HTTP pool is ALWAYS released, and an
    unforeseen DB/worker/reap fault can never escape as an unsanitized traceback
    (crash-loop-adjacent) — it degrades to a sanitized log + non-zero exit instead.
    """
    # A per-invocation correlation id lets an operator tie the sanitized failure log
    # to an external cron alert WITHOUT us ever having to log the provider's raw
    # (potentially secret-bearing) error text to make the failure findable. Minted
    # before ANY startup work so even a config-load failure can reference it.
    correlation_id = uuid4().hex

    # Declared before the try so the finally can release the pool even if the
    # constructor itself partial-allocates then raises (``None`` => nothing to close).
    publisher: LinkedInPublisher | None = None
    published = 0
    stuck = 0
    try:
        # Startup (logging + config/secret parsing) is INSIDE the boundary: a bad
        # env/secret must fail closed with a sanitized log, never escape as a raw
        # traceback before the handler is in scope.
        configure_logging()
        settings = get_settings()
        # Publish mode (api vs prefill) is the worker's terminal behaviour; log it
        # for operational visibility before we start driving drafts.
        logger.info(
            "vision-publisher invoked",
            extra={"env": settings.vision_env.value, "publish_mode": settings.publish_mode.value},
        )
        # Construct INSIDE the try/finally: a partial-alloc-then-raise (e.g. the HTTP
        # pool opened but a later init step throws) still reaches the finally, so the
        # connection pool can never leak (the resource-leak HIGH).
        publisher = LinkedInPublisher(settings)
        # One committing session wraps the whole poll (state transitions + audit
        # rows commit atomically via the worker's own guarded writes).
        with get_session() as session:
            now = datetime.now(timezone.utc)
            published = publisher.poll_and_publish(session, now)
            # Safety net: surface any draft stranded in ``queued`` past its lease so
            # an approved post is never silently lost (worker ISSUE 4 reaper/alert).
            stuck = publisher.reap_stuck(session, now)
    except Exception as exc:  # noqa: BLE001 — cron boundary: degrade, never crash-loop
        # Fail-closed (§22.9): log ONLY the exception class + correlation id. We
        # deliberately do NOT use ``logger.exception``/``exc_info`` here (unlike a
        # low-sensitivity job): a traceback or ``str(exc)`` can carry raw provider
        # text and must never reach the logs. ``get_session`` already rolled the
        # transaction back, so nothing is left half-applied.
        logger.error(
            "vision-publisher crashed unexpectedly (fail-closed)",
            extra={"error_type": exc.__class__.__name__, "correlation_id": correlation_id},
        )
        return 1
    finally:
        # Always release the LinkedIn HTTP connection pool, even on error / partial
        # construction. ``publisher`` is ``None`` only if construction itself raised
        # (nothing was allocated to release). A failing ``close()`` must itself fail
        # closed — never re-raise out of the cron boundary as an unsanitized
        # traceback (that would be its own crash-loop source).
        if publisher is not None:
            try:
                publisher.close()
            except Exception as close_exc:  # noqa: BLE001 — cleanup never crashes cron
                logger.error(
                    "vision-publisher cleanup failed (fail-closed)",
                    extra={
                        "error_type": close_exc.__class__.__name__,
                        "correlation_id": correlation_id,
                    },
                )
                return 1

    logger.info(
        "vision-publisher complete",
        extra={"published": published, "stuck_alerts": stuck},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
