"""Tests for the draft-expiry job (BRD FR-16 / §10.4).

Covers the four behaviours the job promises: a past-cutoff pending draft expires,
an in-window pending draft does not, an already-approved/published draft is never
touched, and the cutoff is evaluated in the owner's timezone (not UTC). Also
pins idempotency and the compare-and-set guard.

All tests use the hermetic in-memory SQLite ``db_session`` fixture (conftest) —
no real database, network, email, or clock. AAA structure throughout.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from vision.cli.expire import (
    STATE_EXPIRED,
    STATE_PENDING_APPROVAL,
    expire_stale_drafts,
)
from vision.config import get_settings
from vision.db.models import AuditLog, Draft

# --- Fixed reference instants ----------------------------------------------
# The default config is TZ=Asia/Kolkata (UTC+5:30, no DST) and cutoff 20:00, so
# the day's cutoff of 20:00 IST is exactly 14:30 UTC. These constants make that
# relationship explicit and let every test reason in the owner's wall clock.
_DRAFT_CREATED_UTC = datetime(2026, 7, 6, 1, 0, tzinfo=timezone.utc)  # 06:30 IST, "post day"
_IST_CUTOFF_UTC = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)  # 20:00 IST cutoff instant
_PAST_CUTOFF_UTC = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)  # 20:30 IST — past cutoff
_IN_WINDOW_UTC = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)  # 18:30 IST — before cutoff


@pytest.fixture(autouse=True)
def _default_settings() -> None:
    """Guarantee each test sees the documented defaults (TZ + 20:00 cutoff).

    ``get_settings`` is process-cached; clearing it stops any env set by another
    test from leaking in and makes the tz/cutoff arithmetic above deterministic.
    """
    get_settings.cache_clear()


def _make_draft(session: Session, state: str, created_at: datetime) -> Draft:
    """Persist and return a single ``Draft`` in ``state`` created at ``created_at``.

    A tiny factory (not inline objects) keeps each test's Arrange block to the one
    variable it cares about.
    """
    draft = Draft(state=state, created_at=created_at, post_text="draft body")
    session.add(draft)
    session.flush()  # assign the UUID PK so it can be re-fetched / audited
    return draft


def test_past_cutoff_pending_draft_expires(db_session: Session) -> None:
    # Arrange: a pending draft created this morning, evaluated after tonight's cutoff.
    draft = _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)

    # Act
    expire_stale_drafts(db_session, now=_PAST_CUTOFF_UTC)

    # Assert
    assert db_session.get(Draft, draft.id).state == STATE_EXPIRED


def test_past_cutoff_expiry_is_audit_logged(db_session: Session) -> None:
    # Arrange
    draft = _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)

    # Act
    expire_stale_drafts(db_session, now=_PAST_CUTOFF_UTC)

    # Assert: exactly one append-only audit row records the transition.
    rows = (
        db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == str(draft.id))
        )
        .scalars()
        .all()
    )
    assert [(r.entity, r.action, r.actor) for r in rows] == [
        ("draft", "expired", "system:vision-expire")
    ]


def test_in_window_pending_draft_not_expired(db_session: Session) -> None:
    # Arrange: a pending draft evaluated before the day's cutoff.
    draft = _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)

    # Act
    expire_stale_drafts(db_session, now=_IN_WINDOW_UTC)

    # Assert: still awaiting the owner, untouched.
    assert db_session.get(Draft, draft.id).state == STATE_PENDING_APPROVAL


def test_in_window_writes_no_audit_row(db_session: Session) -> None:
    # Arrange
    _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)

    # Act
    expire_stale_drafts(db_session, now=_IN_WINDOW_UTC)

    # Assert: nothing expired -> nothing logged.
    assert db_session.execute(select(AuditLog)).scalars().all() == []


def test_returns_count_of_expired_drafts(db_session: Session) -> None:
    # Arrange: two pending past-cutoff drafts + one in-window draft.
    _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)
    _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)
    _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)

    # Act: past cutoff -> all three are eligible.
    expired = expire_stale_drafts(db_session, now=_PAST_CUTOFF_UTC)

    # Assert
    assert expired == 3


@pytest.mark.parametrize("terminal_state", ["approved", "queued", "published", "rejected"])
def test_non_pending_drafts_are_untouched(db_session: Session, terminal_state: str) -> None:
    # Arrange: a draft in a state the expiry job must never transition, well past cutoff.
    draft = _make_draft(db_session, terminal_state, _DRAFT_CREATED_UTC)

    # Act
    expire_stale_drafts(db_session, now=_PAST_CUTOFF_UTC)

    # Assert: the state machine forbids expiring anything but pending_approval.
    assert db_session.get(Draft, draft.id).state == terminal_state


def test_tz_cutoff_evaluated_in_owner_timezone(db_session: Session) -> None:
    # Arrange: a pending draft evaluated at exactly 20:00 IST (== 14:30 UTC). A
    # timezone-naive implementation would read 14:30 as "before 20:00" and skip it.
    draft = _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)

    # Act: at the IST cutoff instant.
    expire_stale_drafts(db_session, now=_IST_CUTOFF_UTC)

    # Assert: expired, proving the cutoff is resolved against IST, not UTC.
    assert db_session.get(Draft, draft.id).state == STATE_EXPIRED


def test_tz_before_ist_cutoff_not_expired(db_session: Session) -> None:
    # Arrange: one minute before the IST cutoff (14:29 UTC == 19:59 IST).
    just_before = datetime(2026, 7, 6, 14, 29, tzinfo=timezone.utc)
    draft = _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)

    # Act
    expire_stale_drafts(db_session, now=just_before)

    # Assert: still inside the window in the owner's timezone.
    assert db_session.get(Draft, draft.id).state == STATE_PENDING_APPROVAL


def test_idempotent_second_run_expires_nothing_new(db_session: Session) -> None:
    # Arrange: a draft already expired by a first run.
    _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)
    expire_stale_drafts(db_session, now=_PAST_CUTOFF_UTC)

    # Act: run the job again over the same data.
    second_run = expire_stale_drafts(db_session, now=_PAST_CUTOFF_UTC)

    # Assert: nothing left to expire.
    assert second_run == 0


def test_idempotent_second_run_adds_no_duplicate_audit_row(db_session: Session) -> None:
    # Arrange
    _make_draft(db_session, STATE_PENDING_APPROVAL, _DRAFT_CREATED_UTC)
    expire_stale_drafts(db_session, now=_PAST_CUTOFF_UTC)

    # Act
    expire_stale_drafts(db_session, now=_PAST_CUTOFF_UTC)

    # Assert: exactly one audit row exists after two runs.
    assert len(db_session.execute(select(AuditLog)).scalars().all()) == 1


def test_yesterday_pending_draft_expires_today(db_session: Session) -> None:
    # Arrange: a draft created yesterday, still pending; its cutoff is long past.
    yesterday = datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
    draft = _make_draft(db_session, STATE_PENDING_APPROVAL, yesterday)

    # Act: evaluated the next morning, before today's cutoff.
    next_morning = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    expire_stale_drafts(db_session, now=next_morning)

    # Assert: a leftover draft from a prior day is past its own cutoff and expires.
    assert db_session.get(Draft, draft.id).state == STATE_EXPIRED
