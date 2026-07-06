"""Tests for the FastAPI approval service ``vision-web`` (BRD §14.2/§14.3).

These tests exercise the full security contract of the approval endpoints through
the real ``TestClient`` — GET never mutates, POST atomically consumes + transits,
replays/expired/tampered tokens are rejected generically, edits re-validate, and
the security headers are present. Everything is hermetic:

  * an in-memory SQLite DB (shared connection via ``StaticPool``) is created per
    test and injected as the app's ``session_factory``;
  * a ``RecordingPublisher`` mock captures publish calls (no network, no real
    LinkedIn) so "called EXACTLY once" is assertable;
  * tokens are minted with the real :func:`issue_token`, so no crypto is faked.

AAA (Arrange → Act → Assert), one behaviour per test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vision.approval import service
from vision.approval.service import PublishCall
from vision.approval.tokens import issue_token
from vision.approval.web import InMemoryRateLimiter, create_app
from vision.config import Settings
from vision.db.base import Base
from vision.db import models  # noqa: F401 — register tables on Base.metadata
from vision.db.models import Draft, UsedToken

# A fixed test secret + TTL. Constants (not shared mutable state) so tests are
# independent. The secret is non-default so /healthz reports "configured".
_SECRET = "web-test-hmac-secret"
_TTL = 3600  # one hour — comfortably unexpired for the happy paths


# --- Mock publisher ---------------------------------------------------------
@dataclass
class RecordingPublisher:
    """A :class:`service.PublisherPort` that records calls and does no I/O."""

    calls: list[PublishCall] = field(default_factory=list)

    def publish(
        self,
        *,
        draft_id: str,
        text: str,
        image_path: str | None,
        scheduled_for: datetime | None,
        idempotency_key: str = "",
    ) -> str:
        self.calls.append(
            PublishCall(
                draft_id=draft_id,
                text=text,
                image_path=image_path,
                scheduled_for=scheduled_for,
                idempotency_key=idempotency_key,
            )
        )
        return f"mock:{draft_id}"


# --- Hermetic app harness ---------------------------------------------------
@dataclass
class Harness:
    """Everything a test needs to drive the app + inspect the DB."""

    client: TestClient
    publisher: RecordingPublisher
    sessionmaker: sessionmaker
    settings: Settings


@pytest.fixture
def harness() -> Iterator[Harness]:
    """Build an app wired to a fresh in-memory DB + a recording publisher."""
    # One shared in-memory connection so the app and the test see the same DB.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @contextmanager
    def session_factory() -> Iterator[Session]:
        # Mirror db.session.get_session semantics: commit on success, rollback on
        # error — this is what makes the endpoint's atomic transaction real.
        session = TestSession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    settings = Settings(SECRET_HMAC_KEY=_SECRET, TZ="Asia/Kolkata")
    publisher = RecordingPublisher()
    app = create_app(
        settings=settings,
        publisher=publisher,
        session_factory=session_factory,
        # Generous limit so functional tests never trip it; a dedicated test
        # constructs its own tight limiter.
        rate_limiter=InMemoryRateLimiter(max_requests=1000, window_seconds=60),
    )
    client = TestClient(app)
    try:
        yield Harness(
            client=client,
            publisher=publisher,
            sessionmaker=TestSession,
            settings=settings,
        )
    finally:
        client.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed_draft(
    harness: Harness,
    *,
    state: str = service.STATE_NEW,
    post_text: str = "A grounded, professional insight about healthcare AI.",
    hashtags: list[str] | None = None,
) -> Draft:
    """Insert a draft and return it (detached but with a stable id)."""
    session = harness.sessionmaker()
    try:
        draft = Draft(
            post_text=post_text,
            hashtags=hashtags if hashtags is not None else ["#Health", "#AI", "#Data"],
            state=state,
        )
        session.add(draft)
        session.commit()
        session.refresh(draft)
        session.expunge(draft)
        return draft
    finally:
        session.close()


def _mint(draft: Draft, action: str, *, ttl: int = _TTL) -> str:
    """Mint a real signed token for ``draft`` + ``action``."""
    token_str, _hash, _exp = issue_token(str(draft.id), action, ttl, _SECRET)
    return token_str


def _draft_state(harness: Harness, draft_id) -> str:
    """Read the current persisted state of a draft."""
    session = harness.sessionmaker()
    try:
        return session.get(Draft, draft_id).state
    finally:
        session.close()


def _used_count(harness: Harness) -> int:
    """Count rows in the single-use ledger."""
    session = harness.sessionmaker()
    try:
        return session.query(UsedToken).count()
    finally:
        session.close()


# --- GET never mutates ------------------------------------------------------
def test_get_approve_shows_confirmation_and_does_not_change_state(harness: Harness) -> None:
    # Arrange
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")

    # Act
    response = harness.client.get("/approve", params={"token": token})

    # Assert — a confirmation page is shown, but nothing changed.
    assert response.status_code == 200
    assert "Confirm" in response.text
    assert "<form" in response.text and 'method="post"' in response.text.lower()
    assert _draft_state(harness, draft.id) == service.STATE_NEW
    assert _used_count(harness) == 0
    assert harness.publisher.calls == []


def test_get_edit_renders_editable_page_with_char_counter(harness: Harness) -> None:
    # Arrange
    draft = _seed_draft(harness)
    token = _mint(draft, "edit")

    # Act
    response = harness.client.get("/edit", params={"token": token})

    # Assert — the edit page has a textarea pre-filled + the inline counter.
    assert response.status_code == 200
    assert "<textarea" in response.text
    assert "characters" in response.text  # the live counter label
    assert draft.post_text in response.text
    assert _draft_state(harness, draft.id) == service.STATE_NEW


# --- POST approve: transition + consume + publish once ----------------------
def test_post_approve_transitions_consumes_and_publishes_once(harness: Harness) -> None:
    # Arrange
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")

    # Act
    response = harness.client.post("/approve", data={"token": token})

    # Assert — scheduled, token consumed once, publisher called EXACTLY once.
    assert response.status_code == 200
    assert "approved" in response.text.lower()
    assert _draft_state(harness, draft.id) == service.STATE_SCHEDULED
    assert _used_count(harness) == 1
    assert len(harness.publisher.calls) == 1
    call = harness.publisher.calls[0]
    assert call.draft_id == str(draft.id)
    assert call.scheduled_for is not None  # approve enqueues for a slot


def test_post_post_now_publishes_immediately(harness: Harness) -> None:
    # Arrange
    draft = _seed_draft(harness)
    token = _mint(draft, "post_now")

    # Act — URL uses the hyphen form; the token action is post_now.
    response = harness.client.post("/post-now", data={"token": token})

    # Assert — published now (scheduled_for is None) and published exactly once.
    assert response.status_code == 200
    assert _draft_state(harness, draft.id) == service.STATE_PUBLISHED
    assert len(harness.publisher.calls) == 1
    assert harness.publisher.calls[0].scheduled_for is None


# --- Replay / invalid / expired --------------------------------------------
def test_replayed_token_is_rejected_without_second_state_change(harness: Harness) -> None:
    # Arrange — approve once so the nonce is consumed.
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")
    first = harness.client.post("/approve", data={"token": token})
    assert first.status_code == 200

    # Act — replay the very same link.
    replay = harness.client.post("/approve", data={"token": token})

    # Assert — generic rejection, no second publish, still exactly one consumed.
    assert replay.status_code == 400
    assert "no longer valid" in replay.text.lower()
    assert len(harness.publisher.calls) == 1
    assert _used_count(harness) == 1


def test_expired_token_is_rejected(harness: Harness) -> None:
    # Arrange — a token whose TTL is already in the past.
    draft = _seed_draft(harness)
    token = _mint(draft, "approve", ttl=-10)

    # Act
    response = harness.client.post("/approve", data={"token": token})

    # Assert — generic error, nothing changed, publisher untouched.
    assert response.status_code == 400
    assert "no longer valid" in response.text.lower()
    assert _draft_state(harness, draft.id) == service.STATE_NEW
    assert harness.publisher.calls == []


def test_tampered_token_is_rejected(harness: Harness) -> None:
    # Arrange — flip the last character of a valid token to break the signature.
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    # Act
    response = harness.client.post("/approve", data={"token": tampered})

    # Assert
    assert response.status_code == 400
    assert _draft_state(harness, draft.id) == service.STATE_NEW
    assert harness.publisher.calls == []


def test_get_action_must_match_token_action(harness: Harness) -> None:
    # Arrange — a token scoped to approve cannot be replayed on the reject path.
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")

    # Act — present the approve token to the /reject endpoint.
    response = harness.client.get("/reject", params={"token": token})

    # Assert — mismatch is rejected generically, no mutation.
    assert response.status_code == 400
    assert _draft_state(harness, draft.id) == service.STATE_NEW


# --- Reject path ------------------------------------------------------------
def test_post_reject_discards_without_publishing(harness: Harness) -> None:
    # Arrange
    draft = _seed_draft(harness)
    token = _mint(draft, "reject")

    # Act
    response = harness.client.post("/reject", data={"token": token})

    # Assert — rejected, token consumed, publisher NEVER called.
    assert response.status_code == 200
    assert "rejected" in response.text.lower()
    assert _draft_state(harness, draft.id) == service.STATE_REJECTED
    assert _used_count(harness) == 1
    assert harness.publisher.calls == []


# --- Edit path --------------------------------------------------------------
def test_post_edit_updates_text_and_schedules(harness: Harness) -> None:
    # Arrange
    draft = _seed_draft(harness)
    token = _mint(draft, "edit")
    new_text = "An edited, still-grounded take on prior-authorisation turnaround."

    # Act
    response = harness.client.post(
        "/edit",
        data={
            "token": token,
            "post_text": new_text,
            "hashtags": "#Health #AI #RCM",
        },
    )

    # Assert — text replaced, scheduled, published once with the EDITED text.
    assert response.status_code == 200
    assert _draft_state(harness, draft.id) == service.STATE_SCHEDULED
    assert len(harness.publisher.calls) == 1
    assert harness.publisher.calls[0].text == new_text
    session = harness.sessionmaker()
    try:
        assert session.get(Draft, draft.id).post_text == new_text
    finally:
        session.close()


def test_post_edit_invalid_revalidates_without_consuming(harness: Harness) -> None:
    # Arrange — too few hashtags fails the format re-check.
    draft = _seed_draft(harness)
    token = _mint(draft, "edit")

    # Act
    response = harness.client.post(
        "/edit",
        data={"token": token, "post_text": "Short valid text.", "hashtags": "#One"},
    )

    # Assert — edit page re-rendered with an error, nothing consumed/changed.
    assert response.status_code == 400
    assert "<textarea" in response.text
    assert "fix" in response.text.lower()
    assert _draft_state(harness, draft.id) == service.STATE_NEW
    assert _used_count(harness) == 0
    assert harness.publisher.calls == []


def test_post_edit_can_retry_after_validation_failure(harness: Harness) -> None:
    # Arrange — first submit is invalid, so the token must remain usable.
    draft = _seed_draft(harness)
    token = _mint(draft, "edit")
    harness.client.post(
        "/edit",
        data={"token": token, "post_text": "Text.", "hashtags": "#One"},
    )

    # Act — resubmit the SAME link with a valid edit.
    good = harness.client.post(
        "/edit",
        data={
            "token": token,
            "post_text": "A corrected, grounded insight.",
            "hashtags": "#Health #AI #Ops",
        },
    )

    # Assert — the retry succeeds (token was never consumed on the failure).
    assert good.status_code == 200
    assert _draft_state(harness, draft.id) == service.STATE_SCHEDULED
    assert len(harness.publisher.calls) == 1


# --- Health + security headers ---------------------------------------------
def test_healthz_reports_ok(harness: Harness) -> None:
    # Act
    response = harness.client.get("/healthz")

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["token_secret"] == "configured"


def test_security_headers_present_on_every_response(harness: Harness) -> None:
    # Arrange
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")

    # Act
    response = harness.client.get("/approve", params={"token": token})

    # Assert — the load-bearing no-referrer + hardening headers are set.
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_docs_are_disabled(harness: Harness) -> None:
    # Act — the OpenAPI schema + docs must not be served (no public docs).
    schema = harness.client.get("/openapi.json")
    docs = harness.client.get("/docs")

    # Assert — both resolve to the generic 404 path, not a live schema/UI.
    assert schema.status_code == 404
    assert docs.status_code == 404


def test_rate_limiter_blocks_flood() -> None:
    # Arrange — a dedicated app with a tight limiter (1 request / window).
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @contextmanager
    def session_factory() -> Iterator[Session]:
        session = TestSession()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app = create_app(
        settings=Settings(SECRET_HMAC_KEY=_SECRET),
        publisher=RecordingPublisher(),
        session_factory=session_factory,
        rate_limiter=InMemoryRateLimiter(max_requests=1, window_seconds=60),
    )
    client = TestClient(app)

    # Act — the second request within the window is throttled.
    first = client.get("/approve", params={"token": "x"})
    second = client.get("/approve", params={"token": "x"})

    # Assert
    assert first.status_code == 400  # invalid token, but under the limit
    assert second.status_code == 429  # throttled
    client.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


# --- Commit-then-publish contract (transactional outbox, BRD §10.2 / §22.9) --
# These pin the durability guarantee Phase 3's real publisher depends on: the
# approval (state transition + nonce consumption + audit) commits ATOMICALLY
# BEFORE publish() is invoked. A publish failure must therefore be unable to
# un-consume the nonce or revert the approval — otherwise the signed link would
# go live again and the owner could re-approve, double-publishing the post.
@dataclass
class _FailingPublisher:
    """A :class:`service.PublisherPort` whose ``publish`` always raises.

    Simulates a real LinkedIn outage AFTER the atomic commit. ``idempotency_key``
    defaults so this mock is callable under both the pre-fix signature (no key)
    and the fixed one (key passed) — the test asserts on DURABLE state, not on
    the call shape.
    """

    keys_seen: list[str] = field(default_factory=list)

    def publish(
        self,
        *,
        draft_id: str,
        text: str,
        image_path: str | None,
        scheduled_for: datetime | None,
        idempotency_key: str = "",
    ) -> str:
        self.keys_seen.append(idempotency_key)
        raise RuntimeError("simulated LinkedIn outage after commit")


def _build_client(
    publisher: service.PublisherPort, TestSession: sessionmaker, settings: Settings
) -> TestClient:
    """Wire an app to an ARBITRARY publisher over an existing session factory."""

    @contextmanager
    def session_factory() -> Iterator[Session]:
        session = TestSession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app = create_app(
        settings=settings,
        publisher=publisher,
        session_factory=session_factory,
        rate_limiter=InMemoryRateLimiter(max_requests=1000, window_seconds=60),
    )
    return TestClient(app)


def test_publish_failure_after_commit_keeps_approval_durable() -> None:
    """RED before fix: a publish that raises must NOT revert the committed approval.

    Pre-fix, publish runs before commit, so the raise rolls the transaction back —
    the draft reverts to ``new`` and the nonce is un-consumed, re-opening the
    signed link for a replay (the double-publish window). Post-fix, the approval is
    already durable, so the state stays ``scheduled`` and the nonce stays spent.
    """
    # Arrange — a fresh DB and an app whose publisher always fails.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    settings = Settings(SECRET_HMAC_KEY=_SECRET, TZ="Asia/Kolkata")
    publisher = _FailingPublisher()
    client = _build_client(publisher, TestSession, settings)
    harness = Harness(
        client=client, publisher=publisher, sessionmaker=TestSession, settings=settings
    )
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")

    # Act — approve; the publisher raises AFTER the approval has committed.
    response = client.post("/approve", data={"token": token})

    # Assert — the approval survived the publish failure (durable, fail-closed):
    # scheduled, nonce spent exactly once, and the link cannot be replayed.
    assert response.status_code == 200
    assert _draft_state(harness, draft.id) == service.STATE_SCHEDULED
    assert _used_count(harness) == 1
    replay = client.post("/approve", data={"token": token})
    assert replay.status_code == 400  # nonce already spent — no replay window
    assert _draft_state(harness, draft.id) == service.STATE_SCHEDULED
    assert _used_count(harness) == 1

    client.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_publish_is_invoked_only_after_the_commit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """RED before fix: publish must see the approval already COMMITTED.

    Uses a file-backed SQLite DB so the publisher can open an INDEPENDENT
    connection that only observes committed rows. Pre-fix, publish runs inside the
    still-open transaction, so the independent connection sees ``new``; post-fix it
    sees ``scheduled``, proving publish happens strictly after the commit. It also
    proves the draft id is threaded through as the ``idempotency_key``.
    """
    db_url = f"sqlite:///{tmp_path / 'outbox.db'}"
    engine = create_engine(
        db_url, connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    settings = Settings(SECRET_HMAC_KEY=_SECRET, TZ="Asia/Kolkata")

    @dataclass
    class _CommitCheckingPublisher:
        """Reads the draft's state on a SEPARATE connection at publish time."""

        state_at_publish: list[str] = field(default_factory=list)
        keys_seen: list[str] = field(default_factory=list)

        def publish(
            self,
            *,
            draft_id: str,
            text: str,
            image_path: str | None,
            scheduled_for: datetime | None,
            idempotency_key: str = "",
        ) -> str:
            self.keys_seen.append(idempotency_key)
            probe = create_engine(
                db_url, connect_args={"check_same_thread": False}, future=True
            )
            try:
                probe_session = sessionmaker(bind=probe, class_=Session)()
                try:
                    row = probe_session.get(Draft, uuid.UUID(draft_id))
                    self.state_at_publish.append(row.state if row else "<absent>")
                finally:
                    probe_session.close()
            finally:
                probe.dispose()
            return f"checked:{draft_id}"

    publisher = _CommitCheckingPublisher()
    client = _build_client(publisher, TestSession, settings)
    harness = Harness(
        client=client, publisher=publisher, sessionmaker=TestSession, settings=settings
    )
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")

    # Act
    response = client.post("/approve", data={"token": token})

    # Assert — at publish time the draft was already committed as scheduled, and
    # the draft id was passed as the idempotency key.
    assert response.status_code == 200
    assert publisher.state_at_publish == [service.STATE_SCHEDULED]
    assert publisher.keys_seen == [str(draft.id)]

    client.close()
    engine.dispose()


def test_idempotency_key_is_the_draft_id(harness: Harness) -> None:
    """RED before fix: publish must receive ``idempotency_key == draft id``.

    Phase 3's worker dedupes retries on this key, so the contract requires it to
    be the draft id. Pre-fix, no key is passed (it defaults to "").
    """
    # Arrange
    draft = _seed_draft(harness)
    token = _mint(draft, "approve")

    # Act
    response = harness.client.post("/approve", data={"token": token})

    # Assert
    assert response.status_code == 200
    assert len(harness.publisher.calls) == 1
    assert harness.publisher.calls[0].idempotency_key == str(draft.id)
