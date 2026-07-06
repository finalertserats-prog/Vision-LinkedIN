"""Unit tests for the draft state machine (BRD §10.4, threat model §2).

These tests run against the hermetic in-memory SQLite session from ``conftest``
(no real DB, no network) and exercise the full contract: the legal lifecycle
path, rejection of every illegal edge, the token requirement for approval, the
single-use / atomic-consumption guarantee, published idempotency, and the
append-only audit trail (one row per real transition).

Every test follows AAA (Arrange -> Act -> Assert) with one behaviour per test.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from vision.approval.errors import ExpiredToken
from vision.approval.errors import UsedToken as TokenAlreadyUsed
from vision.approval.state_errors import IllegalTransition, TokenBindingError, TokenRequired
from vision.approval.state_machine import DraftState, transition
from vision.approval.tokens import issue_token
from vision.config import get_settings
from vision.db.models import AuditLog, Draft

# A generous TTL so a freshly-issued approval token is comfortably unexpired
# during a test run (real seconds, deterministic enough for a unit test).
_VALID_TTL_SECONDS = 3600


def _new_draft(session: Session, state: DraftState = DraftState.NEW) -> Draft:
    """Persist a draft in ``state`` and return it with a real UUID id.

    A factory (not an inline object) per the testing rules; ``flush`` assigns the
    PK so the id is usable both as a token binding and as an ``audit_log`` key.
    """
    draft = Draft(state=state.value)
    session.add(draft)
    session.flush()
    return draft


def _approve_token(draft: Draft) -> str:
    """Mint a valid ``approve`` token bound to ``draft`` using the app secret.

    Uses the same ``issue_token`` + configured HMAC secret the production email
    job uses, so the state machine's ``verify_token`` call sees a genuinely valid,
    correctly-bound token.
    """
    token_str, _hash, _exp = issue_token(
        str(draft.id), "approve", _VALID_TTL_SECONDS, get_settings().secret_hmac_key
    )
    return token_str


def _audit_count(session: Session, draft: Draft) -> int:
    """Return how many append-only audit rows exist for ``draft``."""
    return (
        session.query(AuditLog)
        .filter(AuditLog.entity == "draft", AuditLog.entity_id == str(draft.id))
        .count()
    )


def test_full_legal_lifecycle_reaches_published(db_session: Session) -> None:
    # Arrange: a brand-new draft at the start of the lifecycle.
    draft = _new_draft(db_session)

    # Act: walk the entire happy path new -> drafted -> pending_approval ->
    # approved (with a valid token) -> queued -> published.
    transition(db_session, draft, DraftState.DRAFTED, actor="system")
    transition(db_session, draft, DraftState.PENDING_APPROVAL, actor="system")
    transition(
        db_session,
        draft,
        DraftState.APPROVED,
        actor="owner",
        meta={"token": _approve_token(draft), "ip": "203.0.113.7"},
    )
    transition(db_session, draft, DraftState.QUEUED, actor="scheduler")
    transition(db_session, draft, DraftState.PUBLISHED, actor="publisher")

    # Assert: the draft lands in the terminal published state.
    assert draft.state == DraftState.PUBLISHED.value


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (DraftState.NEW, DraftState.APPROVED),  # can't skip straight to approved
        (DraftState.NEW, DraftState.PUBLISHED),  # can't skip the whole pipeline
        (DraftState.DRAFTED, DraftState.PUBLISHED),  # must be approved+queued first
        (DraftState.PENDING_APPROVAL, DraftState.QUEUED),  # must be approved first
        (DraftState.APPROVED, DraftState.PUBLISHED),  # must be queued first
        (DraftState.QUEUED, DraftState.APPROVED),  # no going back to approved
        (DraftState.REJECTED, DraftState.APPROVED),  # rejected is terminal
        (DraftState.EXPIRED, DraftState.APPROVED),  # expired is terminal
        (DraftState.DEAD_LETTER, DraftState.QUEUED),  # dead_letter is terminal
        (DraftState.PUBLISHED, DraftState.QUEUED),  # published cannot be re-queued
    ],
)
def test_illegal_transition_raises(
    db_session: Session, from_state: DraftState, to_state: DraftState
) -> None:
    # Arrange: a draft parked directly in the illegal edge's source state.
    draft = _new_draft(db_session, state=from_state)

    # Act / Assert: the disallowed edge is refused before any state change.
    with pytest.raises(IllegalTransition):
        transition(db_session, draft, to_state, actor="owner")

    # Assert: the draft's state was not touched by the refused transition.
    assert draft.state == from_state.value


def test_approve_without_token_raises_token_required(db_session: Session) -> None:
    # Arrange: a draft awaiting approval, but no token supplied.
    draft = _new_draft(db_session, state=DraftState.PENDING_APPROVAL)

    # Act / Assert: approval is refused for lack of authorisation.
    with pytest.raises(TokenRequired):
        transition(db_session, draft, DraftState.APPROVED, actor="owner")

    # Assert: fail-closed — the draft stays pending.
    assert draft.state == DraftState.PENDING_APPROVAL.value


def test_approve_with_expired_token_raises(db_session: Session) -> None:
    # Arrange: a pending draft and an already-expired approval token (past TTL).
    draft = _new_draft(db_session, state=DraftState.PENDING_APPROVAL)
    expired_token, _h, _e = issue_token(
        str(draft.id), "approve", -10, get_settings().secret_hmac_key
    )

    # Act / Assert: an unexpired token is required, so this is rejected.
    with pytest.raises(ExpiredToken):
        transition(
            db_session, draft, DraftState.APPROVED, actor="owner",
            meta={"token": expired_token},
        )

    # Assert: no state change on a dead token.
    assert draft.state == DraftState.PENDING_APPROVAL.value


def test_approve_with_token_for_other_draft_raises_binding_error(
    db_session: Session,
) -> None:
    # Arrange: two drafts; a token minted for the OTHER draft.
    draft = _new_draft(db_session, state=DraftState.PENDING_APPROVAL)
    other = _new_draft(db_session, state=DraftState.PENDING_APPROVAL)
    foreign_token = _approve_token(other)

    # Act / Assert: a valid-but-misbound token is refused (defence in depth).
    with pytest.raises(TokenBindingError):
        transition(
            db_session, draft, DraftState.APPROVED, actor="owner",
            meta={"token": foreign_token},
        )

    assert draft.state == DraftState.PENDING_APPROVAL.value


def test_approval_token_is_single_use(db_session: Session) -> None:
    # Arrange: approve a draft with a token, then simulate a replay by resetting
    # the draft to pending and re-presenting the SAME token.
    draft = _new_draft(db_session, state=DraftState.PENDING_APPROVAL)
    token = _approve_token(draft)
    transition(db_session, draft, DraftState.APPROVED, actor="owner", meta={"token": token})
    draft.state = DraftState.PENDING_APPROVAL.value  # force a replay opportunity
    db_session.flush()

    # Act / Assert: the nonce was consumed atomically with the first approval, so
    # the replayed token is rejected.
    with pytest.raises(TokenAlreadyUsed):
        transition(db_session, draft, DraftState.APPROVED, actor="owner", meta={"token": token})


def test_published_is_idempotent_no_op(db_session: Session) -> None:
    # Arrange: a draft already live (terminal published state).
    draft = _new_draft(db_session, state=DraftState.PUBLISHED)
    audit_before = _audit_count(db_session, draft)

    # Act: a repeat publish and a late re-approve must both be no-ops.
    transition(db_session, draft, DraftState.PUBLISHED, actor="publisher")
    transition(db_session, draft, DraftState.APPROVED, actor="owner")

    # Assert: state unchanged and NO new audit rows written for the no-ops.
    assert draft.state == DraftState.PUBLISHED.value
    assert _audit_count(db_session, draft) == audit_before


def test_audit_row_written_per_transition(db_session: Session) -> None:
    # Arrange: a fresh draft with no audit history.
    draft = _new_draft(db_session)
    assert _audit_count(db_session, draft) == 0

    # Act: perform three real transitions.
    transition(db_session, draft, DraftState.DRAFTED, actor="system")
    transition(db_session, draft, DraftState.PENDING_APPROVAL, actor="system")
    transition(
        db_session, draft, DraftState.APPROVED, actor="owner",
        meta={"token": _approve_token(draft)},
    )

    # Assert: exactly one append-only audit row exists per real transition.
    assert _audit_count(db_session, draft) == 3


def test_audit_row_never_stores_raw_token(db_session: Session) -> None:
    # Arrange: approve a draft, passing the raw token and an ip in meta.
    draft = _new_draft(db_session, state=DraftState.PENDING_APPROVAL)
    token = _approve_token(draft)

    # Act: perform the approval.
    transition(
        db_session, draft, DraftState.APPROVED, actor="owner",
        meta={"token": token, "ip": "203.0.113.9"},
    )

    # Assert: the audit row records the ip and a nonce hash, but NEVER the raw
    # token (threat model §1 — never log full tokens).
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == str(draft.id), AuditLog.action == "approved")
        .one()
    )
    assert row.ip == "203.0.113.9"
    assert "token" not in (row.meta or {})
    assert token not in str(row.meta)
    assert "token_hash" in (row.meta or {})
