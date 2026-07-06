"""``vision-expire`` entry point (cron, ~20:00 IST daily) — BRD FR-16 / §10.4.

WHY this job exists: FR-16 mandates that any draft the owner never actioned by
the daily cutoff (default 20:00 IST) auto-expires so nothing is posted that day.
This module finds every ``pending_approval`` draft whose cutoff has passed *in the
owner's local timezone* and moves it to ``expired`` through the one legal
transition of the §10.4 state machine, writing an append-only ``audit_log`` row
for each (BRD §16 auditability).

Security posture (driven by prep/security_threatmodel.md):
  * **Explicit state machine** — only ``pending_approval`` may become ``expired``
    (T7 "invalid state transition"). ``approved`` / ``queued`` / ``published`` /
    ``rejected`` drafts are never touched.
  * **Transactional compare-and-set** — the transition is a guarded UPDATE
    (``... WHERE state = 'pending_approval'``) so a draft the owner approves at
    the same instant as this job runs is claimed by exactly one writer; the loser
    affects zero rows and does nothing. This also makes overlapping cron runs
    safe (idempotent) without a separate lock (threat-model "prevent overlapping
    cron runs").
  * **Server-side UTC time, strict parsing, fail-closed on clock error** — the
    cutoff is parsed strictly and every comparison is done in UTC. Any timezone
    or parsing error propagates and the job aborts having expired nothing, so a
    clock bug can never wrongly kill a still-valid draft (threat-model expiry row).

The pure worker (:func:`expire_stale_drafts`) takes an injected ``now`` and an
open ``Session`` so it is deterministic and unit-testable; :func:`main` supplies
the real clock and a committing session for cron.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from vision.config import get_settings
from vision.db.models import AuditLog, Draft
from vision.db.session import get_session
from vision.logging_setup import configure_logging, get_logger

logger = get_logger("vision.cli.expire")

# --- State-machine constants (BRD §10.4) -----------------------------------
# Named here (not inlined as magic strings) so the single legal transition this
# job performs — pending_approval -> expired — is a self-documenting contract.
STATE_PENDING_APPROVAL = "pending_approval"
STATE_EXPIRED = "expired"


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    WHY: timestamps that round-trip through SQLite come back *naive* (SQLite has
    no native tz type), while Postgres and our own ``datetime.now(timezone.utc)``
    yield aware values. Normalising to aware-UTC here means every downstream
    comparison is apples-to-apples and can never silently mix a naive local time
    with an aware UTC one (the classic expiry-bypass bug the threat model calls
    out). A naive value is assumed to already be UTC, matching how the ORM stores
    ``server_default=func.now()`` / our explicit UTC writes.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_cutoff(raw: str) -> time:
    """Strictly parse an ``"HH:MM"`` local cutoff string into a ``time``.

    Strict parsing (int-cast of exactly two ``:``-separated fields) is a
    deliberate fail-closed choice: a malformed ``APPROVE_CUTOFF_LOCAL`` raises
    ``ValueError`` here and aborts the whole job rather than being coerced into a
    surprising cutoff that expires drafts at the wrong instant.
    """
    hours_raw, minutes_raw = raw.split(":")
    return time(hour=int(hours_raw), minute=int(minutes_raw))


def _cutoff_instant_utc(local_day: date, cutoff: time, tz: ZoneInfo) -> datetime:
    """Return the UTC instant of ``cutoff`` on ``local_day`` in timezone ``tz``.

    The cutoff is a *wall-clock* time in the owner's timezone (e.g. 20:00 IST),
    so we build the aware local datetime and convert to UTC for comparison. This
    is what makes the job timezone-correct: 20:00 IST resolves to 14:30 UTC, not
    20:00 UTC, so a draft is judged against the owner's clock regardless of where
    the process runs.
    """
    local_dt = datetime.combine(local_day, cutoff, tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


def expire_stale_drafts(session: Session, now: datetime) -> int:
    """Expire every ``pending_approval`` draft whose cutoff has passed by ``now``.

    A draft's cutoff is the configured ``APPROVE_CUTOFF_LOCAL`` time on the
    *local date the draft was created* (its "post day", §10.4) — so yesterday's
    leftover pending draft is already past cutoff today, while a draft created
    this morning only expires once today's cutoff arrives.

    Args:
        session: an open SQLAlchemy session; transaction control (commit/rollback)
            is the caller's responsibility so this composes inside a larger unit
            of work and stays testable against an in-memory DB.
        now: the reference instant (injected for determinism); may be naive
            (treated as UTC) or aware.

    Returns:
        The number of drafts actually transitioned to ``expired`` — i.e. the
        number of guarded UPDATEs that won their compare-and-set. Running the job
        again returns 0 (idempotent), because those drafts are no longer
        ``pending_approval``.
    """
    settings = get_settings()
    now_utc = _as_utc(now)

    # Resolve timezone + cutoff up front. If either is misconfigured this raises
    # *before* any state change, so the job fails closed having expired nothing.
    tz = ZoneInfo(settings.tz)
    cutoff = _parse_cutoff(settings.approve_cutoff_local)

    # Only ``pending_approval`` drafts are ever candidates — the state machine
    # forbids expiring anything else, so we never even load the others.
    candidates = (
        session.execute(select(Draft).where(Draft.state == STATE_PENDING_APPROVAL))
        .scalars()
        .all()
    )

    expired_count = 0
    for draft in candidates:
        # The draft's own creation day (in the owner's tz) anchors its cutoff, so
        # a draft is measured against the cutoff of the day it belongs to.
        draft_local_day = _as_utc(draft.created_at).astimezone(tz).date()
        cutoff_utc = _cutoff_instant_utc(draft_local_day, cutoff, tz)

        # Still inside today's approval window -> leave it for the owner to action.
        # ``>=`` (not ``>``) means a draft is expired *at* the cutoff second, with
        # no one-second grace, mirroring the token verifier's expiry semantics.
        if now_utc < cutoff_utc:
            continue

        # Guarded compare-and-set: transition ONLY if the row is still
        # ``pending_approval``. If the owner approved/rejected it a moment ago the
        # WHERE clause matches zero rows and we make no change and log nothing —
        # this is the atomicity the threat model requires against a concurrent
        # approve and against overlapping cron runs. ``updated_at`` is stamped
        # explicitly because a Core UPDATE bypasses the ORM ``onupdate`` hook.
        result = session.execute(
            update(Draft)
            .where(Draft.id == draft.id, Draft.state == STATE_PENDING_APPROVAL)
            .values(state=STATE_EXPIRED, updated_at=now_utc)
            .execution_options(synchronize_session="fetch"),
        )
        if result.rowcount != 1:
            # Lost the race (concurrently actioned) — respect the other writer.
            logger.info(
                "draft not expired: no longer pending_approval",
                extra={"draft_id": str(draft.id)},
            )
            continue

        # Append-only audit trail of the transition (BRD §16 / §11.7). Actor is
        # the job itself; meta carries the WHY + the exact cutoff instant so a
        # reviewer can reconstruct the decision. No secrets are recorded.
        session.add(
            AuditLog(
                entity="draft",
                entity_id=str(draft.id),
                action="expired",
                actor="system:vision-expire",
                at=now_utc,
                meta={
                    "reason": "cutoff_passed",
                    "from_state": STATE_PENDING_APPROVAL,
                    "to_state": STATE_EXPIRED,
                    "cutoff_local": settings.approve_cutoff_local,
                    "tz": settings.tz,
                    "cutoff_utc": cutoff_utc.isoformat(),
                },
            )
        )
        expired_count += 1
        logger.info(
            "draft expired (cutoff passed)",
            extra={"draft_id": str(draft.id), "cutoff_utc": cutoff_utc.isoformat()},
        )

    # Flush so the audit rows + state changes are pending in the transaction; the
    # caller owns the final commit (get_session in main, or the test's session).
    session.flush()
    return expired_count


def main() -> int:
    """Entry point for the ``vision-expire`` console script (cron at cutoff).

    Returns a process exit code (0 = success, 1 = failure) so cron can detect and
    alert on a failed run. Uses the real UTC clock and a committing session.
    """
    configure_logging()
    settings = get_settings()
    now = datetime.now(timezone.utc)
    logger.info(
        "vision-expire invoked",
        extra={"tz": settings.tz, "cutoff_local": settings.approve_cutoff_local},
    )

    try:
        # get_session commits on success and rolls back on any error, so a partial
        # run never leaves some drafts expired and their audit rows missing.
        with get_session() as session:
            expired_count = expire_stale_drafts(session, now)
    except Exception:
        # Top-level cron boundary (not a bare except): log with stack trace and
        # exit non-zero. Fail-closed — on any error the transaction rolled back,
        # so nothing was expired and no valid draft was wrongly killed.
        logger.exception("vision-expire failed; no drafts expired (fail-closed)")
        return 1

    logger.info("vision-expire complete", extra={"expired": expired_count})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
