"""Nightly database backup for Project VISION (BRD §10.3 / §19).

WHY this module exists: the daily loop's only durable state — drafts, encrypted
OAuth tokens, audit rows — lives in the ``vision`` Postgres schema. A nightly
dump (retained 14 days) means an approved-but-unpublished draft, or a token,
survives a VPS rebuild or disk loss. Adapted from finalert's ``backup.py``
(pg_dump + retention), narrowed to a single schema, with a SQLite file-copy
fallback so the same script "just works" in the dev default (``sqlite:///``).

Security (threat model §3, §22 secrets discipline):
    * The DB password is NEVER placed on the command line or logged. pg_dump
      reads it from the ``PGPASSWORD`` env var of the CHILD process only.
    * Connection details are logged host/db/schema-only — never the password.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from vision.config import Settings, get_settings
from vision.logging_setup import configure_logging, get_logger

# Module logger (structured JSON + secret redaction via logging_setup).
log = get_logger("vision.backup")

# One dump per night → 14 files == 14 days of history (BRD §10.3 retention).
RETENTION_COUNT = 14
# The single application schema we dump (BRD §11). Config-over-code: overridable
# via env for anyone running a differently-named schema.
PG_SCHEMA = os.environ.get("VISION_PG_SCHEMA", "vision")
# Where dumps land. A dedicated dir keeps pruning's glob unambiguous.
DEFAULT_BACKUP_DIR = Path(os.environ.get("VISION_BACKUP_DIR", "/opt/vision/backups"))
# Common filename stem so both engines share one retention glob.
_BACKUP_STEM = "vision_backup"
# Hard ceiling so a wedged dump can never hang the nightly cron indefinitely.
_PG_DUMP_TIMEOUT_S = 600


def _timestamp() -> str:
    """Return a UTC, filesystem-safe timestamp for a backup filename."""
    # UTC keeps filenames monotonically sortable regardless of host tz.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_sqlite(database: str, backup_dir: Path, stamp: str) -> Path:
    """Copy a SQLite database file to the backup dir (dev fallback).

    ``database`` is the path component of a ``sqlite:///`` URL. A plain file copy
    is a valid, consistent snapshot for the dev DB (no concurrent writers).
    """
    # Resolve relative to the current working dir just like SQLAlchemy would.
    src = Path(database).expanduser()
    if not src.exists():
        # Fail-closed: a missing source is an error, not a silent empty backup.
        raise FileNotFoundError(f"SQLite database not found: {src}")
    dest = backup_dir / f"{_BACKUP_STEM}_{stamp}.sqlite"
    # copy2 preserves mtime/metadata; import locally to keep top-level imports lean.
    import shutil

    shutil.copy2(src, dest)
    log.info("sqlite backup written", extra={"file": dest.name})
    return dest


def _backup_postgres(url, backup_dir: Path, stamp: str) -> Path:  # type: ignore[no-untyped-def]
    """Dump a single Postgres schema via ``pg_dump`` (custom format).

    The password is passed ONLY through the child's ``PGPASSWORD`` env var, never
    argv (threat model §3 — "never place tokens in CLI arguments").
    """
    dest = backup_dir / f"{_BACKUP_STEM}_{stamp}.dump"
    # Custom format (-Fc) is compressed and restorable with pg_restore selectively.
    cmd = [
        "pg_dump",
        "--format=custom",
        "--schema",
        PG_SCHEMA,
        "--no-owner",  # portable restore into a differently-owned target
        "--no-privileges",
        "--host",
        url.host or "localhost",
        "--port",
        str(url.port or 5432),
        "--username",
        url.username or "postgres",
        "--dbname",
        url.database or "vision",
        "--file",
        str(dest),
    ]
    # Copy the parent env and inject the password out-of-band. url.password is the
    # real secret; it appears here in the child env only, and is never logged.
    child_env = {**os.environ, "PGPASSWORD": url.password or ""}
    # Log the connection WITHOUT the password (host/db/schema are not secret).
    log.info(
        "starting pg_dump",
        extra={"host": url.host, "db": url.database, "schema": PG_SCHEMA},
    )
    # check=True raises CalledProcessError on non-zero; capture stderr for the log.
    # pg_dump never echoes the password, so its stderr is safe to record.
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
        cmd,
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=_PG_DUMP_TIMEOUT_S,
    )
    if result.stderr:
        log.debug("pg_dump stderr: %s", result.stderr.strip())
    log.info("postgres backup written", extra={"file": dest.name})
    return dest


def run_backup(
    *, settings: Settings | None = None, backup_dir: Path = DEFAULT_BACKUP_DIR
) -> Path:
    """Create one backup of the configured database and return its path.

    Dispatches on the ``DATABASE_URL`` driver: Postgres → pg_dump, SQLite → file
    copy. Raises on any failure (fail-closed) so the caller can exit non-zero.
    """
    cfg = settings or get_settings()
    # make_url safely parses the SQLAlchemy URL into typed components.
    url = make_url(cfg.database_url)
    # mkdir here so both engines share the guarantee the dir exists.
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()

    # get_backend_name() is "sqlite"/"postgresql"/... regardless of any +driver.
    backend = url.get_backend_name()
    if backend == "sqlite":
        return _backup_sqlite(url.database or "vision.db", backup_dir, stamp)
    if backend == "postgresql":
        return _backup_postgres(url, backup_dir, stamp)
    # Anything else is unsupported — fail loudly rather than pretend to back up.
    raise ValueError(f"unsupported database backend for backup: {backend!r}")


def prune_old_backups(
    backup_dir: Path = DEFAULT_BACKUP_DIR, keep: int = RETENTION_COUNT
) -> list[Path]:
    """Delete all but the newest ``keep`` backups; return the deleted paths.

    Retention is by count (newest-``keep`` wins) which, at one dump per night,
    equals ``keep`` days of history — deterministic and trivially testable.
    """
    if not backup_dir.exists():
        return []
    # Newest last, so [:-keep] is exactly the stale head of the list.
    backups = sorted(
        backup_dir.glob(f"{_BACKUP_STEM}_*"), key=lambda p: p.stat().st_mtime
    )
    to_delete = backups[:-keep] if len(backups) > keep else []
    for old in to_delete:
        # Specific, not bare: only ignore a concurrent delete of this same file.
        try:
            old.unlink()
        except FileNotFoundError:
            continue
        log.info("pruned old backup", extra={"file": old.name})
    return to_delete


def main() -> int:
    """Console entry point: back up + prune, returning a cron-friendly exit code.

    Returns 0 on success, 1 on any failure (fail-closed) so the nightly cron /
    systemd timer surfaces problems to the operator instead of failing silently.
    """
    configure_logging()
    try:
        path = run_backup()
    # Specific exceptions only (no bare except, §22): a missing pg_dump binary,
    # a failed dump, a missing SQLite file, an unsupported backend, or an I/O error.
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
        OSError,
    ):
        log.exception("backup failed (fail-closed) — no valid backup produced")
        return 1

    deleted = prune_old_backups(path.parent)
    log.info(
        "backup complete",
        extra={"file": path.name, "pruned": len(deleted), "retained": RETENTION_COUNT},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
