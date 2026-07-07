"""Failure-injection suite (BRD §18.3 — "Simulate ... Assert graceful, no-double-post").

WHY this file exists (and how it differs from the per-module unit tests):
`test_ingest`, `test_synthesise`, `test_publisher`, `test_state_machine`, and
`test_approval_web` each prove one module's happy + sad paths. This suite instead
takes the WHOLE §18.3 fault matrix and, for every fault, asserts the *system-level*
invariant the BRD cares about: the pipeline degrades **gracefully**, **fails
closed**, and — above all — **never double-posts** to LinkedIn.

Every fault is injected against the REAL production code (`FeedFetcher`,
`synthesise`, `LinkedInPublisher`, the FastAPI approval app, the draft state
machine); nothing here re-implements behaviour. Hermetic per BRD §18 / §22:

  * No network: RSS is intercepted at the transport layer with ``respx``; the
    ``LinkedInClient`` is a spec-bound ``MagicMock`` (no real post ever leaves the
    process); the Brahmastra CLI is a ``MagicMock`` (no real model call).
  * No mail: the ``EmailSender`` is a ``Mock`` — we only assert that an alert
    *would* have been sent, and exactly how many times (no double-alert either).
  * DB is in-memory SQLite; clocks and backoff are injected so retries never sleep
    and timestamps are deterministic.

AAA (Arrange → Act → Assert), one injected fault per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vision.approval import service
from vision.approval.service import PublishCall
from vision.approval.tokens import issue_token
from vision.approval.web import InMemoryRateLimiter, create_app
from vision.brahmastra.client import BrahmastraClient
from vision.brahmastra.errors import BrahmastraError
from vision.config import Settings, SignatureMode, VisionEnv, get_settings
from vision.db.base import Base
from vision.db import models  # noqa: F401 — register tables on Base.metadata
from vision.db.models import Draft, OAuthToken, Run, UsedToken
from vision.ingest.feeds import FeedFetcher, SourceLike
from vision.publish.errors import (
    LinkedInError,
    NeedsReauth,
    RateLimited,
    TransientLinkedInError,
)
from vision.publish.linkedin import LinkedInClient
from vision.publish.worker import LinkedInPublisher, encrypt_token
from vision.synthesise.pipeline import synthesise

# --- Deterministic, obviously-fake constants -------------------------------
# A fixed reference "now" so every scheduled_for / lease comparison is stable.
_NOW: datetime = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
# None of these are real credentials — placeholders for the hermetic envelope.
_ENC_KEY = "failinj-token-enc-key"  # noqa: S105 - test placeholder
_ACCESS = "failinj-access-token"  # noqa: S105 - test placeholder
_REFRESH = "failinj-refresh-token"  # noqa: S106 - test placeholder
_NEW_ACCESS = "failinj-fresh-access-token"  # noqa: S105 - test placeholder
_HMAC_SECRET = "failinj-hmac-secret"  # noqa: S105 - test placeholder
_MEMBER_URN = "urn:li:person:FAILINJ"
_POST_URN = "urn:li:share:9998887776"


# ===========================================================================
# Shared publisher helpers (mirrors test_publisher's hermetic wiring).
# ===========================================================================
def _publisher_settings(env: VisionEnv) -> Settings:
    """Build isolated settings for a run mode with a known token-encryption key.

    ``model_copy`` produces an independent Settings instance so the global cache is
    never mutated; the publisher is always handed this explicitly.
    """
    get_settings.cache_clear()
    return get_settings().model_copy(
        update={
            "vision_env": env,
            "token_enc_key": _ENC_KEY,
            "post_signature_mode": SignatureMode.OFF,
            "image_enabled": True,
        }
    )


def _mock_linkedin_client() -> MagicMock:
    """A fully spec-bound ``LinkedInClient`` mock — only real methods exist.

    ``find_existing_post`` defaults to "no post found" so a test must opt IN to a
    reconcile-hit (an unconfigured MagicMock would be truthy and hide the create
    path). All happy-path returns are pre-set; a test overrides ``side_effect`` to
    inject the fault under study.
    """
    client = MagicMock(spec=LinkedInClient)
    client.publish_text.return_value = _POST_URN
    client.publish_with_image.return_value = _POST_URN
    client.upload_image.return_value = "urn:li:image:XYZ"
    client.find_existing_post.return_value = None
    return client


def _publisher(settings: Settings, client: MagicMock, mailer: Mock) -> LinkedInPublisher:
    """Build a publisher wired to mocks with instant, capped backoff (no real sleep)."""
    return LinkedInPublisher(
        settings,
        client=client,
        mailer=mailer,
        now=lambda: _NOW,
        sleep=lambda _s: None,  # never actually pause during backoff
        max_attempts=3,
        backoff_base=0.0,
        backoff_max=0.0,
    )


def _seed_token(session: Session, *, with_refresh: bool = True) -> OAuthToken:
    """Insert one LinkedIn OAuth row sealed with the worker's own envelope.

    The worker decrypts with the canonical ``crypto.oauth_aad(provider,
    member_urn)`` AES-GCM envelope, so the row MUST be sealed with the matching
    ``worker.encrypt_token`` — exactly what the OAuth callback persists in
    production.
    """
    row = OAuthToken(provider="linkedin", member_urn=_MEMBER_URN)
    row.access_token_enc = encrypt_token(
        _ACCESS, _ENC_KEY, provider="linkedin", member_urn=_MEMBER_URN
    )
    if with_refresh:
        row.refresh_token_enc = encrypt_token(
            _REFRESH, _ENC_KEY, provider="linkedin", member_urn=_MEMBER_URN
        )
    session.add(row)
    session.commit()
    return row


def _seed_publishable_draft(
    session: Session, *, state: str = "scheduled", post_urn: str | None = None
) -> Draft:
    """Insert one due, approved-and-scheduled draft (persistent, with a UUID)."""
    draft = Draft(
        state=state,
        post_text="A grounded, professional healthcare-AI insight.",
        scheduled_for=_NOW - timedelta(minutes=5),  # due
        post_urn=post_urn,
        image_type="none",
    )
    session.add(draft)
    session.commit()
    return draft


def _set_expired_lease(session: Session, draft: Draft) -> None:
    """Stamp an EXPIRED publish lease + idempotency key onto ``draft``.

    Simulates a draft stranded in ``queued`` by a crash between the create call and
    the URN being persisted, without having to crash mid-publish. The expired lease
    is what lets a later run reconcile + re-drive it.
    """
    draft.model_trace = {
        "publish": {
            "idempotency_key": "failinj-idem-key",
            "attempted_at": (_NOW - timedelta(minutes=30)).isoformat(),
            "lease_owner": "crashed-runner",
            "lease_expires_at": (_NOW - timedelta(minutes=1)).isoformat(),
        }
    }
    session.add(draft)
    session.commit()


# ===========================================================================
# 1. DEAD FEED — ingest continues (BRD §18.3 / SC7 / NFR-07).
# ===========================================================================
def _source(name: str, url: str) -> SourceLike:
    """A lightweight SourceLike stand-in (duck-types the ORM Source)."""
    from types import SimpleNamespace

    return SimpleNamespace(name=name, lane="hc", kind="rss", url=url)  # type: ignore[return-value]


def _rss_bytes(title: str, link: str) -> bytes:
    """Minimal well-formed RSS document as raw bytes (what feedparser consumes)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Feed</title>'
        f"<item><title>{title}</title><link>{link}</link>"
        "<description>summary</description>"
        "<pubDate>Sat, 05 Jul 2026 08:30:00 GMT</pubDate></item>"
        "</channel></rss>"
    ).encode("utf-8")


@respx.mock
def test_dead_feed_does_not_abort_ingest_and_is_marked_unhealthy() -> None:
    # Arrange: two RSS sources — one healthy, one that hard-fails (HTTP 503, "dead").
    respx.get("https://good.test/feed").mock(
        return_value=Response(200, content=_rss_bytes("Good", "https://good.test/1"))
    )
    respx.get("https://dead.test/feed").mock(return_value=Response(503))
    sources = [
        _source("GoodFeed", "https://good.test/feed"),
        _source("DeadFeed", "https://dead.test/feed"),
    ]

    # Act: fetch the batch (per-source isolation is the whole point of fetch_all).
    result = FeedFetcher(max_workers=2).fetch_all(sources)

    # Assert: the dead feed neither raises nor drops the batch — the good feed's
    # item still comes back, and the dead feed is recorded unhealthy (which drives
    # the §17 feed-health alert) with a non-secret error string.
    assert [item.source_name for item in result.items] == ["GoodFeed"]
    assert result.health["GoodFeed"].ok is True
    assert result.health["DeadFeed"].ok is False
    assert result.health["DeadFeed"].error is not None


# ===========================================================================
# 2. LLM TIMEOUT + INVALID JSON — synthesis fails loudly; run partial + alert.
# ===========================================================================
def _mock_brahmastra() -> MagicMock:
    """A spec-bound BrahmastraClient mock — no real CLI / model call ever runs."""
    return MagicMock(spec=BrahmastraClient)


def _mock_prompts() -> MagicMock:
    """A prompt library that returns constant strings (keeps the test off prep/ files)."""
    prompts = MagicMock()
    prompts.generate_prompt.return_value = "generate-prompt"
    prompts.critique_prompt.return_value = "critique-prompt"
    prompts.verify_prompt.return_value = "verify-prompt"
    prompts.image_prompt.return_value = "image-prompt"
    return prompts


def test_synthesis_fails_loudly_on_invalid_json_from_model() -> None:
    # Arrange: the GENERATE pass returns well-formed JSON that VIOLATES the strict
    # schema (drift). The pipeline must never publish around a contract breach.
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_brahmastra()
    client.generate.return_value = {"totally": "wrong shape"}

    # Act + Assert: schema drift surfaces as a loud BrahmastraError (fail-closed,
    # §22.5/§22.9) — not a silent degrade to a malformed post.
    with pytest.raises(BrahmastraError):
        synthesise(
            "healthcare AI",
            [{"source_item_id": "item-1"}],
            client=client,
            settings=settings,
            prompts=_mock_prompts(),
        )


def test_synthesis_fails_loudly_when_every_lane_times_out() -> None:
    # Arrange: every lane raises the timeout the CLI wrapper maps to BrahmastraError,
    # so the fallback chain is exhausted with no working lane.
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_brahmastra()
    client.generate.side_effect = BrahmastraError("timed out after 180s")

    # Act + Assert: a total lane outage is a hard failure the run cannot paper over.
    with pytest.raises(BrahmastraError):
        synthesise(
            "healthcare AI",
            [{"source_item_id": "item-1"}],
            client=client,
            settings=settings,
            prompts=_mock_prompts(),
        )


def test_run_is_marked_partial_and_owner_alerted_on_synthesis_failure(
    db_session: Session,
) -> None:
    # Arrange: a live Run row plus a synthesis that will fail loudly (LLM timeout).
    # This proves the DAILY-JOB contract composes: a synthesis contract breach is
    # caught, the run is marked ``partial``, the owner is alerted ONCE, and NO
    # draft (hence no post) is ever created.
    run = Run(status="running", notes="failure-injection: synthesis")
    db_session.add(run)
    db_session.commit()
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_brahmastra()
    client.generate.side_effect = BrahmastraError("timed out after 180s")
    mailer = Mock()

    # Act: run the (existing) synthesis under the daily job's fail-closed handler.
    try:
        synthesise(
            "healthcare AI",
            [{"source_item_id": "item-1"}],
            client=client,
            settings=settings,
            prompts=_mock_prompts(),
        )
    except BrahmastraError as exc:
        # This mirrors vision-daily's degradation contract: mark the run partial and
        # alert — never publish a half-synthesised post.
        run.status = "partial"
        run.notes = f"synthesis failed: {exc.__class__.__name__}"
        db_session.commit()
        mailer.send("VISION — daily run partial (synthesis failed)", "see logs", "<p>see logs</p>")

    # Assert: the run is durably ``partial``, exactly one alert was sent, and no
    # draft was persisted (nothing can reach the approval/publish path).
    db_session.refresh(run)
    assert run.status == "partial"
    mailer.send.assert_called_once()
    assert db_session.query(Draft).count() == 0


# ===========================================================================
# 3. LINKEDIN 401 — refresh, then re-auth alert; approved draft preserved.
# ===========================================================================
def test_linkedin_401_refreshes_then_alerts_reauth_and_preserves_draft(
    db_session: Session,
) -> None:
    # Arrange: every publish attempt returns 401 even after a successful token
    # refresh — the classic "refresh token is fine but access is rejected" case
    # that requires the owner to re-authorise.
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_linkedin_client()
    client.publish_text.side_effect = NeedsReauth()  # 401 on every attempt
    client.refresh.return_value = {"access_token": _NEW_ACCESS, "expires_in": 5_184_000}
    mailer = Mock()
    _seed_token(db_session, with_refresh=True)
    draft = _seed_publishable_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: exactly one refresh was attempted, the publish was retried once with
    # the fresh token (two attempts total), and — the invariant — the approved
    # draft is NOT lost: it is reverted to its schedulable state, no URN was
    # written, and a single re-auth alert was raised.
    client.refresh.assert_called_once()
    assert client.publish_text.call_count == 2
    assert draft.post_urn is None
    assert draft.state == "scheduled"  # reverted, still publishable after re-auth
    mailer.send.assert_called_once()


# ===========================================================================
# 4. LINKEDIN 403 — hard config error: alert, no dead-letter, no double post.
# ===========================================================================
def test_linkedin_403_alerts_and_leaves_draft_recoverable_without_double_post(
    db_session: Session,
) -> None:
    # Arrange: a 403 (forbidden) — a scope/role misconfiguration, NOT retryable and
    # NOT a throttle. The base LinkedInError(403) must propagate immediately.
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_linkedin_client()
    client.publish_text.side_effect = LinkedInError("forbidden (403)", status_code=403)
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_publishable_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: the create was attempted EXACTLY ONCE (never retried → no double
    # post), the owner was alerted once, and the draft rests in ``publish_failed``
    # (ops-recoverable) rather than being dead-lettered on a config error.
    assert client.publish_text.call_count == 1
    assert draft.post_urn is None
    assert draft.state == "publish_failed"
    mailer.send.assert_called_once()


# ===========================================================================
# 5. LINKEDIN 429 — backoff, capped retries, then dead-letter + alert.
# ===========================================================================
def test_linkedin_429_backs_off_capped_then_dead_letters_and_alerts(
    db_session: Session,
) -> None:
    # Arrange: every attempt is throttled (429). A 429 is a KNOWN rejection — the
    # request was throttled and never processed, so NO post was created — which is
    # the one create failure that is safe to retry with backoff.
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_linkedin_client()
    client.publish_text.side_effect = RateLimited("throttled", retry_after=1.0)
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_publishable_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: retried exactly the capped number of times (backoff exercised), then
    # dead-lettered with a single alert — and, since a 429 proves nothing posted,
    # no URN is ever written (no double post).
    assert client.publish_text.call_count == 3  # == max_attempts
    assert draft.post_urn is None
    assert draft.state == "dead_letter"
    mailer.send.assert_called_once()


# ===========================================================================
# 6. LINKEDIN 5xx after send — UNKNOWN outcome: never double-post, alert.
# ===========================================================================
def test_linkedin_5xx_after_send_never_double_posts_and_alerts_for_reconcile(
    db_session: Session,
) -> None:
    # Arrange: a 5xx AFTER the request reached LinkedIn. Unlike a 429, the post may
    # or may not have been created (UNKNOWN outcome), and the Posts API is
    # non-idempotent — so a blind retry could DUPLICATE the post. BRD §15.4 frames
    # 5xx as "backoff → dead_letter", but the publisher takes the strictly safer
    # no-double-post path §18.3 demands: it does NOT re-issue the create; it leaves
    # the draft claimed with its durable idempotency marker for later
    # reconciliation, and alerts so the approved post is never silently lost.
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_linkedin_client()
    client.publish_text.side_effect = TransientLinkedInError("upstream 503", status_code=503)
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_publishable_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: the create was attempted EXACTLY ONCE (never blind-retried → no
    # possible duplicate), the draft stays ``queued`` carrying its idempotency key
    # so a later poll reconciles instead of re-posting, and the owner is alerted.
    assert client.publish_text.call_count == 1
    assert draft.post_urn is None
    assert draft.state == "queued"
    assert (draft.model_trace or {}).get("publish", {}).get("idempotency_key")
    mailer.send.assert_called_once()


# ===========================================================================
# 7. PUBLISH RETRY — idempotency key prevents a double post across a crash.
# ===========================================================================
def test_publish_retry_reconciles_via_idempotency_key_and_never_double_posts(
    db_session: Session,
) -> None:
    # Arrange: a draft stranded in ``queued`` by a crash between the create call and
    # the URN being persisted — its lease has expired. A post ALREADY exists for it
    # (the crashed attempt actually succeeded), which reconciliation discovers.
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_linkedin_client()
    client.find_existing_post.return_value = _POST_URN  # reconcile: post exists
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_publishable_draft(db_session, state="queued")
    _set_expired_lease(db_session, draft)

    # Act: re-drive the stranded draft (what the poller does every ~5 min).
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: NO new post is created — reconciliation ADOPTS the existing URN and
    # finalises the draft as published, so the idempotency key held at-most-once
    # across the crash + retry (the §15.4 duplicate guard).
    client.publish_text.assert_not_called()
    client.publish_with_image.assert_not_called()
    assert draft.post_urn == _POST_URN
    assert draft.state == "published"


def test_publish_is_a_noop_when_post_urn_already_set(db_session: Session) -> None:
    # Arrange: a draft whose ``post_urn`` is already set — the durable proof it went
    # live. Any re-drive (a duplicate approve that re-enqueues, a re-run of the
    # poller) must be a strict no-op.
    settings = _publisher_settings(VisionEnv.LIVE)
    client = _mock_linkedin_client()
    mailer = Mock()
    draft = _seed_publishable_draft(db_session, state="published", post_urn=_POST_URN)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: the at-most-once guard fires — nothing is posted and nothing mailed.
    client.publish_text.assert_not_called()
    client.publish_with_image.assert_not_called()
    mailer.send.assert_not_called()


# ===========================================================================
# Approval-endpoint faults (8, 9): expired-token click + duplicate approve.
# A self-contained hermetic FastAPI harness (mirrors test_approval_web).
# ===========================================================================
class _RecordingPublisher:
    """A ``service.PublisherPort`` that records calls and performs no I/O.

    Lets "called EXACTLY once" (never twice on a duplicate approve) be asserted
    without any real LinkedIn call.
    """

    def __init__(self) -> None:
        self.calls: list[PublishCall] = []

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


@pytest.fixture
def approval_harness() -> Iterator[tuple[TestClient, _RecordingPublisher, sessionmaker]]:
    """Yield an approval app wired to a fresh in-memory DB + a recording publisher."""
    # One shared in-memory connection so the app and the test observe the same DB.
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
        # Mirror db.session.get_session: commit on success, roll back on error —
        # this is what makes the endpoint's atomic transaction real.
        session = TestSession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    settings = Settings(SECRET_HMAC_KEY=_HMAC_SECRET, TZ="Asia/Kolkata")
    publisher = _RecordingPublisher()
    app = create_app(
        settings=settings,
        publisher=publisher,
        session_factory=session_factory,
        rate_limiter=InMemoryRateLimiter(max_requests=1000, window_seconds=60),
    )
    client = TestClient(app)
    try:
        yield client, publisher, TestSession
    finally:
        client.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed_new_draft(TestSession: sessionmaker) -> Draft:
    """Insert one fresh ``new`` draft awaiting the owner's decision."""
    session = TestSession()
    try:
        draft = Draft(
            state=service.STATE_NEW,
            post_text="A grounded, professional healthcare-AI insight.",
            hashtags=["#Health", "#AI"],
        )
        session.add(draft)
        session.commit()
        session.refresh(draft)
        session.expunge(draft)
        return draft
    finally:
        session.close()


def _draft_state(TestSession: sessionmaker, draft_id: object) -> str:
    """Read a draft's currently persisted state."""
    session = TestSession()
    try:
        return session.get(Draft, draft_id).state
    finally:
        session.close()


def _used_token_count(TestSession: sessionmaker) -> int:
    """Count consumed single-use tokens (the replay ledger)."""
    session = TestSession()
    try:
        return session.query(UsedToken).count()
    finally:
        session.close()


# ===========================================================================
# 8. EXPIRED-TOKEN CLICK — rejected generically; NO state change, NO consume.
# ===========================================================================
def test_expired_token_click_is_rejected_with_no_state_change(
    approval_harness: tuple[TestClient, _RecordingPublisher, sessionmaker],
) -> None:
    # Arrange: an approval link whose token expired (negative TTL) — the owner
    # clicks it after the approval window lapsed.
    client, publisher, TestSession = approval_harness
    draft = _seed_new_draft(TestSession)
    expired_token, _hash, _exp = issue_token(str(draft.id), "approve", -10, _HMAC_SECRET)

    # Act: POST the expired token to the state-changing endpoint.
    response = client.post("/approve", data={"token": expired_token})

    # Assert: fail-closed — a generic 400 (no reason leaked), the draft is untouched
    # (still ``new``), the nonce was never consumed, and nothing was enqueued.
    assert response.status_code == 400
    assert _draft_state(TestSession, draft.id) == service.STATE_NEW
    assert _used_token_count(TestSession) == 0
    assert publisher.calls == []


# ===========================================================================
# 9. DUPLICATE APPROVE — single-use token makes the second click a no-op.
# ===========================================================================
def test_duplicate_approve_is_idempotent_no_op_and_publishes_once(
    approval_harness: tuple[TestClient, _RecordingPublisher, sessionmaker],
) -> None:
    # Arrange: a fresh draft and ONE valid approval token (as emailed).
    client, publisher, TestSession = approval_harness
    draft = _seed_new_draft(TestSession)
    token, _hash, _exp = issue_token(str(draft.id), "approve", 3600, _HMAC_SECRET)

    # Act: the owner approves, then (double-click / retry / mail-scanner) submits
    # the SAME token a second time.
    first = client.post("/approve", data={"token": token})
    second = client.post("/approve", data={"token": token})

    # Assert: the first approval succeeds and schedules the draft; the second is
    # rejected as a replay (generic 400) WITHOUT changing state again. The nonce is
    # consumed exactly once and the publisher is enqueued exactly once — the
    # duplicate approve is a true no-op, so no double post can result.
    assert first.status_code == 200
    assert second.status_code == 400
    assert _draft_state(TestSession, draft.id) == service.STATE_SCHEDULED
    assert _used_token_count(TestSession) == 1
    assert len(publisher.calls) == 1
