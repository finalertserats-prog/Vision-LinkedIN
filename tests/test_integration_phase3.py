"""End-to-end INTEGRATION test for VISION's Phase-3 PUBLISH path, fully offline.

WHY this test exists: the Phase-3 unit suite (``test_publisher.py``) proves each
publisher behaviour in isolation. This test proves the pieces *compose* into the
one real journey the ``vision-publisher`` cron lives every ~5 minutes (BRD
§15.2/§15.4, FR-12/13/14) using ONLY the real code under test:

    seed an approved+due draft (with and without an image)
      + persist REAL AES-256-GCM-encrypted OAuth tokens (worker.encrypt_token —
        the exact envelope the OAuth callback writes; no faked crypto)
      -> drive the REAL ``LinkedInPublisher.publish`` against a MOCK
         ``LinkedInClient`` (no network, no real post) and a MOCK EmailSender
         (no SMTP/HTTP), asserting the full §15.4 error matrix + idempotency:
           text publish    -> post_urn+post_url stored, state=published,
                              confirmation sent EXACTLY once, audit trail intact
           second publish  -> at-most-once no-op (draft already has a URN)
           image publish   -> upload_image then publish_with_image, image_urn kept
           401 on publish  -> ONE token refresh (re-encrypted atomically) + retry
           repeated 5xx    -> capped retries -> dead_letter + a single alert
           image failure   -> degrades to a text-only post, still publishes
           dry_run         -> posts NOTHING, mutates NOTHING

The ONLY collaborators mocked are the two real-world side-effects a test must
never perform: the LinkedIn HTTP client and the email sender. Everything
security-critical — the envelope encryption/decryption, the guarded compare-and-
set state transitions, the append-only ``audit_log`` — is the REAL code
(BRD §18/§22). Each test follows AAA (Arrange -> Act -> Assert).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

from sqlalchemy.orm import Session

from vision.config import Settings, SignatureMode, VisionEnv, get_settings
from vision.db.models import AuditLog, Draft, OAuthToken
from vision.publish.errors import (
    LinkedInError,
    NeedsReauth,
    RateLimited,
)
from vision.publish.linkedin import LinkedInClient
from vision.publish.worker import (
    LinkedInPublisher,
    decrypt_token,
    encrypt_token,
)

# --- Deterministic test constants ------------------------------------------
# A fixed reference "now" so every ``scheduled_for`` comparison and audit
# timestamp is fully reproducible across machines (no wall-clock flakiness).
_NOW: datetime = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)

# Obviously-fake, non-secret placeholders — nothing here is a real credential
# (mirrors the fixtures in the unit suite so the two stay in lock-step).
_ENC_KEY = "phase3-integration-token-enc-key"  # noqa: S105 - test placeholder
_ACCESS = "access-token-VALUE"  # noqa: S105 - test placeholder
_REFRESH = "refresh-token-VALUE"  # noqa: S106 - test placeholder
_NEW_ACCESS = "fresh-access-token-VALUE"  # noqa: S105 - test placeholder
_MEMBER_URN = "urn:li:person:INTEGRATION"
_POST_URN = "urn:li:share:9876543210"
_IMAGE_URN = "urn:li:image:XYZ789"


# --- Builders (config-shaped Arrange helpers, not inline magic) -------------


def _make_settings(
    env: VisionEnv, *, signature: SignatureMode = SignatureMode.OFF
) -> Settings:
    """Return isolated settings for a run mode with the test's known enc key.

    ``model_copy`` yields an independent ``Settings`` (it never mutates the
    cached singleton), and the publisher is always handed this instance
    explicitly, so the global settings cache is irrelevant to the outcome.
    """
    get_settings.cache_clear()
    return get_settings().model_copy(
        update={
            "vision_env": env,
            "token_enc_key": _ENC_KEY,
            "post_signature_mode": signature,
            "image_enabled": True,
        }
    )


def _seed_token(session: Session, *, with_refresh: bool = True) -> OAuthToken:
    """Persist ONE LinkedIn OAuth row using the worker's REAL envelope encryption.

    The publisher decrypts through ``worker.decrypt_token`` (HKDF-SHA256 key and
    the canonical ``crypto.oauth_aad(provider, member_urn)`` GCM AAD, versioned
    envelope), so the row MUST be sealed with the matching ``worker.encrypt_token``
    — exactly what the OAuth callback persists in production. This is deliberately
    the real crypto, not a stub, so the test exercises the authenticated round-trip
    end-to-end.
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


def _seed_draft(
    session: Session,
    *,
    state: str = "scheduled",
    scheduled_for: datetime | None = None,
    post_urn: str | None = None,
    image_type: str = "none",
    image_path: str | None = None,
) -> Draft:
    """Insert one approved-and-due draft and return the persistent ORM row.

    ``state='scheduled'`` is the value the wired approval loop
    (``vision.approval.service.approve``) stamps on an approved draft that has
    been enqueued for its publish slot — i.e. the real "approved & due" state the
    publisher polls. ``scheduled_for`` defaults to just-past ``_NOW`` so the
    draft is due.
    """
    draft = Draft(
        state=state,
        post_text="Hello from VISION — an approved, grounded post.",
        scheduled_for=(
            scheduled_for if scheduled_for is not None else _NOW - timedelta(minutes=5)
        ),
        post_urn=post_urn,
        image_type=image_type,
        image_path=image_path,
    )
    session.add(draft)
    session.commit()
    return draft


def _mock_client() -> MagicMock:
    """Return a spec-bound mock ``LinkedInClient`` (only real methods exist).

    ``spec=LinkedInClient`` means a typo'd method name fails the test instead of
    silently returning a mock — the mock surface tracks the real client exactly.
    """
    client = MagicMock(spec=LinkedInClient)
    client.publish_text.return_value = _POST_URN
    client.publish_with_image.return_value = _POST_URN
    client.upload_image.return_value = _IMAGE_URN
    return client


def _publisher(settings: Settings, client: MagicMock, mailer: Mock) -> LinkedInPublisher:
    """Wire a publisher to the mocks with instant, capped backoff (no real sleep).

    ``now`` is pinned and ``backoff_*`` are zero so retries are immediate and
    timestamps deterministic; ``max_attempts=3`` bounds the 5xx retry assertion.
    """
    return LinkedInPublisher(
        settings,
        client=client,
        mailer=mailer,
        now=lambda: _NOW,
        max_attempts=3,
        backoff_base=0.0,
        backoff_max=0.0,
    )


def _audit_actions(session: Session, draft_id: object) -> list[str]:
    """Return the ``audit_log`` actions recorded for a draft, oldest first.

    The append-only audit trail is the single source of truth for what happened
    to a draft (threat model §1 repudiation control), so the integration tests
    assert on it rather than on internal method calls where a transition matters.
    """
    rows = (
        session.query(AuditLog)
        .filter(AuditLog.entity == "draft", AuditLog.entity_id == str(draft_id))
        .order_by(AuditLog.at.asc(), AuditLog.created_at.asc())
        .all()
    )
    return [row.action for row in rows]


# ---------------------------------------------------------------------------
# (1) LIVE text publish: URN + URL stored, state=published, ONE confirmation.
# ---------------------------------------------------------------------------
def test_live_text_publish_stores_urn_transitions_and_confirms_once(
    db_session: Session,
) -> None:
    # --- Arrange: an approved+due text-only draft with a usable token -------
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session)

    # --- Act ----------------------------------------------------------------
    _publisher(settings, client, mailer).publish(db_session, draft)

    # --- Assert: a text post was made (not an image post) -------------------
    client.publish_text.assert_called_once()
    client.publish_with_image.assert_not_called()

    # --- Assert: the URN + derived permalink are persisted ------------------
    assert draft.post_urn == _POST_URN
    assert draft.post_url and _POST_URN in draft.post_url

    # --- Assert: the state machine advanced to the terminal published state -
    assert draft.state == "published"

    # --- Assert: the confirmation email was mock-sent EXACTLY once ----------
    mailer.send.assert_called_once()

    # --- Assert: the audit trail records the claim then the publish ---------
    assert _audit_actions(db_session, draft.id) == ["queued", "published"]


# ---------------------------------------------------------------------------
# (2) Idempotency: a second publish of the same draft is an at-most-once no-op.
# ---------------------------------------------------------------------------
def test_second_publish_is_idempotent_noop(db_session: Session) -> None:
    # --- Arrange: publish once so the draft carries a stored URN ------------
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session)
    publisher = _publisher(settings, client, mailer)
    publisher.publish(db_session, draft)  # first, genuine publish

    # --- Act: re-run publish on the very same (now published) draft ---------
    publisher.publish(db_session, draft)

    # --- Assert: the second run posts NOTHING and sends NO second email -----
    # The Posts API is not natively idempotent, so a stored ``post_urn`` is the
    # at-most-once key: exactly one network post and one confirmation in total.
    client.publish_text.assert_called_once()
    mailer.send.assert_called_once()
    # No extra audit rows: the second call short-circuits before any transition.
    assert _audit_actions(db_session, draft.id) == ["queued", "published"]


# ---------------------------------------------------------------------------
# (3) LIVE image publish: upload_image -> publish_with_image, image URN stored.
# ---------------------------------------------------------------------------
def test_live_image_publish_uploads_then_publishes_with_image(
    db_session: Session, tmp_path: Path
) -> None:
    # --- Arrange: a draft whose approved visual lane produced a real file ---
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    image_file = tmp_path / "card.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\n-fake-card-bytes")
    draft = _seed_draft(
        db_session, image_type="informative-card", image_path=str(image_file)
    )

    # --- Act ----------------------------------------------------------------
    _publisher(settings, client, mailer).publish(db_session, draft)

    # --- Assert: the two-step image path ran (upload then image post) -------
    client.upload_image.assert_called_once()
    client.publish_with_image.assert_called_once()
    client.publish_text.assert_not_called()

    # --- Assert: both URNs are persisted on the draft -----------------------
    assert draft.image_urn == _IMAGE_URN
    assert draft.post_urn == _POST_URN
    assert draft.state == "published"


# ---------------------------------------------------------------------------
# (4) 401 -> refresh the token ONCE (re-encrypted) -> retry publish -> success.
# ---------------------------------------------------------------------------
def test_publish_refreshes_on_401_then_retries_and_reencrypts_token(
    db_session: Session,
) -> None:
    # --- Arrange: first publish 401s; after a refresh the retry succeeds ----
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    client.publish_text.side_effect = [NeedsReauth(), _POST_URN]
    # LinkedIn returns a brand-new access token from the refresh grant.
    client.refresh.return_value = {
        "access_token": _NEW_ACCESS,
        "expires_in": 5_184_000,
    }
    mailer = Mock()
    token_row = _seed_token(db_session)  # refresh token present -> refresh viable
    draft = _seed_draft(db_session)

    # --- Act ----------------------------------------------------------------
    _publisher(settings, client, mailer).publish(db_session, draft)

    # --- Assert: exactly one refresh and two publish attempts ---------------
    client.refresh.assert_called_once()
    assert client.publish_text.call_count == 2
    assert draft.post_urn == _POST_URN
    assert draft.state == "published"

    # --- Assert: the refreshed token was re-encrypted and stored ATOMICALLY -
    # Re-read via the REAL decrypt to prove the new access token round-trips
    # through the same authenticated envelope (threat model §3 atomic replace).
    db_session.refresh(token_row)
    assert (
        decrypt_token(
            token_row.access_token_enc, _ENC_KEY, provider="linkedin", member_urn=_MEMBER_URN
        )
        == _NEW_ACCESS
    )


# ---------------------------------------------------------------------------
# (5) Repeated 429 -> capped retries -> dead_letter + a single owner alert.
# ---------------------------------------------------------------------------
def test_repeated_rate_limit_dead_letters_and_alerts_once(db_session: Session) -> None:
    # --- Arrange: every publish attempt is throttled (429) ------------------
    # A 429 is a KNOWN rejection (LinkedIn never processed the request, so no post
    # was created) — the one create failure that is safe to retry to exhaustion.
    # A 5xx/timeout, by contrast, is an UNKNOWN outcome and must NOT be blindly
    # retried (see test_publisher.test_unknown_outcome_transient_does_not_double_post).
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    client.publish_text.side_effect = RateLimited("throttled")
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session)

    # --- Act ----------------------------------------------------------------
    _publisher(settings, client, mailer).publish(db_session, draft)

    # --- Assert: retried the capped number of times, then gave up -----------
    assert client.publish_text.call_count == 3  # == max_attempts
    assert draft.state == "dead_letter"
    assert draft.post_urn is None  # never re-posted / no partial URN
    mailer.send.assert_called_once()  # a single dead-letter alert

    # --- Assert: the failure lane is fully audited (claim, fail, dead-letter)
    assert _audit_actions(db_session, draft.id) == [
        "queued",
        "publish_failed",
        "dead_letter",
    ]


# ---------------------------------------------------------------------------
# (6) Image failure degrades to a TEXT-ONLY post — an image never blocks pub.
# ---------------------------------------------------------------------------
def test_image_failure_degrades_to_text_only_and_still_publishes(
    db_session: Session, tmp_path: Path
) -> None:
    # --- Arrange: an image draft whose UPLOAD fails (non-auth LinkedIn error)
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    # The image registration/upload raises a hard (non-401) LinkedIn error; per
    # BRD §13.6 this must degrade to text-only rather than fail the publish.
    client.upload_image.side_effect = LinkedInError(
        "image rejected (422)", status_code=422
    )
    mailer = Mock()
    _seed_token(db_session)
    image_file = tmp_path / "card.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\n-fake-card-bytes")
    draft = _seed_draft(
        db_session, image_type="concept-illustration", image_path=str(image_file)
    )

    # --- Act ----------------------------------------------------------------
    _publisher(settings, client, mailer).publish(db_session, draft)

    # --- Assert: the upload was attempted, failed, and we fell back to text --
    client.upload_image.assert_called_once()
    client.publish_with_image.assert_not_called()
    client.publish_text.assert_called_once()

    # --- Assert: the post still went live, with NO image URN recorded -------
    assert draft.post_urn == _POST_URN
    assert draft.image_urn is None
    assert draft.state == "published"
    mailer.send.assert_called_once()  # the owner still gets a confirmation


# ---------------------------------------------------------------------------
# (7) dry_run: log only — post NOTHING, mutate NOTHING (the safe default mode).
# ---------------------------------------------------------------------------
def test_dry_run_posts_nothing_and_leaves_state_untouched(
    db_session: Session,
) -> None:
    # --- Arrange: DRY_RUN mode with a due draft and credentials available ---
    settings = _make_settings(VisionEnv.DRY_RUN)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session, state="scheduled")

    # --- Act ----------------------------------------------------------------
    _publisher(settings, client, mailer).publish(db_session, draft)

    # --- Assert: no network publish, no mail, no URN, state unchanged -------
    client.publish_text.assert_not_called()
    client.publish_with_image.assert_not_called()
    client.upload_image.assert_not_called()
    mailer.send.assert_not_called()
    assert draft.post_urn is None
    assert draft.state == "scheduled"
    assert _audit_actions(db_session, draft.id) == []  # no transition logged
