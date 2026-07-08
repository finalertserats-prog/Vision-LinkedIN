"""Data-lifecycle retention tests — the fail-closed guarantees around deletion.

These tests exist because retention DELETES data: the load-bearing invariants are
(1) nothing is pruned without a verified backup, (2) fresh + in-flight + dedup
rows survive, (3) an unconfigured/failed backup keeps everything.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vision.config import Settings
from vision.db.models import Draft, Item, OwnPost, Run, Source
from vision.ops.retention import RcloneUploader, run_retention

_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
_OLD = _NOW - timedelta(days=45)   # older than the 30d window
_FRESH = _NOW - timedelta(days=5)  # inside the window


class _FakeUploader:
    """Stands in for RcloneUploader: records uploads, returns a canned verdict."""

    def __init__(self, *, configured: bool = True, ok: bool = True) -> None:
        self._configured = configured
        self._ok = ok
        self.uploaded: list[str] = []

    def configured(self) -> bool:
        return self._configured

    def upload(self, path: Path) -> bool:
        self.uploaded.append(path.name)
        return self._ok


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "RETENTION_ENABLED": True,
        "RETENTION_DAYS": 30,
        "RETENTION_ARCHIVE_DIR": str(tmp_path / "archive"),
        "RCLONE_REMOTE": "gdrive",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _seed(session: Session) -> None:
    """One old + one fresh item, an old terminal + old in-flight + fresh draft,
    and an own_posts row (must never be deleted)."""
    run = Run(status="ok", created_at=_OLD)
    session.add(run)
    session.flush()
    src = Source(name="s", lane="ai", kind="rss", url="http://x#f", authority_weight=1.0, enabled=True)
    session.add(src)
    session.flush()
    session.add(Item(source_id=src.id, run_id=run.id, lane="ai", title="old", url="http://x/1",
                     summary="s", created_at=_OLD, published_at=_OLD))
    session.add(Item(source_id=src.id, run_id=run.id, lane="ai", title="new", url="http://x/2",
                     summary="s", created_at=_FRESH, published_at=_FRESH))
    session.add(Draft(state="published", post_text="old published", created_at=_OLD,
                      post_urn="urn:li:share:1"))
    session.add(Draft(state="pending_approval", post_text="old in-flight", created_at=_OLD))
    session.add(Draft(state="published", post_text="fresh published", created_at=_FRESH))
    session.add(OwnPost(post_urn="urn:li:share:1", created_at=_OLD, published_at=_OLD))
    session.commit()


def _count(session: Session, model: type) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_prunes_old_rows_only_after_verified_backup(db_session: Session, tmp_path: Path) -> None:
    _seed(db_session)
    up = _FakeUploader(configured=True, ok=True)

    report = run_retention(db_session, _settings(tmp_path), now=_NOW, uploader=up,
                           archive_dir=tmp_path / "archive", image_dir=tmp_path / "noimg")

    assert report.backed_up is True and report.pruned is True
    assert up.uploaded, "the archive must have been uploaded before pruning"
    # Old item + old terminal draft gone; fresh item, fresh draft, and the
    # old IN-FLIGHT draft survive (never archive a non-terminal draft).
    assert _count(db_session, Item) == 1
    assert _count(db_session, Draft) == 2
    remaining = {d.post_text for d in db_session.execute(select(Draft)).scalars()}
    assert remaining == {"old in-flight", "fresh published"}
    # Dedup memory is sacrosanct.
    assert _count(db_session, OwnPost) == 1
    # The compressed archive was actually written.
    assert Path(report.archive_path).exists()


def test_fail_closed_when_backup_upload_fails(db_session: Session, tmp_path: Path) -> None:
    _seed(db_session)
    up = _FakeUploader(configured=True, ok=False)  # upload/verify fails

    report = run_retention(db_session, _settings(tmp_path), now=_NOW, uploader=up,
                           archive_dir=tmp_path / "archive", image_dir=tmp_path / "noimg")

    assert report.backed_up is False and report.pruned is False
    assert "fail-closed" in report.note
    # NOTHING was deleted — every seeded row still present.
    assert _count(db_session, Item) == 2
    assert _count(db_session, Draft) == 3
    assert _count(db_session, OwnPost) == 1


def test_skips_prune_when_rclone_unconfigured(db_session: Session, tmp_path: Path) -> None:
    _seed(db_session)
    up = _FakeUploader(configured=False)

    report = run_retention(db_session, _settings(tmp_path, RCLONE_REMOTE=""), now=_NOW,
                           uploader=up, archive_dir=tmp_path / "archive",
                           image_dir=tmp_path / "noimg")

    assert report.pruned is False
    assert "not configured" in report.note
    assert _count(db_session, Item) == 2  # kept locally, un-pruned
    assert Path(report.archive_path).exists()  # but still archived


def test_disabled_is_a_noop(db_session: Session, tmp_path: Path) -> None:
    _seed(db_session)
    report = run_retention(db_session, _settings(tmp_path, RETENTION_ENABLED=False), now=_NOW,
                           uploader=_FakeUploader(), archive_dir=tmp_path / "archive",
                           image_dir=tmp_path / "noimg")
    assert report.enabled is False
    assert _count(db_session, Item) == 2


def test_rclone_uploader_requires_copy_and_verify_to_pass(tmp_path: Path) -> None:
    f = tmp_path / "a.gz"
    f.write_bytes(b"x")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        # copy (index 1 == 'copy') succeeds; check succeeds -> overall True.
        return subprocess.CompletedProcess(args, 0, "", "")

    up = RcloneUploader(_settings(tmp_path), runner=runner)
    assert up.configured() is True
    assert up.upload(f) is True
    assert [c[1] for c in calls] == ["copy", "check"]  # verify always follows copy


def _old_png(image_dir: Path, name: str) -> Path:
    image_dir.mkdir(parents=True, exist_ok=True)
    png = image_dir / name
    png.write_bytes(b"\x89PNG\r\n\x1a\n stub")
    os.utime(png, (_OLD.timestamp(), _OLD.timestamp()))  # backdate mtime
    return png


def test_image_of_pruned_draft_is_archived_and_removed(db_session: Session, tmp_path: Path) -> None:
    # Codex regression: an image referenced ONLY by an old terminal draft (which is
    # itself being pruned) must be archived + removed, not leaked forever.
    _seed(db_session)
    imgdir = tmp_path / "img"
    png = _old_png(imgdir, "old.png")
    d = db_session.execute(select(Draft).where(Draft.post_text == "old published")).scalar_one()
    d.image_path = str(png)
    db_session.commit()
    up = _FakeUploader(ok=True)

    report = run_retention(db_session, _settings(tmp_path), now=_NOW, uploader=up,
                           archive_dir=tmp_path / "archive", image_dir=imgdir)

    assert report.archived_images == 1
    assert report.pruned is True
    assert not png.exists()  # removed only after the verified backup
    assert any(n.startswith("images-") and n.endswith(".zip") for n in up.uploaded)


def test_image_only_run_is_not_pruned_when_backup_fails(db_session: Session, tmp_path: Path) -> None:
    # An image-only run whose backup fails must keep the image (fail-closed) — the
    # exact case Codex flagged as silently succeeding.
    imgdir = tmp_path / "img"
    png = _old_png(imgdir, "orphan.png")  # old, referenced by no draft
    up = _FakeUploader(ok=False)

    report = run_retention(db_session, _settings(tmp_path), now=_NOW, uploader=up,
                           archive_dir=tmp_path / "archive", image_dir=imgdir)

    assert report.archived_images == 1
    assert report.backed_up is False and report.pruned is False
    assert png.exists()  # never deleted without a verified backup


def test_retention_days_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, RETENTION_DAYS=0)


def test_rclone_uploader_fails_when_verify_fails(tmp_path: Path) -> None:
    f = tmp_path / "a.gz"
    f.write_bytes(b"x")

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        rc = 0 if args[1] == "copy" else 1  # copy ok, verify FAILS
        return subprocess.CompletedProcess(args, rc, "", "mismatch")

    up = RcloneUploader(_settings(tmp_path), runner=runner)
    assert up.upload(f) is False  # a failed verify must not report success
