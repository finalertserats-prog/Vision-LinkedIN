"""``vision-daily`` entry point (cron-triggered, ~06:30 IST).

Runs Ingest -> Curate -> Synthesise -> Quality -> Email and writes a draft
record (§10.2). This is a wired stub: it configures logging and reports intent
so the console script installs and runs; the pipeline is implemented in Phase 1.
"""

from __future__ import annotations

from vision.config import get_settings
from vision.logging_setup import configure_logging, get_logger


def main() -> int:
    """Entry point for the ``vision-daily`` console script.

    Returns a process exit code (0 = success) so cron can detect failures.
    """
    # Structured logging first so even the stub emits observable, correlated logs.
    configure_logging()
    logger = get_logger("vision.cli.daily")
    settings = get_settings()
    # Report the active mode: the daily job's side effects (email/post) depend on
    # VISION_ENV, so surfacing it up front aids debugging of cron runs.
    logger.info("vision-daily invoked", extra={"env": settings.vision_env.value})
    logger.info("daily pipeline not yet implemented (scaffold) — exiting cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
