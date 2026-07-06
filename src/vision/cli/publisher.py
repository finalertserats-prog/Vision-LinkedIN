"""``vision-publisher`` entry point (poller, every ~5 min).

Publishes any ``approved && due`` drafts via the LinkedIn API and emails a
confirmation (§10.2). Wired stub for now; publishing logic lands in Phase 3.
"""

from __future__ import annotations

from vision.config import get_settings
from vision.logging_setup import configure_logging, get_logger


def main() -> int:
    """Entry point for the ``vision-publisher`` console script."""
    configure_logging()
    logger = get_logger("vision.cli.publisher")
    settings = get_settings()
    # Publish mode (api vs prefill) determines the terminal behaviour of the
    # worker, so it is logged for operational visibility.
    logger.info("vision-publisher invoked", extra={"publish_mode": settings.publish_mode.value})
    logger.info("publisher not yet implemented (scaffold) — exiting cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
