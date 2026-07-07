"""Atomic, portable per-job run lock (threat model §4 "prevent overlapping cron
runs with an atomic lock" + Hardening Checklist "concurrency lock").

WHY this module exists: the ``vision-daily`` cron mints an *approvable* draft with
live, single-use LinkedIn action links every time it runs. If two invocations
overlap — a slow run still working when the next cron tick fires, a manual re-run
racing the scheduled one, a systemd restart storm — each would independently mint
a fresh draft + fresh approval tokens, and the owner could approve BOTH, causing a
duplicate LinkedIn post (the exact "no double-post" invariant the threat model
guards). An in-process ``threading.Lock`` cannot help here: the racing runs are
*separate processes*. We therefore serialise on the one thing every process can
see atomically — the filesystem — using an ``O_CREAT | O_EXCL`` create, which is
atomic on both POSIX and Windows (so the identical guard works in SQLite-dev and a
Linux-prod deployment without a DB round-trip).

Design (mirrors the OAuth-refresh "losing writer fails fast" pattern in
``db/models.py``):
  * **Exclusive create** — ``os.open(..., O_CREAT | O_EXCL)`` succeeds for exactly
    one racer; every other racer gets ``FileExistsError`` and is told the lock is
    held. No read-then-write window, so two processes can never both "see it free".
  * **Per (job, date) key** — the lockfile name embeds the UTC date, so a lock is
    naturally scoped to one day's run and yesterday's leftover file (if any) can
    never block today.
  * **Stale-lock breaking (fail-open against deadlock, not against double-post)** —
    a crashed run could leave its lockfile forever, which would silently halt the
    daily job (a crash-loop of a different kind the threat model also forbids). If
    an existing lockfile is older than ``stale_after_secs`` we steal it exactly
    once. The window is chosen to comfortably exceed a healthy run, so we never
    steal a lock a live sibling still holds.

Nothing secret is ever written to the lockfile — only the owning PID and an
acquire timestamp, for operator debugging (never a token, never a secret).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from collections.abc import Iterator

log = logging.getLogger(__name__)

# Env var naming the directory lockfiles live in (config over code, BRD §22.6).
# Defaults under the system temp dir so a bare checkout works; prod points it at a
# durable, run-shared volume so overlapping *processes* observe the same lock.
_LOCK_DIR_ENV = "VISION_LOCK_DIR"

# A lockfile older than this is presumed abandoned by a crashed run and may be
# stolen. Generously larger than a healthy daily run (minutes) so a live sibling is
# never robbed of its lock; small enough that a genuine crash cannot wedge the cron
# for more than one cycle. Config-over-code via the constructor for tests.
_DEFAULT_STALE_AFTER_SECS = 2 * 60 * 60  # 2 hours


@dataclass(frozen=True)
class JobLock:
    """A held run lock — the open descriptor + its on-disk path.

    Immutable (frozen) per the project's immutability principle: once acquired, the
    identity of the lock a caller holds must not be mutated out from under the
    ``release`` call. Callers treat this as an opaque handle and only ever pass it
    back to :func:`release_job_lock`.
    """

    path: Path
    fd: int


def _lock_dir(configured: Path | None) -> Path:
    """Resolve the lock directory (explicit arg > env > temp default)."""
    if configured is not None:
        return configured
    from_env = os.environ.get(_LOCK_DIR_ENV)
    return Path(from_env) if from_env else Path(gettempdir()) / "vision" / "locks"


def date_key(now: datetime) -> str:
    """Return the UTC calendar-date key a day's lock is scoped to (``YYYY-MM-DD``).

    Always in UTC so the lock's day boundary is server-clock-stable and cannot be
    shifted by a process running in a different local timezone (the same UTC
    discipline the token expiry uses).
    """
    aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).date().isoformat()


def _lock_path(lock_dir: Path, job: str, now: datetime) -> Path:
    """Build the per-(job, date) lockfile path inside ``lock_dir``."""
    return lock_dir / f"{job}.{date_key(now)}.lock"


def _is_stale(path: Path, stale_after_secs: int, now_epoch: float) -> bool:
    """Return True if ``path`` is older than the stale window (crashed-run leftover).

    A missing file (it vanished between our failed create and this stat) is treated
    as NOT stale — the safe reading is "someone else is mid-acquire", so we do not
    barge in. Any stat error is likewise treated as not-stale (fail closed against
    stealing a lock we cannot prove is dead).
    """
    try:
        age = now_epoch - path.stat().st_mtime
    except (FileNotFoundError, OSError):
        return False
    return age > stale_after_secs


def acquire_job_lock(
    job: str,
    now: datetime,
    *,
    lock_dir: Path | None = None,
    stale_after_secs: int = _DEFAULT_STALE_AFTER_SECS,
) -> JobLock | None:
    """Atomically acquire the per-(job, date) lock, or ``None`` if already held.

    Returns a :class:`JobLock` the caller MUST hand to :func:`release_job_lock`
    (use :func:`job_lock` to guarantee release). Returns ``None`` when a live
    sibling already holds today's lock — the caller should then skip its work
    rather than mint an overlapping draft.

    The create is ``O_CREAT | O_EXCL`` so exactly one racer wins; a loser only
    steals the lock if it is provably stale (older than ``stale_after_secs``),
    which breaks a deadlock left by a crashed run without ever robbing a healthy
    one.
    """
    directory = _lock_dir(lock_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = _lock_path(directory, job, now)

    fd = _try_create(path)
    if fd is None:
        # Someone holds it. Break it ONLY if it is provably abandoned, then retry
        # the exclusive create exactly once so two stealers still can't both win.
        if _is_stale(path, stale_after_secs, datetime.now(timezone.utc).timestamp()):
            log.warning("stealing stale job lock %s (older than %ds)", path.name, stale_after_secs)
            try:
                path.unlink()
            except FileNotFoundError:
                # A sibling stole/released it first — fall through to one more try.
                pass
            except OSError as exc:
                log.warning("could not remove stale lock %s (%s)", path.name, exc.__class__.__name__)
                return None
            fd = _try_create(path)
        if fd is None:
            log.info("job lock %s already held; skipping overlapping run", path.name)
            return None

    # Record owning PID + acquire time for operator debugging — never a secret.
    try:
        os.write(fd, f"pid={os.getpid()} acquired_at={datetime.now(timezone.utc).isoformat()}\n".encode())
    except OSError:
        # A write failure does not invalidate the lock (the exclusive create is what
        # provides mutual exclusion); the metadata is best-effort only.
        pass
    return JobLock(path=path, fd=fd)


def _try_create(path: Path) -> int | None:
    """Attempt the atomic exclusive create; return the fd, or ``None`` if it exists."""
    try:
        # 0o600: only the owning user may read/write the lock (least privilege).
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    except OSError as exc:
        # An unexpected filesystem error must fail CLOSED for a run lock: we cannot
        # prove exclusivity, so we refuse to proceed rather than risk a double run.
        log.warning("could not create job lock %s (%s); treating as held", path.name, exc.__class__.__name__)
        return None


def release_job_lock(lock: JobLock) -> None:
    """Release a held lock: close the descriptor and remove the lockfile.

    Never raises — a release-time error (already-removed file, closed fd) must not
    turn a completed run into a crash. Best-effort cleanup is correct here because
    the stale-lock breaker is the backstop if a file is ever left behind.
    """
    try:
        os.close(lock.fd)
    except OSError:
        pass
    try:
        lock.path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove job lock %s (%s)", lock.path.name, exc.__class__.__name__)


@contextmanager
def job_lock(
    job: str,
    now: datetime,
    *,
    lock_dir: Path | None = None,
    stale_after_secs: int = _DEFAULT_STALE_AFTER_SECS,
) -> Iterator[JobLock | None]:
    """Context-managed lock: yields the :class:`JobLock` (or ``None`` if held).

    Guarantees release on every exit path, so a raising body can never leak the
    lockfile and wedge the next run::

        with job_lock("vision-daily", now) as lock:
            if lock is None:
                return  # a sibling is already running today
            ... do the run ...
    """
    lock = acquire_job_lock(job, now, lock_dir=lock_dir, stale_after_secs=stale_after_secs)
    try:
        yield lock
    finally:
        if lock is not None:
            release_job_lock(lock)
