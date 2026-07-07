"""Unit tests for the ``vision-council`` CLI glue (BRD §5 evolution).

WHY this suite exists: ``vision.cli.council.run_council_cli`` is the GLUE that
turns a council deliberation into an approvable draft on the SAME rails the daily
lane uses. These tests prove the glue's contract WITHOUT ever touching a real
model, a real mailer, or a real database:

  * the 3-AI engine (``run_council``) is MOCKED — a unit test must never call a
    real model (BRD §18);
  * the email sender is a MOCK — no SMTP/HTTP leaves the test;
  * the session is a hermetic in-memory SQLite (the ``db_session`` fixture).

Everything security-critical between those seams — the DB write, the REAL signed
single-use approval token, the ``content_mode`` tag, the ``council_meta`` blob —
runs as real code. Each test follows AAA (Arrange -> Act -> Assert) with one
behaviour per test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from vision.cli.council import run_council_cli
from vision.config import Settings, SignatureMode, VisionEnv
from vision.db.models import Draft

# A fixed reference "now" so token expiry / cutoff arithmetic is reproducible
# across machines (no wall-clock flakiness).
_NOW: datetime = datetime(2026, 7, 7, 6, 30, 0, tzinfo=timezone.utc)

# Where the CLI imports the engine from — patched so the real 3-AI deliberation
# never runs in a unit test. Patching at the CLI's import site (not the engine
# module) ensures the name the CLI actually calls is the mock.
_ENGINE_TARGET = "vision.cli.council.run_council"


def _settings(env: VisionEnv) -> Settings:
    """Pinned settings for a run mode, independent of any developer's ``.env``.

    A fixed HMAC key makes the minted approval token deterministic-shaped, and an
    explicit cutoff keeps the token TTL reproducible. OFF signature keeps the run
    free of any on-disk watermark dependency.
    """
    return Settings(
        VISION_ENV=env,
        TZ="Asia/Kolkata",
        APPROVE_CUTOFF_LOCAL="20:00",
        POST_SIGNATURE_MODE=SignatureMode.OFF,
        SECRET_HMAC_KEY="council-cli-test-hmac",  # noqa: S106 - test placeholder
    )


def _council_payload() -> dict[str, Any]:
    """A canned ``run_council`` return value (the shape the engine promises).

    Mirrors ``vision.council.engine.run_council``'s documented Draft-shaped dict so
    the mock is faithful: a de-named public post + hashtags, plus the provenance
    (topic/format/situation/council_block/transcript) the CLI must stash into
    ``council_meta`` and NEVER publish.
    """
    return {
        "content_mode": "council",
        "topic": "Should hospitals trust unexplainable but superior AI?",
        "format": "show_the_split",
        "situation": "disagreed — one voice prioritised outcomes, another accountability",
        "post_text": "Three minds argued about trust in medicine...\n\nPowered by Brahmastra",
        "hashtags": ["#Healthcare", "#AI", "#Trust"],
        "council_block": "• Outcomes first\n• Accountability first\n• A third path\nPowered by Brahmastra",
        "transcript": {
            "Gemini": {"round1": "r1-g", "round2": "r2-g"},
            "Codex": {"round1": "r1-c", "round2": "r2-c"},
            "Claude": {"round1": "r1-cl", "round2": "r2-cl"},
        },
        "model_trace": {"content_mode": "council", "live_voices": ["Gemini", "Codex", "Claude"]},
    }


def test_run_creates_pending_approval_council_draft(db_session: Session) -> None:
    """A run persists a pending_approval draft tagged council with council_meta set."""
    # Arrange: mock the engine (no real model) and a mock sender (no email path hit
    # in staging is fine — we assert the DRAFT here, not the email).
    settings = _settings(VisionEnv.STAGING)
    sender = MagicMock()
    sender.send.return_value = True

    # Act
    with patch(_ENGINE_TARGET, return_value=_council_payload()) as engine:
        result = run_council_cli(
            _NOW, VisionEnv.STAGING, session=db_session, settings=settings, sender=sender
        )

    # Assert: exactly one draft, in the right state, tagged and provenanced.
    engine.assert_called_once()
    stored = db_session.query(Draft).one()
    assert stored.state == "pending_approval"
    assert stored.content_mode == "council"
    assert result.draft_id == str(stored.id)
    # council_meta carries the full provenance shape (never published, only stored).
    assert stored.council_meta is not None
    assert stored.council_meta["topic"] == _council_payload()["topic"]
    assert stored.council_meta["format"] == "show_the_split"
    assert stored.council_meta["council_block"].endswith("Powered by Brahmastra")
    assert stored.council_meta["transcript"]["Gemini"]["round1"] == "r1-g"


def test_dry_run_sends_no_email_but_stores_draft(db_session: Session) -> None:
    """dry_run composes + stores the draft but sends NO email (FR-20 safe default)."""
    # Arrange
    settings = _settings(VisionEnv.DRY_RUN)
    sender = MagicMock()

    # Act
    with patch(_ENGINE_TARGET, return_value=_council_payload()):
        result = run_council_cli(
            _NOW, VisionEnv.DRY_RUN, session=db_session, settings=settings, sender=sender
        )

    # Assert: the draft exists, but the sender was NEVER invoked and no email sent.
    assert db_session.query(Draft).count() == 1
    assert result.email_sent is False
    sender.send.assert_not_called()


def test_approval_token_is_issued(db_session: Session) -> None:
    """The run mints and persists an approval token (hash + expiry) on the draft."""
    # Arrange
    settings = _settings(VisionEnv.DRY_RUN)
    sender = MagicMock()

    # Act
    with patch(_ENGINE_TARGET, return_value=_council_payload()):
        run_council_cli(
            _NOW, VisionEnv.DRY_RUN, session=db_session, settings=settings, sender=sender
        )

    # Assert: the single-use approve token key + a future expiry are stored (the raw
    # token itself is never persisted — only its hash lives on the draft, §14.2).
    stored = db_session.query(Draft).one()
    assert stored.approve_token_hash
    assert stored.token_expires_at is not None
    # SQLite strips tzinfo on round-trip, so normalise both sides to naive-UTC before
    # the ordering compare (the token is minted aware-UTC; only the DB display drops
    # the offset). A future expiry proves a live, non-dead link was issued.
    stored_naive = stored.token_expires_at.replace(tzinfo=None)
    assert stored_naive > _NOW.replace(tzinfo=None)


def test_staging_sends_the_approval_email(db_session: Session) -> None:
    """In staging the composed approval email is actually sent via the sender."""
    # Arrange
    settings = _settings(VisionEnv.STAGING)
    sender = MagicMock()
    sender.send.return_value = True

    # Act
    with patch(_ENGINE_TARGET, return_value=_council_payload()):
        result = run_council_cli(
            _NOW, VisionEnv.STAGING, session=db_session, settings=settings, sender=sender
        )

    # Assert: the sender was called once and the result reflects a sent email.
    sender.send.assert_called_once()
    assert result.email_sent is True
