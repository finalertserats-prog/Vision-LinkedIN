"""Unit tests for the finalised IMAGE lane + SIGNATURE modes (BRD §13.6, §15.6).

WHY these tests (BRD §18/§22 — tests are part of "done"): the IMAGE lane makes
four promises the publish path relies on —

  1. Only well-formed images at a LinkedIn canvas are ever uploaded
     (``style_guide.validate_linkedin_image``).
  2. The per-week image cap is enforced from the drafts table
     (``style_guide.weekly_image_cap_reached``).
  3. Each ``POST_SIGNATURE_MODE`` produces exactly the right text/footer
     (``signature.apply_signature``).
  4. Upload validates BEFORE calling the client, and every failure degrades to a
     text-only post — image never blocks publishing
     (``publish.image_upload.prepare_and_upload``).

Every test is AAA (Arrange → Act → Assert). The only external collaborators —
``LinkedInClient`` and ``BrahmastraImageClient`` — are MOCKED, so no real
network call and no real image generation ever happen.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image
from sqlalchemy.orm import Session

from vision.config import Settings, SignatureMode
from vision.db.models import Draft
from vision.publish.errors import TransientLinkedInError
from vision.publish.image_upload import prepare_and_upload
from vision.visuals.signature import apply_signature
from vision.visuals import style_guide
from vision.visuals.style_guide import (
    ImageValidationError,
    ImageSpec,
    illustration_style_guide,
    validate_linkedin_image,
    weekly_image_cap_reached,
    weekly_image_count,
)

# --- Helpers ----------------------------------------------------------------

_TOKEN = "fake-access-token"  # noqa: S105 - test placeholder, not a real secret


def _settings(**overrides: object) -> Settings:
    """Build deterministic Settings independent of the developer's environment."""
    base: dict[str, object] = {
        "POST_SIGNATURE_MODE": SignatureMode.OFF,
        "POST_SIGNATURE_TEXT": "— curated via Brahmastra",
        "IMAGE_MAX_PER_WEEK": 4,
    }
    base.update(overrides)
    return Settings(**base)


def _png_bytes(size: tuple[int, int], image_format: str = "PNG") -> bytes:
    """Render a solid image at ``size`` and return its encoded bytes.

    Kept as a factory (not inline literals) so each test declares only the one
    property it cares about — dimension or format — per the testing rules.
    """
    buffer = io.BytesIO()
    Image.new("RGB", size, (11, 31, 58)).save(buffer, format=image_format)
    return buffer.getvalue()


# --- style_guide: illustration guide ----------------------------------------


def test_illustration_style_guide_enforces_text_free_negatives() -> None:
    # Arrange
    settings = _settings(IMAGE_STYLE_GUIDE="minimal, muted palette")

    # Act
    guide = illustration_style_guide(settings)

    # Assert — the owner aesthetic is present AND the mandatory negatives are
    # always appended so the model cannot bake in text/logos.
    assert "minimal, muted palette" in guide
    assert "no text" in guide
    assert "no logos" in guide


# --- style_guide: image validation ------------------------------------------


def test_validate_accepts_landscape_dimensions() -> None:
    # Arrange
    data = _png_bytes((1200, 627))

    # Act
    spec = validate_linkedin_image(data)

    # Assert — a valid landscape card returns its measured spec.
    assert spec == ImageSpec(width=1200, height=627, image_format="PNG", size_bytes=len(data))


def test_validate_accepts_square_dimensions() -> None:
    # Arrange
    data = _png_bytes((1200, 1200))

    # Act
    spec = validate_linkedin_image(data)

    # Assert
    assert (spec.width, spec.height) == (1200, 1200)


def test_validate_rejects_off_canvas_dimensions() -> None:
    # Arrange — a square that is nowhere near an allowed canvas.
    data = _png_bytes((600, 600))

    # Act / Assert — rejected before any upload could happen.
    with pytest.raises(ImageValidationError, match="not within"):
        validate_linkedin_image(data)


def test_validate_rejects_disallowed_format() -> None:
    # Arrange — correct dimensions but a GIF (not PNG/JPEG).
    data = _png_bytes((1200, 1200), image_format="GIF")

    # Act / Assert
    with pytest.raises(ImageValidationError, match="format"):
        validate_linkedin_image(data)


def test_validate_rejects_non_image_payload() -> None:
    # Arrange — a text error message masquerading as an image.
    data = b"upstream model returned an error, not an image"

    # Act / Assert
    with pytest.raises(ImageValidationError, match="decodable image"):
        validate_linkedin_image(data)


def test_validate_rejects_oversize_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange — shrink the cap so a normal PNG exceeds it, without building 10 MiB.
    monkeypatch.setattr(style_guide, "MAX_IMAGE_BYTES", 10)
    data = _png_bytes((1200, 627))

    # Act / Assert
    with pytest.raises(ImageValidationError, match="upload cap"):
        validate_linkedin_image(data)


# --- style_guide: weekly image cap ------------------------------------------


def _add_image_draft(session: Session, *, created_at: datetime) -> None:
    """Persist one draft that carried an uploaded image at ``created_at``."""
    session.add(Draft(state="published", image_urn="urn:li:image:x", created_at=created_at))


def test_weekly_cap_reached_when_count_meets_threshold(db_session: Session) -> None:
    # Arrange — exactly IMAGE_MAX_PER_WEEK image-carrying drafts this week.
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    settings = _settings(IMAGE_MAX_PER_WEEK=3)
    for _ in range(3):
        _add_image_draft(db_session, created_at=now - timedelta(days=1))
    db_session.flush()

    # Act
    reached = weekly_image_cap_reached(db_session, settings=settings, now=now)

    # Assert — the cap is met, so the next draft must publish text-only.
    assert reached is True


def test_weekly_cap_not_reached_below_threshold(db_session: Session) -> None:
    # Arrange — one image this week, cap is 4.
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    settings = _settings(IMAGE_MAX_PER_WEEK=4)
    _add_image_draft(db_session, created_at=now - timedelta(hours=2))
    db_session.flush()

    # Act
    reached = weekly_image_cap_reached(db_session, settings=settings, now=now)

    # Assert
    assert reached is False


def test_weekly_count_excludes_drafts_outside_window(db_session: Session) -> None:
    # Arrange — one image inside the 7-day window, one well outside it.
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    _add_image_draft(db_session, created_at=now - timedelta(days=1))
    _add_image_draft(db_session, created_at=now - timedelta(days=30))
    db_session.flush()

    # Act
    count = weekly_image_count(db_session, now=now)

    # Assert — only the in-window image is counted.
    assert count == 1


def test_weekly_count_ignores_text_only_drafts(db_session: Session) -> None:
    # Arrange — a recent draft with NO image_urn must not count toward the cap.
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    db_session.add(Draft(state="published", created_at=now - timedelta(hours=1)))
    db_session.flush()

    # Act
    count = weekly_image_count(db_session, now=now)

    # Assert
    assert count == 0


# --- signature: the four modes ----------------------------------------------


def test_signature_off_leaves_text_and_card_untouched() -> None:
    # Arrange
    settings = _settings(POST_SIGNATURE_MODE=SignatureMode.OFF)
    card = b"\x89PNG-card"

    # Act
    result = apply_signature("Body copy.", card, settings=settings)

    # Assert — nothing appended, card passed straight through.
    assert result.post_text == "Body copy."
    assert result.card_bytes is card


def test_signature_text_footer_appends_configured_text() -> None:
    # Arrange
    settings = _settings(
        POST_SIGNATURE_MODE=SignatureMode.TEXT_FOOTER,
        POST_SIGNATURE_TEXT="— curated via Brahmastra",
    )

    # Act
    result = apply_signature("Body copy.", None, settings=settings)

    # Assert — footer appended on its own detached line.
    assert result.post_text == "Body copy.\n\n— curated via Brahmastra"


def test_signature_card_watermark_does_not_touch_text() -> None:
    # Arrange — the watermark lives on the card pixels, not the copy.
    settings = _settings(POST_SIGNATURE_MODE=SignatureMode.CARD_WATERMARK)
    card = b"card-with-baked-watermark"

    # Act
    result = apply_signature("Body copy.", card, settings=settings)

    # Assert — text unchanged, card passed through (never re-watermarked here).
    assert result.post_text == "Body copy."
    assert result.card_bytes is card


def test_signature_both_appends_footer_and_passes_card() -> None:
    # Arrange
    settings = _settings(
        POST_SIGNATURE_MODE=SignatureMode.BOTH,
        POST_SIGNATURE_TEXT="— via Brahmastra",
    )
    card = b"card-bytes"

    # Act
    result = apply_signature("Body.", card, settings=settings)

    # Assert — footer on the text AND the (already-watermarked) card through.
    assert result.post_text == "Body.\n\n— via Brahmastra"
    assert result.card_bytes is card


def test_signature_footer_is_idempotent() -> None:
    # Arrange — text already carries the footer (e.g. edit → re-approve).
    settings = _settings(
        POST_SIGNATURE_MODE=SignatureMode.TEXT_FOOTER,
        POST_SIGNATURE_TEXT="— sig",
    )
    already = "Body.\n\n— sig"

    # Act
    result = apply_signature(already, None, settings=settings)

    # Assert — no second copy of the footer is stacked on.
    assert result.post_text == already


def test_signature_explicit_mode_overrides_settings() -> None:
    # Arrange — config says OFF, caller forces TEXT_FOOTER.
    settings = _settings(POST_SIGNATURE_MODE=SignatureMode.OFF, POST_SIGNATURE_TEXT="— sig")

    # Act
    result = apply_signature("Body.", None, SignatureMode.TEXT_FOOTER, settings=settings)

    # Assert — the explicit argument wins.
    assert result.post_text == "Body.\n\n— sig"


# --- image_upload: validate-then-upload with graceful degradation -----------


def test_prepare_and_upload_validates_before_calling_client() -> None:
    # Arrange — a valid image and a mocked LinkedIn client returning a URN.
    client = MagicMock(spec=["upload_image"])
    client.upload_image.return_value = "urn:li:image:OK"
    data = _png_bytes((1200, 627))

    # Act
    urn = prepare_and_upload(client, _TOKEN, data, owner_urn="urn:li:person:ME")

    # Assert — client called exactly once with the token/bytes/owner; URN returned.
    assert urn == "urn:li:image:OK"
    client.upload_image.assert_called_once_with(
        _TOKEN, data, owner_urn="urn:li:person:ME"
    )


def test_prepare_and_upload_rejects_bad_image_without_calling_client() -> None:
    # Arrange — a mis-sized image that must fail validation.
    client = MagicMock(spec=["upload_image"])
    data = _png_bytes((500, 500))

    # Act
    urn = prepare_and_upload(client, _TOKEN, data)

    # Assert — degraded to text-only, and the client was never invoked.
    assert urn is None
    client.upload_image.assert_not_called()


def test_prepare_and_upload_returns_none_for_empty_bytes() -> None:
    # Arrange
    client = MagicMock(spec=["upload_image"])

    # Act
    urn = prepare_and_upload(client, _TOKEN, None)

    # Assert — nothing to upload, client untouched.
    assert urn is None
    client.upload_image.assert_not_called()


def test_prepare_and_upload_degrades_on_client_error() -> None:
    # Arrange — validation passes, but the LinkedIn upload fails transiently.
    client = MagicMock(spec=["upload_image"])
    client.upload_image.side_effect = TransientLinkedInError("images 503", status_code=503)
    data = _png_bytes((1200, 1200))

    # Act
    urn = prepare_and_upload(client, _TOKEN, data)

    # Assert — the failure is swallowed at the image boundary → text-only post.
    assert urn is None
    client.upload_image.assert_called_once()


def test_prepare_and_upload_degrades_on_raw_httpx_timeout() -> None:
    # Arrange — validation passes, but the client raises a RAW httpx transport
    # error (a timeout) that is NOT a LinkedInError. Per BRD §13.6 an image
    # failure of ANY kind must degrade to text-only, never propagate and block the
    # text post (ISSUE 7 — only LinkedInError was previously caught).
    client = MagicMock(spec=["upload_image"])
    client.upload_image.side_effect = httpx.TimeoutException("image upload timed out")
    data = _png_bytes((1200, 1200))

    # Act
    urn = prepare_and_upload(client, _TOKEN, data)

    # Assert — the raw transport error is swallowed at the image boundary → the
    # caller degrades to a text-only post rather than crashing.
    assert urn is None
    client.upload_image.assert_called_once()


def test_image_lane_end_to_end_with_mocked_generation_and_upload() -> None:
    # Arrange — mock BOTH collaborators: the image generator and the uploader.
    # BrahmastraImageClient yields bytes; LinkedInClient uploads them. No real
    # generation, no real network.
    brahmastra = MagicMock(spec=["illustrate"])
    brahmastra.illustrate.return_value = _png_bytes((1200, 1200))
    linkedin = MagicMock(spec=["upload_image"])
    linkedin.upload_image.return_value = "urn:li:image:GEN"

    # Act — the lane: generate → validate+upload.
    generated = brahmastra.illustrate("abstract muted horizon")
    urn = prepare_and_upload(linkedin, _TOKEN, generated)

    # Assert — a generated, valid image flows through to an image URN.
    assert urn == "urn:li:image:GEN"
    brahmastra.illustrate.assert_called_once()
    linkedin.upload_image.assert_called_once()
