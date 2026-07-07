"""Unit tests for the real LinkedIn publisher worker (BRD §15.2/§15.4, FR-12/13/14).

WHY these tests / how they stay hermetic (BRD §18 — tests are part of done):
  * ``LinkedInClient`` is a ``MagicMock(spec=LinkedInClient)`` — **no real network
    and no real post** ever leaves the process. Every publish/upload/delete/refresh
    is a mock whose return value or ``side_effect`` we control.
  * The email sender is a ``Mock`` — no mail is sent; we only assert that a
    confirmation / alert *would* have been sent, and exactly how many times.
  * The DB is the in-memory SQLite ``db_session`` fixture from ``conftest``.
  * The clock (``now``) and the backoff (``max_attempts``/``backoff_base``) are
    injected so retries never actually sleep and timestamps are deterministic.

Each test follows AAA (Arrange → Act → Assert) with a single behavioural focus,
covering the required matrix: idempotent no-op, text publish (URN + transition +
one confirmation), image publish, 401→refresh→retry, repeated 5xx→dead-letter+
alert, dry_run posts nothing, staging posts-then-deletes, and due-filtering in
``poll_and_publish``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.orm import Session

from vision.cli import publisher as publisher_cli
from vision.config import Settings, SignatureMode, VisionEnv, get_settings
from vision.db.models import Draft, OAuthToken
from vision.publish.errors import NeedsReauth, RateLimited, TransientLinkedInError
from vision.publish.linkedin import LinkedInClient
from vision.publish.worker import (
    ForbiddenNameInPost,
    LinkedInPublisher,
    encrypt_token,
)

# --- Deterministic test constants ------------------------------------------
# A fixed reference "now" so every scheduled_for comparison is fully deterministic.
_NOW: datetime = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
# Obviously-fake secrets/values — nothing here is a real credential.
_ENC_KEY = "unit-test-token-enc-key"  # noqa: S105 - test placeholder
_ACCESS = "access-token-value"  # noqa: S105 - test placeholder
_REFRESH = "refresh-token-value"  # noqa: S106 - test placeholder
_NEW_ACCESS = "fresh-access-token-value"  # noqa: S105 - test placeholder
_MEMBER_URN = "urn:li:person:TEST"
_POST_URN = "urn:li:share:1234567890"
_IMAGE_URN = "urn:li:image:ABC123"


def _make_settings(env: VisionEnv, *, signature: SignatureMode = SignatureMode.OFF) -> Settings:
    """Build deterministic settings for a given run mode with a known enc key.

    ``model_copy`` produces an isolated Settings instance (no cache mutation); the
    publisher is always handed this explicitly, so the global settings cache is
    irrelevant to the test's behaviour.
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
    """Insert one LinkedIn OAuth row sealed under the CANONICAL crypto contract.

    The worker decrypts via ``vision.publish.worker.decrypt_token``, which derives
    its key with HKDF-SHA256 and its AAD from ``(provider, member_urn)`` through the
    shared ``crypto.oauth_aad`` helper — exactly what the OAuth callback persists.
    Seeding through the same ``worker.encrypt_token`` guarantees the round-trip
    mirrors production and cannot drift onto a different scheme.
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
    """Insert one publishable draft row and return it (persistent, with a UUID)."""
    draft = Draft(
        state=state,
        post_text="Hello from VISION.",
        scheduled_for=scheduled_for if scheduled_for is not None else _NOW - timedelta(minutes=5),
        post_urn=post_urn,
        image_type=image_type,
        image_path=image_path,
    )
    session.add(draft)
    session.commit()
    return draft


def _set_publish_lease(
    session: Session,
    draft: Draft,
    *,
    lease_expires_at: datetime,
    idempotency_key: str = "idem-key-TEST",
    lease_owner: str = "some-other-runner",
) -> None:
    """Stamp a durable publish/idempotency lease onto a draft (in ``model_trace``).

    The worker persists the at-most-once idempotency marker + lease (owner +
    expiry) under ``model_trace['publish']`` BEFORE the create call, so a crashed
    or in-flight claim can be reconciled instead of blindly re-posted. Tests seed
    that marker directly to exercise the re-drive / lease-expiry / reaper paths
    without having to crash mid-publish.
    """
    draft.model_trace = {
        "publish": {
            "idempotency_key": idempotency_key,
            "attempted_at": _NOW.isoformat(),
            "lease_owner": lease_owner,
            "lease_expires_at": lease_expires_at.isoformat(),
        }
    }
    session.add(draft)
    session.commit()


def _mock_client() -> MagicMock:
    """Return a fully-mocked ``LinkedInClient`` (spec-bound so only real methods exist)."""
    client = MagicMock(spec=LinkedInClient)
    client.publish_text.return_value = _POST_URN
    client.publish_with_image.return_value = _POST_URN
    client.upload_image.return_value = _IMAGE_URN
    # Reconciliation defaults to "no post found" so a test must opt IN to a
    # reconcile-hit; an unconfigured MagicMock would otherwise be truthy and
    # silently short-circuit the publish path.
    client.find_existing_post.return_value = None
    return client


def _publisher(settings: Settings, client: MagicMock, mailer: Mock) -> LinkedInPublisher:
    """Build a publisher wired to mocks with instant, capped backoff (no real sleep)."""
    return LinkedInPublisher(
        settings,
        client=client,
        mailer=mailer,
        now=lambda: _NOW,
        max_attempts=3,
        backoff_base=0.0,
        backoff_max=0.0,
    )


# --- Idempotency: a draft with a stored URN is never re-posted -------------


def test_publish_is_noop_when_post_urn_already_set(db_session: Session) -> None:
    # Arrange: a draft that already carries a post URN (already published).
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    draft = _seed_draft(db_session, state="published", post_urn=_POST_URN)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: at-most-once guard fires — nothing is posted and no mail is sent.
    client.publish_text.assert_not_called()
    client.publish_with_image.assert_not_called()
    mailer.send.assert_not_called()


# --- LIVE text publish: URN stored, state advanced, ONE confirmation -------


def test_live_text_publish_stores_urn_transitions_and_confirms_once(
    db_session: Session,
) -> None:
    # Arrange: a due, approved text-only draft with a usable token.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: a text post was made, the URN + URL persisted, state is published,
    # and the confirmation email was sent EXACTLY once.
    client.publish_text.assert_called_once()
    assert draft.post_urn == _POST_URN
    assert draft.post_url and _POST_URN in draft.post_url
    assert draft.state == "published"
    mailer.send.assert_called_once()


# --- LIVE image publish: upload → publish_with_image, image URN stored -----


def test_live_image_publish_uploads_and_attaches_image(
    db_session: Session, tmp_path: Path
) -> None:
    # Arrange: an approved draft whose visual lane produced a non-empty image file.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    image_file = tmp_path / "card.png"
    image_file.write_bytes(b"\x89PNG-fake-bytes")
    draft = _seed_draft(
        db_session, image_type="informative-card", image_path=str(image_file)
    )

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: the image was uploaded and the post used the image path (not text-only),
    # and the returned image URN is persisted on the draft.
    client.upload_image.assert_called_once()
    client.publish_with_image.assert_called_once()
    client.publish_text.assert_not_called()
    assert draft.image_urn == _IMAGE_URN
    assert draft.post_urn == _POST_URN
    assert draft.state == "published"


# --- 401 → refresh → retry once (approved draft never lost) -----------------


def test_publish_refreshes_token_on_401_then_retries_successfully(
    db_session: Session,
) -> None:
    # Arrange: the first publish attempt gets a 401; after a token refresh the
    # retry succeeds. A refresh token is present so the refresh path is viable.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    client.publish_text.side_effect = [NeedsReauth(), _POST_URN]
    client.refresh.return_value = {"access_token": _NEW_ACCESS, "expires_in": 5_184_000}
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: exactly one refresh, two publish attempts, and a live post recorded.
    client.refresh.assert_called_once()
    assert client.publish_text.call_count == 2
    assert draft.post_urn == _POST_URN
    assert draft.state == "published"


# --- Repeated 429 → capped retries → dead-letter + alert --------------------


def test_repeated_rate_limit_dead_letters_and_alerts(db_session: Session) -> None:
    # Arrange: every publish attempt is throttled (429). A 429 is a KNOWN rejection
    # — LinkedIn never processed the request, so no post was created — which makes
    # it the one create failure that IS safe to retry. Backoff is 0 so the capped
    # retries never actually sleep.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    client.publish_text.side_effect = RateLimited("throttled")
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: retried exactly the capped number of times, then dead-lettered and
    # the owner alerted — never re-posted (post_urn stays unset).
    assert client.publish_text.call_count == 3
    assert draft.state == "dead_letter"
    assert draft.post_urn is None
    mailer.send.assert_called_once()


# --- ISSUE 6: unknown-outcome (5xx/timeout after send) never double-posts ----


def test_unknown_outcome_transient_does_not_double_post_in_one_run(
    db_session: Session,
) -> None:
    # Arrange: the create call fails with a transient 5xx AFTER the request was
    # sent — LinkedIn may or may not have created the post (UNKNOWN outcome). The
    # Posts API is non-idempotent, so blindly retrying the create would duplicate.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    client.publish_text.side_effect = TransientLinkedInError("boom", status_code=503)
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: the create is attempted EXACTLY ONCE (never blind-retried) so no
    # second post can be created within the run; the draft stays claimed
    # ('queued') carrying its durable idempotency marker for later reconciliation,
    # and the owner is alerted so the approved draft is never silently lost.
    assert client.publish_text.call_count == 1
    assert draft.state == "queued"
    assert draft.post_urn is None
    assert (draft.model_trace or {}).get("publish", {}).get("idempotency_key")
    mailer.send.assert_called_once()


# --- ISSUE 4/5: re-driving a 'queued' draft RECONCILES, never double-posts ---


def test_redriving_queued_draft_reconciles_instead_of_double_posting(
    db_session: Session,
) -> None:
    # Arrange: a draft left stranded in 'queued' by a crash between create and
    # persist, whose lease has since expired. A post ALREADY exists for it (the
    # prior attempt actually succeeded), which reconciliation discovers.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    client.find_existing_post.return_value = _POST_URN  # reconcile: a post exists
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session, state="queued")
    _set_publish_lease(db_session, draft, lease_expires_at=_NOW - timedelta(minutes=1))

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: NO new post is created — reconciliation adopts the existing URN and
    # finalises the draft as published (at-most-once preserved across the crash).
    client.publish_text.assert_not_called()
    client.publish_with_image.assert_not_called()
    assert draft.post_urn == _POST_URN
    assert draft.state == "published"


# --- ISSUE 5: a valid (unexpired) lease blocks a second claimer -------------


def test_valid_lease_blocks_reclaim_of_queued_draft(db_session: Session) -> None:
    # Arrange: a draft in 'queued' whose lease is still held (not expired) — a
    # live runner owns it right now. A second caller must NOT publish it.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session, state="queued")
    _set_publish_lease(db_session, draft, lease_expires_at=_NOW + timedelta(minutes=5))

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: the second caller does nothing — no create, no reconcile — because
    # the lease has not expired (no double-post while another runner is in flight).
    client.publish_text.assert_not_called()
    client.find_existing_post.assert_not_called()
    assert draft.state == "queued"
    assert draft.post_urn is None


def test_expired_lease_and_no_existing_post_allows_reclaim_and_publishes(
    db_session: Session,
) -> None:
    # Arrange: a stranded 'queued' draft whose lease has expired and for which
    # reconciliation confirms NO post exists — so it is safe to re-claim + publish.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    client.find_existing_post.return_value = None  # reconcile: no post exists
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session, state="queued")
    _set_publish_lease(db_session, draft, lease_expires_at=_NOW - timedelta(minutes=1))

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: reconciliation ran (confirming safety) and the draft published once.
    client.find_existing_post.assert_called_once()
    client.publish_text.assert_called_once()
    assert draft.post_urn == _POST_URN
    assert draft.state == "published"


# --- ISSUE 4: reaper alerts on a draft stuck in 'queued' past its TTL -------


def test_reaper_alerts_on_draft_stuck_in_queued_past_ttl(db_session: Session) -> None:
    # Arrange: a draft that has been sitting in 'queued' far longer than its lease
    # TTL — the symptom of an approved post that would otherwise be silently lost.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    draft = _seed_draft(db_session, state="queued")
    _set_publish_lease(db_session, draft, lease_expires_at=_NOW - timedelta(days=2))

    # Act.
    alerted = _publisher(settings, client, mailer).reap_stuck(db_session, _NOW)

    # Assert: the reaper flags exactly one stuck draft and alerts the owner so the
    # approved post is never silently dropped.
    assert alerted == 1
    mailer.send.assert_called_once()


# --- dry_run: log only, post nothing, mutate nothing ------------------------


def test_dry_run_posts_nothing_and_leaves_state(db_session: Session) -> None:
    # Arrange: DRY_RUN mode with a due draft and credentials available.
    settings = _make_settings(VisionEnv.DRY_RUN)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session, state="scheduled")

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: no network publish, no mail, no URN, state untouched.
    client.publish_text.assert_not_called()
    client.publish_with_image.assert_not_called()
    mailer.send.assert_not_called()
    assert draft.post_urn is None
    assert draft.state == "scheduled"


# --- staging: publish then immediately delete the marked test post ----------


def test_staging_publishes_then_deletes_marked_test_post(db_session: Session) -> None:
    # Arrange: STAGING mode — the worker should post a marked test and delete it.
    settings = _make_settings(VisionEnv.STAGING)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    draft = _seed_draft(db_session)

    # Act.
    _publisher(settings, client, mailer).publish(db_session, draft)

    # Assert: a post was created and then deleted by URN; the draft is NOT finalised
    # as published (it stays reusable) and no confirmation email is sent.
    client.publish_text.assert_called_once()
    client.delete.assert_called_once()
    assert client.delete.call_args.args[1] == _POST_URN
    assert draft.post_urn is None
    assert draft.state == "scheduled"
    mailer.send.assert_not_called()


# --- The staging post text carries the auto-deleted marker ------------------


def test_staging_marks_post_text_as_auto_deleted(db_session: Session) -> None:
    # Arrange.
    settings = _make_settings(VisionEnv.STAGING)
    client = _mock_client()
    _seed_token(db_session)
    draft = _seed_draft(db_session)

    # Act.
    _publisher(settings, client, Mock()).publish(db_session, draft)

    # Assert: the text actually sent to LinkedIn is clearly marked as a test so a
    # stray post (if a delete ever fails) is unmistakable.
    sent_text = client.publish_text.call_args.args[2]
    assert "STAGING TEST" in sent_text


# --- Council publish: post + Council block + a SINGLE Brahmastra sign-off ----
#
# WHY set the council fields as instance attributes (not constructor kwargs): the
# ``content_mode`` / ``council_meta`` columns are owned by a separate agent's
# models migration. The worker reads them defensively via ``getattr`` on the
# in-memory draft, so stamping them post-construction exercises the assembly path
# hermetically WITHOUT coupling this test to the DB schema/migration.


def _seed_council_draft(
    session: Session,
    *,
    council_block: str,
    post_text: str = "We keep pretending there is one right answer.",
) -> Draft:
    """Insert a council draft (content_mode + council_meta) and return it."""
    draft = _seed_draft(session)
    draft.post_text = post_text
    # Set the council fields as plain instance attributes (see module note above).
    draft.content_mode = "council"
    draft.council_meta = {
        "topic": "unexplainable AI in hospitals",
        "format": "show_the_split",
        "situation": "disagreed",
        "council_block": council_block,
        "transcript": {"Gemini": {"round1": "x", "round2": "y"}},
    }
    session.add(draft)
    session.commit()
    return draft


def test_council_publish_text_includes_council_block_and_single_signature(
    db_session: Session,
) -> None:
    # Arrange: a council draft whose block ALREADY ends with the signature, with a
    # text-footer signature mode so the publisher would also want to sign.
    settings = _make_settings(VisionEnv.LIVE, signature=SignatureMode.TEXT_FOOTER)
    client = _mock_client()
    _seed_token(db_session)
    block = (
        "• Move fast, the upside is huge\n"
        "• Slow down, the downside is irreversible\n"
        "• The real risk is pretending it's binary\n"
        "Powered by Brahmastra"
    )
    draft = _seed_council_draft(db_session, council_block=block)

    # Act.
    _publisher(settings, client, Mock()).publish(db_session, draft)

    # Assert: the published text carries the post, the Council block, and EXACTLY
    # ONE 'Powered by Brahmastra' — never doubled despite the block already ending
    # with it AND the text-footer mode being active.
    sent_text = client.publish_text.call_args.args[2]
    assert "one right answer" in sent_text
    assert "Move fast, the upside is huge" in sent_text
    assert sent_text.count("Powered by Brahmastra") == 1
    # The lone signature is the final line of the post.
    assert sent_text.rstrip().endswith("Powered by Brahmastra")


def test_council_publish_omits_signature_when_signature_mode_off(
    db_session: Session,
) -> None:
    # Arrange: signature OFF (or card_watermark) means the text carries NO textual
    # sign-off — the block's own trailing signature is stripped so it isn't posted.
    settings = _make_settings(VisionEnv.LIVE, signature=SignatureMode.OFF)
    client = _mock_client()
    _seed_token(db_session)
    block = "• A\n• B\n• C\nPowered by Brahmastra"
    draft = _seed_council_draft(db_session, council_block=block)

    # Act.
    _publisher(settings, client, Mock()).publish(db_session, draft)

    # Assert: the Council bullets are present but no Brahmastra text sign-off is
    # added (the watermark, if any, lives on the card — not in the copy).
    sent_text = client.publish_text.call_args.args[2]
    assert "• A" in sent_text
    assert "Powered by Brahmastra" not in sent_text


# --- FINAL de-naming gate: a leaked AI name aborts publish (fail-closed) ------


def test_publish_aborts_when_council_block_names_an_ai(db_session: Session) -> None:
    # Arrange: a council draft whose Council block LEAKS a model name (the composer
    # is meant to fail closed upstream, but this belt-and-braces gate guarantees the
    # exact bytes about to hit LinkedIn are re-checked). The #1 rule: no model name.
    settings = _make_settings(VisionEnv.LIVE, signature=SignatureMode.TEXT_FOOTER)
    client = _mock_client()
    _seed_token(db_session)
    block = (
        "• Move fast, the upside is huge\n"
        "• Gemini warned the downside is irreversible\n"  # <-- leaked name
        "• The real risk is pretending it's binary\n"
        "Powered by Brahmastra"
    )
    draft = _seed_council_draft(db_session, council_block=block)

    # Act / Assert: the gate fires — publish aborts and NOTHING is posted.
    with pytest.raises(ForbiddenNameInPost):
        _publisher(settings, client, Mock()).publish(db_session, draft)
    client.publish_text.assert_not_called()
    client.publish_with_image.assert_not_called()


def test_publish_aborts_when_post_body_names_a_vendor(db_session: Session) -> None:
    # Arrange: a plain (news) draft whose BODY leaks a lowercase vendor variant —
    # proves the gate covers every content path, not just council drafts.
    settings = _make_settings(VisionEnv.LIVE, signature=SignatureMode.OFF)
    client = _mock_client()
    _seed_token(db_session)
    draft = _seed_draft(db_session)
    draft.post_text = "We asked chatgpt to draft this and it nailed the tone."
    db_session.add(draft)
    db_session.commit()

    # Act / Assert: leaked vendor name → publish aborts, nothing posted.
    with pytest.raises(ForbiddenNameInPost):
        _publisher(settings, client, Mock()).publish(db_session, draft)
    client.publish_text.assert_not_called()


# --- poll_and_publish: only approved, due, un-published drafts are posted ---


def test_poll_and_publish_only_processes_due_unpublished_drafts(
    db_session: Session,
) -> None:
    # Arrange: three drafts — one due & unpublished, one scheduled in the FUTURE,
    # and one already published (post_urn set). Only the first should be posted.
    settings = _make_settings(VisionEnv.LIVE)
    client = _mock_client()
    mailer = Mock()
    _seed_token(db_session)
    due = _seed_draft(db_session, scheduled_for=_NOW - timedelta(minutes=1))
    _seed_draft(db_session, scheduled_for=_NOW + timedelta(hours=2))  # future → skip
    _seed_draft(db_session, post_urn=_POST_URN)  # already published → skip

    # Act.
    published = _publisher(settings, client, mailer).poll_and_publish(db_session, _NOW)

    # Assert: exactly one draft was published (the due, unpublished one).
    assert published == 1
    client.publish_text.assert_called_once()
    assert due.post_urn == _POST_URN
    assert due.state == "published"


# ===========================================================================
# CLI crash-loop / resource-leak boundary (Codex HIGH — publisher.py ~41-55).
#
# WHY these live here: they exercise the ``vision-publisher`` cron ENTRY POINT
# (``publisher.main``), proving its fail-closed boundary — a 5-min poller must
# never dump an unsanitized traceback (crash-loop-adjacent) and must always
# release the LinkedIn HTTP pool, even when the worker raises mid-poll.
# ===========================================================================

# A marker only ever present inside the injected exception's message. If it
# surfaces anywhere in the logs, raw provider text leaked (the bug under test).
_PROVIDER_SECRET_MARKER = "PROVIDER-SECRET-LEAK-do-not-log-abc123"  # noqa: S105 - test placeholder


@contextmanager
def _fake_session_cm() -> Iterator[MagicMock]:
    """A stand-in for ``get_session()`` yielding a throwaway session.

    The worker is stubbed to raise before touching the session meaningfully, so a
    plain mock suffices; it exists only to satisfy the ``with`` protocol.
    """
    yield MagicMock(name="session")


class _ExplodingPublisher:
    """A ``LinkedInPublisher`` stand-in whose poll raises with provider-y text.

    Records ``close()`` calls so the test can prove the HTTP pool is always
    released even when the poll blows up (the resource-leak invariant).
    """

    def __init__(self, settings: Settings) -> None:  # noqa: D401 - mirrors real ctor
        self.closed = 0

    def poll_and_publish(self, session: object, now: datetime) -> int:
        # Simulate a provider/library fault whose message carries secret-ish text;
        # the boundary must NOT echo this into the logs.
        raise RuntimeError(f"linkedin upstream refused: {_PROVIDER_SECRET_MARKER}")

    def reap_stuck(self, session: object, now: datetime) -> int:  # pragma: no cover
        return 0

    def close(self) -> None:
        self.closed += 1


def _install_live_publisher_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI module at deterministic LIVE settings and a no-op logger setup.

    ``configure_logging`` is stubbed so pytest's ``caplog`` handler survives (the
    real setup clears the root handlers), letting us assert on the emitted record.
    """
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"vision_env": VisionEnv.LIVE})
    monkeypatch.setattr(publisher_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(publisher_cli, "configure_logging", lambda: None)
    monkeypatch.setattr(publisher_cli, "get_session", _fake_session_cm)


def test_main_returns_1_and_sanitizes_and_closes_when_worker_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: a worker whose poll raises an exception carrying provider text.
    _install_live_publisher_cli(monkeypatch)
    exploding = _ExplodingPublisher(object())  # type: ignore[arg-type]
    monkeypatch.setattr(publisher_cli, "LinkedInPublisher", lambda settings: exploding)
    caplog.set_level(logging.ERROR)

    # Act: the cron entry point must swallow the fault, not propagate it.
    exit_code = publisher_cli.main()

    # Assert: fail-closed non-zero exit (cron alerts), the HTTP pool was released,
    # and the log is SANITIZED — exception class + correlation id only, with NO
    # traceback and NO raw provider text.
    assert exit_code == 1
    assert exploding.closed == 1
    record = caplog.records[-1]
    assert record.error_type == "RuntimeError"
    assert record.correlation_id
    assert record.exc_info is None  # no traceback captured
    assert _PROVIDER_SECRET_MARKER not in caplog.text
    assert _PROVIDER_SECRET_MARKER not in str(record.__dict__)


def test_main_releases_nothing_but_fails_closed_when_construction_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: constructing the publisher itself raises (partial-alloc-then-raise).
    _install_live_publisher_cli(monkeypatch)

    def _boom_ctor(settings: Settings) -> _ExplodingPublisher:
        raise RuntimeError(f"pool init failed: {_PROVIDER_SECRET_MARKER}")

    monkeypatch.setattr(publisher_cli, "LinkedInPublisher", _boom_ctor)
    caplog.set_level(logging.ERROR)

    # Act: must not raise (no AttributeError from close() on a never-built publisher).
    exit_code = publisher_cli.main()

    # Assert: fail-closed, sanitized, and no traceback / provider text leaked.
    assert exit_code == 1
    record = caplog.records[-1]
    assert record.error_type == "RuntimeError"
    assert record.exc_info is None
    assert _PROVIDER_SECRET_MARKER not in caplog.text


class _CloseFailingPublisher:
    """A publisher whose poll SUCCEEDS but whose ``close()`` raises.

    Proves the finally-time cleanup can never escape the cron boundary as an
    unsanitized traceback (a crash-loop source): a failing ``close()`` must degrade
    to a sanitized log + non-zero exit, not propagate.
    """

    def __init__(self, settings: Settings) -> None:
        pass

    def poll_and_publish(self, session: object, now: datetime) -> int:
        return 0

    def reap_stuck(self, session: object, now: datetime) -> int:
        return 0

    def close(self) -> None:
        raise RuntimeError(f"pool close failed: {_PROVIDER_SECRET_MARKER}")


def test_main_fails_closed_when_close_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: a clean poll, but the HTTP-pool close() blows up during cleanup.
    _install_live_publisher_cli(monkeypatch)
    monkeypatch.setattr(
        publisher_cli, "LinkedInPublisher", lambda settings: _CloseFailingPublisher(settings)
    )
    caplog.set_level(logging.ERROR)

    # Act: cleanup failure must not escape as a traceback.
    exit_code = publisher_cli.main()

    # Assert: fail-closed non-zero exit with a sanitized cleanup log (no traceback,
    # no provider text).
    assert exit_code == 1
    record = caplog.records[-1]
    assert record.error_type == "RuntimeError"
    assert record.exc_info is None
    assert _PROVIDER_SECRET_MARKER not in caplog.text


def test_main_fails_closed_when_settings_load_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: settings/secret parsing raises during startup (before any work).
    get_settings.cache_clear()
    monkeypatch.setattr(publisher_cli, "configure_logging", lambda: None)

    def _boom_settings() -> Settings:
        raise RuntimeError(f"bad config: {_PROVIDER_SECRET_MARKER}")

    monkeypatch.setattr(publisher_cli, "get_settings", _boom_settings)
    caplog.set_level(logging.ERROR)

    # Act: a startup fault must be caught by the same fail-closed boundary.
    exit_code = publisher_cli.main()

    # Assert: non-zero exit, sanitized log, no traceback / provider text leaked.
    assert exit_code == 1
    record = caplog.records[-1]
    assert record.error_type == "RuntimeError"
    assert record.exc_info is None
    assert _PROVIDER_SECRET_MARKER not in caplog.text
