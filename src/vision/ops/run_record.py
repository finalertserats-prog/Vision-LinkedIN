"""Run-recording helpers for Project VISION observability (BRD §17, NFR-08).

WHY this module exists: §11.3 gives every daily execution a ``runs`` row, and §17
requires it to carry a structured ``stats`` blob — counts, timings, token usage
and model versions — correlated by ``run_id``. This module turns that into a tiny,
correct-by-construction API:

  * :func:`open_run` / :func:`close_run` — open a ``runs`` row (status
    ``running``) and later close it with a terminal status (``ok`` | ``partial``
    | ``failed``) and its accumulated ``stats``.
  * :class:`RunStats` — a small accumulator the pipeline fills as it works.
  * :func:`record_run` — a context manager that ties it together: it opens the
    row, binds the ``run_id`` into the logging context (so every downstream log
    line is correlated), yields a handle, and — on exit — closes the row with
    ``ok``/``partial`` on success or ``failed`` if the block raised.

CRASH-SAFETY (WHY its own sessions): a run that raises must still be RECORDED as
``failed``. The pipeline's own unit of work rolls back on error (see
``db.session.get_session``), so if the failure status were written on that same
session it would be rolled back with everything else — the failure would vanish.
:func:`record_run` therefore performs its open/close writes on independent
sessions obtained from the injected ``session_factory``; the failed-run record is
committed on a fresh transaction that the pipeline's rollback cannot erase.

The recorded ``stats`` NEVER contains a secret — only non-secret operational
metrics (counts/timings/aggregate token totals/model version strings).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy.orm import Session

from vision.db.models import Run
from vision.db.session import get_session
from vision.logging_setup import set_run_id

_log = logging.getLogger(__name__)

# Run status vocabulary. ``running`` is the transient state an open row carries
# so a crashed/never-closed run is distinguishable from a completed one — a key
# observability signal. The three terminal states match §11.3.
RUN_STATUS_RUNNING: Final[str] = "running"
RUN_STATUS_OK: Final[str] = "ok"
RUN_STATUS_PARTIAL: Final[str] = "partial"
RUN_STATUS_FAILED: Final[str] = "failed"

# Same session-factory contract used across the app (approval.web, ops.health):
# a zero-arg callable returning a context manager that yields a commit-on-success
# Session. Prod passes ``get_session``; tests inject an in-memory-backed factory.
SessionFactory = Callable[[], AbstractContextManager[Session]]

# A monotonic clock is injected so elapsed-time recording is deterministic in
# tests. ``time.monotonic`` (not wall clock) is used for durations because it is
# immune to system-clock adjustments during a long run.
MonotonicClock = Callable[[], float]


@dataclass
class RunStats:
    """Mutable accumulator for a run's structured metrics (§17).

    WHY mutable (against the default immutability rule): this is a *builder* — the
    pipeline incrementally records counts/timings/usage as each stage completes,
    which is inherently stateful. The mutation is confined to this object; the
    value persisted to the DB is a FRESH dict produced by :meth:`as_dict`, so no
    shared mutable state ever leaks into the stored row.
    """

    counts: dict[str, int] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    token_usage: dict[str, int] = field(default_factory=dict)
    model_versions: dict[str, str] = field(default_factory=dict)
    # Set True when a stage degraded but the run still produced output — this maps
    # the terminal status to ``partial`` instead of ``ok``.
    _degraded: bool = False

    def incr(self, name: str, by: int = 1) -> None:
        """Increment a named counter (e.g. items ingested, drafts synthesised)."""
        self.counts[name] = self.counts.get(name, 0) + by

    def set_count(self, name: str, value: int) -> None:
        """Set a named counter to an absolute value."""
        self.counts[name] = value

    def record_timing(self, name: str, seconds: float) -> None:
        """Record a named stage duration in seconds."""
        self.timings[name] = seconds

    def record_token_usage(self, model: str, tokens: int) -> None:
        """Add to the aggregate token count attributed to a model (non-secret)."""
        self.token_usage[model] = self.token_usage.get(model, 0) + tokens

    def record_model_version(self, pass_name: str, version: str) -> None:
        """Record which model/version served a synthesis pass (generate/critique/…)."""
        self.model_versions[pass_name] = version

    def mark_partial(self) -> None:
        """Flag the run as degraded so it closes as ``partial`` rather than ``ok``."""
        self._degraded = True

    def final_status(self) -> str:
        """Return the terminal status for a *successful* block (ok | partial)."""
        return RUN_STATUS_PARTIAL if self._degraded else RUN_STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        """Return a FRESH, JSON-serialisable snapshot for the ``runs.stats`` column."""
        return {
            "counts": dict(self.counts),
            "timings": dict(self.timings),
            "token_usage": dict(self.token_usage),
            "model_versions": dict(self.model_versions),
        }


@dataclass(frozen=True)
class RunHandle:
    """What :func:`record_run` yields: the run id + the live stats accumulator.

    Frozen so the handle itself cannot be swapped out, while ``stats`` (a builder)
    stays writable for the duration of the run.
    """

    run_id: uuid.UUID
    stats: RunStats


def open_run(session: Session, *, notes: str | None = None) -> Run:
    """Insert and flush a new ``runs`` row in the transient ``running`` state.

    Flushing assigns the Python-side UUID PK immediately so callers (and the
    items/drafts that FK to it) can reference ``run.id`` at once. The row is
    committed by the surrounding session context.
    """
    run = Run(status=RUN_STATUS_RUNNING, notes=notes)
    session.add(run)
    session.flush()  # assign the UUID PK now so run_id is usable immediately
    return run


def close_run(
    session: Session,
    run: Run,
    *,
    status: str,
    stats: dict[str, Any] | None = None,
    notes: str | None = None,
) -> Run:
    """Close a ``runs`` row with a terminal status and its accumulated stats.

    Assigns the terminal ``status`` and (optionally) the ``stats`` blob / notes
    together, then flushes; the surrounding session context commits them
    atomically. ``updated_at`` is bumped automatically by the timestamp mixin.
    """
    run.status = status
    if stats is not None:
        # Store a shallow copy so a later mutation of the caller's dict cannot
        # retroactively change the persisted value (immutability at the boundary).
        run.stats = dict(stats)
    if notes is not None:
        run.notes = notes
    session.flush()
    return run


def _finalize(
    session_factory: SessionFactory,
    run_id: uuid.UUID,
    status: str,
    stats: RunStats,
) -> None:
    """Close the run row on a FRESH session so the record survives a rollback.

    Opening an independent session (rather than reusing the pipeline's, which may
    be rolling back) is exactly what makes a ``failed`` outcome durable. A failure
    to finalise is logged but never re-raised over the original error.
    """
    try:
        with session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                # The row vanished (e.g. a test tore down the DB) — nothing to
                # close. Log and move on; there is no correct write to make.
                _log.error("run %s missing at finalize; cannot record status", run_id)
                return
            close_run(session, run, status=status, stats=stats.as_dict())
    except Exception:  # noqa: BLE001 — finalize must not mask the original error
        _log.exception("failed to finalize run %s with status %s", run_id, status)


@contextmanager
def record_run(
    session_factory: SessionFactory = get_session,
    *,
    notes: str | None = None,
    clock: MonotonicClock = time.monotonic,
) -> Iterator[RunHandle]:
    """Record one pipeline execution as a ``runs`` row, correlated by ``run_id``.

    On entry: opens (and commits) the row in state ``running`` and binds its id
    into the logging context so every downstream log line is correlated (§17). It
    yields a :class:`RunHandle`; the caller fills ``handle.stats`` as work
    proceeds. On exit:

      * clean completion → close as ``ok`` (or ``partial`` if
        ``stats.mark_partial()`` was called), with a ``total_seconds`` timing;
      * an exception → close as ``failed`` (still recording elapsed time) on an
        INDEPENDENT session, then re-raise so the caller still sees the error.

    ``session_factory`` and ``clock`` are injected for hermetic tests; production
    calls ``record_run()`` and gets the real, config-driven session + monotonic
    clock.
    """
    # Open + commit the run row first so run_id is durable before any work runs.
    with session_factory() as session:
        run = open_run(session, notes=notes)
        run_id = run.id
    # Correlate all subsequent logs (across ingest/curate/synthesise/…) to this run.
    set_run_id(str(run_id))
    stats = RunStats()
    started = clock()
    _log.info("run started")

    try:
        yield RunHandle(run_id=run_id, stats=stats)
    except BaseException:
        # Record the failure on a fresh session so the pipeline's own rollback
        # cannot erase the ``failed`` status, then propagate the original error.
        stats.record_timing("total_seconds", clock() - started)
        _finalize(session_factory, run_id, RUN_STATUS_FAILED, stats)
        _log.error("run failed")
        raise
    else:
        stats.record_timing("total_seconds", clock() - started)
        final_status = stats.final_status()
        _finalize(session_factory, run_id, final_status, stats)
        _log.info("run complete: %s", final_status)
