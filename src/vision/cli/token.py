"""``vision-token`` entry point (cron, daily).

Refreshes LinkedIn tokens when within the refresh window and alerts the owner if
re-authorisation is required (§15.3). Wired stub for now; token lifecycle lands
in Phase 3.
"""

from __future__ import annotations

from vision.logging_setup import configure_logging, get_logger


def main() -> int:
    """Entry point for the ``vision-token`` console script."""
    configure_logging()
    logger = get_logger("vision.cli.token")
    logger.info("vision-token invoked")
    logger.info("token refresh not yet implemented (scaffold) — exiting cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
