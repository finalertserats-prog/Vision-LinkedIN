"""Data lifecycle: archive -> verified Google Drive backup -> prune + VACUUM.

The engine accumulates news ``items``, terminal ``drafts`` (with large council
provenance blobs), ``runs``, ``audit_log`` and generated images forever. This
module keeps the local footprint small WITHOUT losing history:

  1. ARCHIVE every row/image older than the retention window to a compressed
     JSON bundle + a clean SQLite snapshot in the archive dir.
  2. BACK UP that archive to Google Drive via rclone and VERIFY it landed.
  3. PRUNE the archived rows/images locally and VACUUM to reclaim disk.

Fail-closed ordering is the whole point (BRD §22.9): a row is deleted ONLY after
its backup is verified off-box. If rclone is not configured, or the upload/verify
fails, the local archive is kept and the prune is SKIPPED — the job never destroys
data it could not prove is safe elsewhere.

NEVER pruned: ``own_posts`` (dedup memory), ``oauth_tokens`` (live credentials),
``sources``, ``alert_state`` — these are small and load-bearing.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from vision.config import Settings, get_settings
from vision.db.models import AuditLog, Draft, Item, Run, UsedToken

logger = logging.getLogger("vision.ops.retention")

# Draft states safe to archive once old: the post is either live (its URN + the
# own_posts dedup row persist) or it was discarded. Never archive an in-flight
# draft regardless of age.
_TERMINAL_DRAFT_STATES = frozenset({"published", "rejected", "expired"})


@dataclass(frozen=True)
class _TableSpec:
    """One archivable table: how to select its old rows and delete them."""

    name: str
    model: type
    time_col: str
    terminal_states: frozenset[str] | None = None  # drafts only: gate on state


_SPECS: tuple[_TableSpec, ...] = (
    _TableSpec("items", Item, "created_at"),
    _TableSpec("runs", Run, "created_at"),
    _TableSpec("drafts", Draft, "created_at", _TERMINAL_DRAFT_STATES),
    _TableSpec("audit_log", AuditLog, "created_at"),
    _TableSpec("used_tokens", UsedToken, "used_at"),
)


@dataclass
class RetentionReport:
    """Outcome of one retention run (for the CLI exit code + audit)."""

    enabled: bool = True
    cutoff: datetime | None = None
    archived_rows: dict[str, int] = field(default_factory=dict)
    archived_images: int = 0
    archive_path: str | None = None
    backed_up: bool = False
    pruned: bool = False
    bytes_archived: int = 0
    note: str = ""

    @property
    def total_rows(self) -> int:
        return sum(self.archived_rows.values())


class RcloneUploader:
    """Backs an archive up to Google Drive via rclone and verifies it landed.

    Injectable ``runner`` (defaults to ``subprocess.run``) so tests never shell
    out. ``configured()`` is False until the owner sets ``RCLONE_REMOTE`` after a
    one-time ``rclone config`` OAuth — until then the caller keeps the local
    archive and skips the prune.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._run = runner or self._default_run

    @staticmethod
    def _default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - args are built from config, never user input
            args, capture_output=True, text=True, timeout=600, check=False
        )

    def configured(self) -> bool:
        return bool(self._settings.rclone_remote.strip())

    def _dest(self) -> str:
        remote = self._settings.rclone_remote.strip()
        path = self._settings.rclone_drive_path.strip("/")
        return f"{remote}:{path}"

    def upload(self, local_path: Path) -> bool:
        """Copy ``local_path`` to Drive and VERIFY with ``rclone check``.

        Returns True only when BOTH the copy and the post-copy hash check exit 0,
        so the caller can safely prune. Any non-zero exit / launch error -> False
        (fail-closed: keep the local copy, skip the prune).
        """
        bin_ = self._settings.rclone_bin
        dest = self._dest()
        try:
            cp = self._run([bin_, "copy", str(local_path), dest])
            if cp.returncode != 0:
                logger.error("rclone copy failed (rc=%s): %s", cp.returncode, cp.stderr[:200])
                return False
            # Verify: rclone check compares the single file against the remote by
            # hash. --one-way keeps it to "is our file there and identical".
            chk = self._run(
                [bin_, "check", str(local_path), dest, "--one-way", "--include", local_path.name]
            )
            if chk.returncode != 0:
                logger.error("rclone verify failed (rc=%s): %s", chk.returncode, chk.stderr[:200])
                return False
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("rclone unavailable: %s", type(exc).__name__)
            return False


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Serialize an ORM row to JSON-safe primitives (datetimes -> ISO strings)."""
    out: dict[str, Any] = {}
    for col in sa_inspect(row.__class__).columns:
        val = getattr(row, col.key)
        if isinstance(val, datetime):
            out[col.key] = val.isoformat()
        elif isinstance(val, (bytes, bytearray)):
            out[col.key] = f"<{len(val)} bytes omitted>"
        else:
            out[col.key] = val if _json_safe(val) else str(val)
    return out


def _json_safe(val: Any) -> bool:
    return val is None or isinstance(val, (str, int, float, bool))


def _old_images(image_dir: Path, cutoff: datetime, keep: set[str]) -> list[Path]:
    """PNG files older than ``cutoff`` and not referenced by a surviving draft."""
    if not image_dir.is_dir():
        return []
    cutoff_ts = cutoff.timestamp()
    old: list[Path] = []
    for png in image_dir.glob("*.png"):
        if str(png) in keep or png.name in keep:
            continue
        try:
            if png.stat().st_mtime < cutoff_ts:
                old.append(png)
        except OSError:
            continue
    return old


def run_retention(
    session: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    uploader: RcloneUploader | None = None,
    archive_dir: Path | None = None,
    image_dir: Path | None = None,
) -> RetentionReport:
    """Archive old data, back it up (verified), then prune + VACUUM (fail-closed)."""
    settings = settings or get_settings()
    if not settings.retention_enabled:
        return RetentionReport(enabled=False, note="retention disabled")

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings.retention_days)
    uploader = uploader or RcloneUploader(settings)
    archive_dir = archive_dir or Path(settings.retention_archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    report = RetentionReport(cutoff=cutoff)

    # 1) ARCHIVE — gather old rows per table (drafts also gated on terminal state).
    bundle: dict[str, list[dict[str, Any]]] = {}
    to_delete: dict[str, list[Any]] = {}
    for spec in _SPECS:
        col = getattr(spec.model, spec.time_col)
        stmt = select(spec.model).where(col < cutoff)
        if spec.terminal_states is not None:
            stmt = stmt.where(spec.model.state.in_(spec.terminal_states))
        rows = list(session.execute(stmt).scalars().all())
        if rows:
            bundle[spec.name] = [_row_to_dict(r) for r in rows]
            to_delete[spec.name] = rows
            report.archived_rows[spec.name] = len(rows)

    # Old images not referenced by any surviving (kept) draft.
    kept_image_paths = {
        p for (p,) in session.execute(select(Draft.image_path).where(Draft.image_path.isnot(None)))
    }
    kept_image_paths.discard(None)
    old_images = _old_images(
        image_dir or Path("prep"), cutoff, {str(p) for p in kept_image_paths}
    )
    report.archived_images = len(old_images)

    if not bundle and not old_images:
        report.note = "nothing older than cutoff"
        return report

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"vision-archive-{stamp}.json.gz"
    payload = json.dumps({"cutoff": cutoff.isoformat(), "tables": bundle}, ensure_ascii=False)
    archive_path.write_bytes(gzip.compress(payload.encode("utf-8")))
    report.archive_path = str(archive_path)
    report.bytes_archived = archive_path.stat().st_size

    # A consistent full-DB snapshot (agy's review): VACUUM INTO reads a coherent
    # view without WAL bloat, giving a complete recovery point beyond the extracted
    # rows. Best-effort — a missing snapshot never blocks the archive/backup.
    staged: list[Path] = [archive_path]
    snapshot = archive_dir / f"vision-db-{stamp}.db"
    if _snapshot_db(session, snapshot):
        staged.append(snapshot)

    # Copy old images alongside the bundle so the backup is self-contained.
    img_archive = archive_dir / f"images-{stamp}"
    if old_images:
        img_archive.mkdir(exist_ok=True)
        for png in old_images:
            shutil.copy2(png, img_archive / png.name)
        staged.append(img_archive)

    # 2) BACK UP + VERIFY every staged artifact. If rclone is unconfigured or ANY
    # upload/verify fails, keep the local archive and DO NOT prune (fail-closed —
    # never delete data we could not prove is safe off-box).
    if not uploader.configured():
        report.note = "rclone not configured; archived locally, prune skipped"
        logger.warning("Retention: %s", report.note)
        return report
    report.backed_up = all(uploader.upload(p) for p in staged)
    if not report.backed_up:
        report.note = "backup upload/verify failed; prune skipped (fail-closed)"
        logger.error("Retention: %s", report.note)
        return report

    # 3) PRUNE — only now, after a verified off-box backup.
    for spec in _SPECS:
        for row in to_delete.get(spec.name, []):
            session.delete(row)
    session.commit()
    for png in old_images:
        try:
            png.unlink()
        except OSError:
            logger.warning("could not delete archived image %s", png.name)
    _vacuum(session)
    report.pruned = True
    report.note = f"archived + backed up + pruned {report.total_rows} rows, {report.archived_images} images"
    logger.info("Retention complete: %s", report.note)
    return report


def _snapshot_db(session: Session, dest: Path) -> bool:
    """Write a consistent SQLite snapshot via ``VACUUM INTO`` (agy's review).

    VACUUM INTO reads a coherent view into a fresh file without the WAL-bloat /
    exclusive-lock hazards of an in-place VACUUM. No-op (returns False) for
    non-SQLite backends. Best-effort: a failure never aborts the retention run.
    """
    try:
        if not str(session.get_bind().url).startswith("sqlite"):
            return False
        conn = session.connection().engine.raw_connection()
        try:
            # dest is our own config-derived path; escape quotes defensively since
            # VACUUM INTO takes a string literal, not a bind parameter.
            safe = str(dest).replace("'", "''")
            conn.execute(f"VACUUM INTO '{safe}'")
            conn.commit()
        finally:
            conn.close()
        return dest.exists()
    except Exception as exc:  # noqa: BLE001 - snapshot is best-effort, not correctness
        logger.warning("db snapshot skipped: %s", type(exc).__name__)
        return False


def _vacuum(session: Session) -> None:
    """Reclaim freed pages (SQLite VACUUM). Best-effort: never fail the run."""
    try:
        # VACUUM cannot run inside a transaction; use a fresh autocommit connection.
        conn = session.connection().engine.raw_connection()
        try:
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - VACUUM is an optimization, not correctness
        logger.warning("VACUUM skipped: %s", type(exc).__name__)
