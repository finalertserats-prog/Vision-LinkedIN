"""Feed-health monitoring for Project VISION (BRD §17, §11.1, NFR-07).

WHY this module exists: a content source can rot silently — a feed URL 301s into a
dead page, an API starts 403-ing our bot UA, a site retires an RSS endpoint. None
of these crash the daily run (INGEST isolates per-feed failures, see
:mod:`vision.ingest.feeds`), which is exactly the danger: a source can be quietly
dead for weeks while the run keeps "succeeding" on the survivors. This module
closes that gap:

  * :func:`record_ingest_success` — the helper INGEST calls to stamp a source's
    ``last_ok_at`` after a successful fetch, so "when did this feed last work?" is
    always answerable from the DB (§11.1).
  * :func:`check_feed_health`      — the ops check (run daily / on a timer) that
    flags any enabled source silent past a configurable threshold (or that has
    NEVER succeeded and is old enough to count), raises ONE ``dead_feed`` alert for
    the batch, and — only when explicitly enabled — auto-disables feeds that have
    been dead far longer, so a permanently broken source stops wasting a fetch slot.

SECURITY / SAFETY (§22): fail-closed and least-surprise — auto-disable is OFF by
default (a human toggles a feed back on, §12.2), thresholds are config-over-code,
a brand-new source is NOT flagged before it has had a fair chance to succeed, and
the alert body carries only source NAMES (never a URL that could embed a token).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from vision.db.models import Source
from vision.ops.alerts import Alerter, AlertKind

_log = logging.getLogger(__name__)

# --- Thresholds (config-over-code, §22) ------------------------------------
# A source silent longer than this is "dead" and alerted on. Default 48h mirrors
# the ingest recency window: if a feed hasn't produced anything usable in two days
# it is worth the owner's attention. Overridable via env without a code change.
_DEFAULT_STALE_AFTER = timedelta(hours=48)

# A source dead longer than THIS may be auto-disabled (only when auto-disable is
# switched on). Deliberately much larger than the stale threshold so a transient
# multi-day outage alerts long before anything is disabled. Default 7 days.
_DEFAULT_DISABLE_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class FeedHealthReport:
    """Immutable outcome of one :func:`check_feed_health` pass.

    A factual snapshot the caller can log/assert on. Frozen (§22 immutability) so a
    report is never edited after the check produced it. Lists hold source NAMES —
    safe to surface in logs/alerts, unlike URLs which could embed credentials.
    """

    stale: tuple[str, ...] = ()  # enabled sources flagged silent past the threshold
    disabled: tuple[str, ...] = ()  # sources auto-disabled this pass (subset of stale)
    healthy: tuple[str, ...] = ()  # enabled sources within the freshness threshold
    alerted: bool = False  # whether a dead_feed alert was actually dispatched


@dataclass(frozen=True)
class _Thresholds:
    """Resolved thresholds + toggle for a check, read once from env.

    Bundled so the pure decision logic (:func:`_classify`) takes plain values and
    stays trivially unit-testable, while env parsing lives in one place.
    """

    stale_after: timedelta = _DEFAULT_STALE_AFTER
    disable_after: timedelta = _DEFAULT_DISABLE_AFTER
    auto_disable: bool = False


def record_ingest_success(session: Session, source: Source, now: datetime) -> None:
    """Stamp ``source.last_ok_at = now`` after a successful fetch (§11.1 / §17).

    The tiny helper INGEST calls once a source has yielded a usable feed, so
    feed-health has an authoritative "last worked at" to measure against. The
    assignment is a normal ORM attribute write (a new value, not a mutation of a
    shared object), and the commit is left to the caller's unit-of-work so this can
    participate in the ingest transaction rather than forcing its own.
    """
    source.last_ok_at = now
    session.add(source)
    _log.debug("feed marked healthy: %s", source.name)


def _hours_from_env(var: str, default: timedelta) -> timedelta:
    """Read an hours-valued threshold from env, falling back to ``default``.

    A missing var uses the default; a non-integer or non-positive value is a config
    error that we log and fall back on rather than crash the health check (an
    un-run check is worse than a mis-tuned one).
    """
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        hours = int(raw)
    except ValueError:
        _log.warning("%s is not an integer; using default threshold.", var)
        return default
    if hours <= 0:
        _log.warning("%s must be positive; using default threshold.", var)
        return default
    return timedelta(hours=hours)


def _load_thresholds() -> _Thresholds:
    """Resolve stale/disable thresholds and the auto-disable toggle from env."""
    stale_after = _hours_from_env("FEED_HEALTH_STALE_HOURS", _DEFAULT_STALE_AFTER)
    disable_after = _hours_from_env("FEED_HEALTH_DISABLE_HOURS", _DEFAULT_DISABLE_AFTER)
    # Auto-disable is OFF unless explicitly enabled — a human normally curates feeds
    # (§12.2), so the safe default is to alert and leave the toggle to the owner.
    auto_disable = os.environ.get("FEED_HEALTH_AUTO_DISABLE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return _Thresholds(
        stale_after=stale_after,
        disable_after=disable_after,
        auto_disable=auto_disable,
    )


def _as_aware(value: datetime | None) -> datetime | None:
    """Coerce a datetime to UTC-aware, or pass ``None`` through.

    WHY: our ``DateTime(timezone=True)`` columns are stored tz-aware, but the
    SQLite dev/test backend returns NAIVE datetimes on read-back (Postgres returns
    aware). Comparing a naive DB value against a tz-aware ``now`` would raise
    ``TypeError``. Treating a naive timestamp as UTC (the timezone everything is
    persisted in) makes the staleness maths portable across both backends without
    special-casing the dialect at every call site.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _reference_time(source: Source) -> datetime | None:
    """Return the instant to measure silence from: ``last_ok_at`` else ``created_at``.

    A source that has succeeded before is judged on how long since it last worked.
    A source that has NEVER succeeded (``last_ok_at is None``) is judged on how long
    since it was ADDED — so a freshly configured feed is not flagged dead before it
    has had a fair chance to run, while a feed that has never once worked since being
    added days ago IS flagged.
    """
    return _as_aware(source.last_ok_at or source.created_at)


def _is_stale(source: Source, now: datetime, stale_after: timedelta) -> bool:
    """Whether ``source`` has been silent longer than ``stale_after`` as of ``now``."""
    reference = _reference_time(source)
    if reference is None:
        # No timestamps at all (shouldn't happen once persisted): treat as NOT stale
        # so an un-timestamped in-memory object never triggers a spurious alert.
        return False
    return now - reference > stale_after


def _is_disable_due(source: Source, now: datetime, disable_after: timedelta) -> bool:
    """Whether ``source`` has been dead long enough to be auto-disabled."""
    reference = _reference_time(source)
    if reference is None:
        return False
    return now - reference > disable_after


def check_feed_health(
    session: Session,
    now: datetime,
    alerter: Alerter | None = None,
) -> FeedHealthReport:
    """Flag silent sources, raise one ``dead_feed`` alert, optionally auto-disable.

    Steps:
      1. Load every ENABLED source (a disabled feed is already off — nothing to
         check, and re-flagging it would just be noise).
      2. Classify each as healthy or stale against ``FEED_HEALTH_STALE_HOURS``,
         using ``last_ok_at`` (or ``created_at`` for a never-succeeded feed) as the
         reference so a brand-new feed is not prematurely flagged.
      3. If auto-disable is switched ON, disable any stale feed that has been dead
         past ``FEED_HEALTH_DISABLE_HOURS`` (a much longer threshold) and record it.
      4. Emit exactly ONE ``dead_feed`` alert naming the stale feeds — the Alerter's
         dedup then suppresses repeats across subsequent runs within its window, so
         a persistently dead feed notifies once per incident, not once per tick.

    ``alerter`` is injected (a mock in tests, the real :class:`Alerter` in prod) and
    may be ``None`` to run the check purely for its report without notifying. All DB
    writes are committed here so the auto-disable / (any) change is durable even if
    the alert send is slow.
    """
    thresholds = _load_thresholds()

    # (1) Only enabled sources are in scope.
    enabled_sources = list(
        session.execute(select(Source).where(Source.enabled.is_(True))).scalars()
    )

    stale: list[str] = []
    disabled: list[str] = []
    healthy: list[str] = []

    # (2) + (3) Classify, and auto-disable the long-dead when the toggle is on.
    for source in enabled_sources:
        if not _is_stale(source, now, thresholds.stale_after):
            healthy.append(source.name)
            continue

        stale.append(source.name)
        if thresholds.auto_disable and _is_disable_due(source, now, thresholds.disable_after):
            # Persistently dead: stop wasting a fetch slot. The row is NOT deleted
            # (no-delete policy / §12.2) — the owner can re-enable it after fixing
            # the URL. A distinct log line makes the automated action auditable.
            source.enabled = False
            session.add(source)
            disabled.append(source.name)
            _log.warning("feed auto-disabled after prolonged silence: %s", source.name)

    # Persist any auto-disable changes in one unit of work before we notify, so the
    # DB reflects reality even if the alert channel is slow/unreachable.
    if disabled:
        session.commit()

    # (4) One aggregate alert for the whole batch (dedup handled by the Alerter).
    alerted = False
    if stale and alerter is not None:
        sorted_stale = sorted(stale)
        # The subject names the exact (sorted) dead set, NOT just the count. WHY:
        # the Alerter dedups on kind+subject, so a count-only subject would let a
        # genuinely different incident with the same count (feed B dies the day
        # feed A recovers) be silently suppressed. Naming the set makes the dedup
        # key vary with WHICH feeds are dead, so a new dead feed always alerts
        # while an unchanged dead set stays deduped within the window (NFR-08).
        subject = f"{len(sorted_stale)} feed(s) look dead: " + ", ".join(sorted_stale)
        # Detail carries NAMES only — never URLs (which could embed a token) (§22).
        detail = "Sources with no successful fetch past the health threshold: " + ", ".join(
            sorted_stale
        )
        alerted = alerter.alert(AlertKind.DEAD_FEED, subject, detail)

    if stale:
        _log.warning("feed-health flagged %d stale source(s)", len(stale))
    else:
        _log.info("feed-health: all %d enabled source(s) healthy", len(healthy))

    return FeedHealthReport(
        stale=tuple(stale),
        disabled=tuple(disabled),
        healthy=tuple(healthy),
        alerted=alerted,
    )
