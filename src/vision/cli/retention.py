"""``vision-retention`` entry point: weekly archive -> backup -> prune (BRD §22.9).

Thin CLI wiring over :func:`vision.ops.retention.run_retention`. Opens one
session, runs the fail-closed lifecycle (archive old data, back it up to Google
Drive via rclone, verify, then prune + VACUUM), and maps the outcome to a process
exit code so the scheduler can alert on failure. NEVER deletes data that was not
verified as backed up off-box.
"""

from __future__ import annotations

from datetime import datetime, timezone

from vision.config import get_settings
from vision.db.session import get_session
from vision.logging_setup import configure_logging, get_logger
from vision.ops.retention import run_retention

logger = get_logger("vision.cli.retention")


def main() -> int:
    """Run one retention pass. Exit 0 on a clean run (even a no-op), 1 on failure."""
    configure_logging()
    settings = get_settings()
    logger.info("vision-retention invoked", extra={"env": settings.vision_env.value})
    try:
        with get_session() as session:
            report = run_retention(session, settings, now=datetime.now(timezone.utc))
    except Exception:
        logger.exception("vision-retention crashed (fail-closed)")
        return 1

    logger.info(
        "vision-retention complete",
        extra={
            "rows": report.total_rows,
            "images": report.archived_images,
            "backed_up": report.backed_up,
            "pruned": report.pruned,
            "note": report.note,
        },
    )
    # A configured-but-failed backup is a real problem worth a non-zero exit so the
    # scheduler surfaces it; an unconfigured rclone (archived locally) is a clean
    # no-op the owner opted into until they run `rclone config`. Gate on ANY staged
    # artifact (rows, images, or the archive file) so an image-only run whose backup
    # failed still alerts (Codex review: gating on rows alone hid image-only failures).
    staged_something = bool(report.total_rows or report.archived_images or report.archive_path)
    if settings.rclone_remote.strip() and not report.backed_up and staged_something:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
