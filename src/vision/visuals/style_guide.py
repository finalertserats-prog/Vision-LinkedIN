"""Concept-illustration style guide + LinkedIn image validation + weekly cap
(BRD §13.6 Step 4, §15.6, D9/D10).

WHY this module exists: the IMAGE lane makes three promises that all belong at a
single, auditable choke point *before* any image reaches LinkedIn:

  1. **A fixed house style** for concept illustrations. Every diffusion prompt is
     hardened with the same text-free, muted, editorial descriptors sourced from
     ``settings.IMAGE_STYLE_GUIDE`` (config over code, §22.6) so the model can
     never bake words/numbers/logos into an image (precision-first, §13.6/D10).
  2. **Dimension / format / size validation** so only well-formed images at a
     LinkedIn-acceptable canvas (≈1200x627 landscape or 1200x1200 square) are
     ever uploaded. A bad image is rejected here rather than failing mid-upload.
  3. **A per-week image cap** (``settings.IMAGE_MAX_PER_WEEK``) computed by
     querying the ``drafts`` table, so VISION stays under a self-imposed visual
     cadence (D9) — text-only posts are always allowed, images are the exception.

None of these functions perform network I/O. Validation and the cap check are
pure/DB-only so they are trivially unit-testable with mocked/​in-memory inputs
(no real generation, no real upload — §22 tests are part of done).
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vision.config import Settings, get_settings
from vision.db.models import Draft

logger = logging.getLogger(__name__)

# --- LinkedIn canvas contract ----------------------------------------------
# WHY named constants (not magic tuples): the accepted canvases are a contract
# the deterministic renderer already emits (card_renderer.LINKEDIN_LANDSCAPE /
# LINKEDIN_SQUARE) and that tests assert against. They are duplicated here as
# plain tuples deliberately: importing card_renderer would drag matplotlib into
# the lightweight upload path, so the upload boundary stays dependency-light.
LINKEDIN_LANDSCAPE: tuple[int, int] = (1200, 627)  # link-preview / stat cards
LINKEDIN_SQUARE: tuple[int, int] = (1200, 1200)  # square charts / illustrations
ALLOWED_DIMENSIONS: tuple[tuple[int, int], ...] = (LINKEDIN_LANDSCAPE, LINKEDIN_SQUARE)

# "≈" tolerance: a diffusion illustration or a re-encoded card can land a few
# pixels off the exact target. A small absolute tolerance accepts those without
# opening the door to arbitrarily-shaped images (which would break LinkedIn's
# preview crop). Kept modest so only genuine rounding drift passes.
_DIMENSION_TOLERANCE_PX = 16

# Only raster formats LinkedIn's image ingest accepts; anything else (GIF, WEBP,
# SVG, or a text error masquerading as an image) is rejected before upload.
ALLOWED_FORMATS: frozenset[str] = frozenset({"PNG", "JPEG"})

# Upper bound on the uploaded blob. Our deterministic PNGs are well under 1 MB;
# this guard exists so a corrupt/huge payload can never be PUT to LinkedIn. Kept
# as a module constant (not a hidden literal) so it is auditable and patchable.
MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 10 MiB

# --- Fixed concept-illustration style guide --------------------------------
# WHY these live here: the negative constraints are *mandatory* — they are what
# make a diffusion image safe to publish under the precision-first rule (§13.6),
# so they are enforced in code and merely *seeded* by the owner-editable
# ``IMAGE_STYLE_GUIDE`` config. The config sets the aesthetic; these guarantee it
# never carries text/numbers/logos regardless of what the model was asked for.
_MANDATORY_NEGATIVES = "no text, no words, no numbers, no logos, no watermarks, no charts"
_ENFORCED_TONE = "editorial, conceptual, abstract"


def illustration_style_guide(settings: Settings | None = None) -> str:
    """Return the fixed, text-free concept-illustration style guide (§13.6 Step 4).

    Composes the owner-editable aesthetic (``settings.IMAGE_STYLE_GUIDE``, e.g.
    'minimal, professional, muted palette, no text, no logos') with the enforced
    editorial tone and the mandatory negative constraints. The negatives are
    always appended so a text-free, precision-first image is guaranteed no matter
    how the synthesis pass phrased its prompt — the config owns taste, this
    function owns the non-negotiable guardrails.

    Args:
        settings: Config source; defaults to the process-wide singleton.

    Returns:
        A single style-guide clause suitable for appending to an illustration
        prompt (see ``visuals.illustrate``).
    """
    settings = settings or get_settings()
    # Strip so a trailing period/space in config never produces doubled
    # punctuation in the composed guide.
    base = settings.image_style_guide.strip().rstrip(".")
    return f"{base}. Tone: {_ENFORCED_TONE}. Enforced: {_MANDATORY_NEGATIVES}."


# --- Image validation -------------------------------------------------------


class ImageValidationError(ValueError):
    """Raised when an image fails the LinkedIn dimension/format/size contract.

    A subclass of ``ValueError`` so callers that only care "is this image
    uploadable?" can catch it narrowly. The IMAGE lane treats this as a *soft*
    failure: ``publish.image_upload`` catches it and degrades to a text-only post
    (image never blocks publishing, §13.6).
    """


class ImageSpec(NamedTuple):
    """The validated, uploadable properties of an image.

    Returned by ``validate_linkedin_image`` so callers get the decoded dimensions
    and format without re-opening the bytes.
    """

    width: int
    height: int
    image_format: str
    size_bytes: int


def _dimensions_ok(width: int, height: int) -> bool:
    """True when ``(width, height)`` is within tolerance of an allowed canvas.

    Checks every allowed target so a landscape card, a square chart, or a square
    illustration all pass, while an arbitrarily-shaped image does not.
    """
    return any(
        abs(width - target_w) <= _DIMENSION_TOLERANCE_PX
        and abs(height - target_h) <= _DIMENSION_TOLERANCE_PX
        for target_w, target_h in ALLOWED_DIMENSIONS
    )


def validate_linkedin_image(image_bytes: bytes) -> ImageSpec:
    """Validate raw image bytes against the LinkedIn upload contract (§15.6).

    Enforces, in order, the cheap-to-expensive checks:
      * non-empty payload no larger than ``MAX_IMAGE_BYTES``;
      * a decodable raster in an ``ALLOWED_FORMATS`` format (PNG/JPEG);
      * dimensions within tolerance of an allowed canvas
        (≈1200x627 or 1200x1200).

    Args:
        image_bytes: The rendered/generated image to upload.

    Returns:
        An ``ImageSpec`` describing the validated image.

    Raises:
        ImageValidationError: On any contract violation. Callers in the IMAGE
            lane catch this and degrade to a text-only post (§13.6) — a bad image
            must never block publishing.
    """
    if not image_bytes:
        raise ImageValidationError("empty image payload; nothing to upload")

    size = len(image_bytes)
    # Size first: cheapest check, and it bounds the work Pillow must do below.
    if size > MAX_IMAGE_BYTES:
        raise ImageValidationError(
            f"image is {size} bytes, exceeds the {MAX_IMAGE_BYTES}-byte upload cap"
        )

    try:
        # ``Image.open`` is lazy; ``.load`` (via .size access after verifying)
        # forces a decode so a truncated/garbage payload fails here, not on PUT.
        with Image.open(io.BytesIO(image_bytes)) as image:
            image_format = image.format or ""
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        # Specific decode failures only (no bare except, §22): a payload Pillow
        # cannot parse is not an image we can safely upload.
        raise ImageValidationError(f"payload is not a decodable image: {exc}") from exc

    if image_format not in ALLOWED_FORMATS:
        raise ImageValidationError(
            f"image format {image_format!r} not in allowed {sorted(ALLOWED_FORMATS)}"
        )

    if not _dimensions_ok(width, height):
        raise ImageValidationError(
            f"image is {width}x{height}, not within {_DIMENSION_TOLERANCE_PX}px of "
            f"an allowed canvas {ALLOWED_DIMENSIONS} (§15.6)"
        )

    return ImageSpec(width=width, height=height, image_format=image_format, size_bytes=size)


# --- Per-week image cap -----------------------------------------------------


def weekly_image_count(
    session: Session,
    *,
    now: datetime | None = None,
    days: int = 7,
) -> int:
    """Count drafts that attached an image in the trailing ``days`` window.

    An image is "used" once it has been uploaded to LinkedIn — i.e. the draft
    carries an ``image_urn``. We count those rows created within the window so the
    cap reflects actual published visual cadence, not merely *proposed* images
    that were later degraded to text-only.

    Args:
        session: Active DB session (SQLite dev / Postgres prod — same code).
        now: Reference "now" (tz-aware); defaults to the current UTC time.
            Injectable so tests are deterministic.
        days: Rolling look-back window; D9 caps images per 7-day week.

    Returns:
        The number of image-carrying drafts inside the window.
    """
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=days)

    # COUNT in SQL (not len() over hydrated rows) so the check stays cheap even as
    # the drafts table grows. Only rows with a real image URN inside the window.
    stmt = (
        select(func.count())
        .select_from(Draft)
        .where(Draft.image_urn.is_not(None), Draft.created_at >= cutoff)
    )
    count = session.scalar(stmt) or 0
    return int(count)


def weekly_image_cap_reached(
    session: Session,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> bool:
    """True when this week's image count has met/exceeded ``IMAGE_MAX_PER_WEEK``.

    The gate the visuals lane consults before spending an image on a draft: when
    the cap is reached the draft still publishes, just as text-only (D9 cadence).
    The threshold comes from config so the cadence is tunable without code (§22).

    Args:
        session: Active DB session.
        settings: Config source (``IMAGE_MAX_PER_WEEK``); defaults to singleton.
        now: Reference "now" (tz-aware) for the window; defaults to current UTC.

    Returns:
        ``True`` if no further image should be attached this week.
    """
    settings = settings or get_settings()
    used = weekly_image_count(session, now=now)
    reached = used >= settings.image_max_per_week

    if reached:
        # Log the cadence decision (no secrets involved) so the run trace shows
        # *why* a draft went text-only despite an available image.
        logger.info(
            "weekly image cap reached (%d/%d); draft will publish text-only",
            used,
            settings.image_max_per_week,
        )
    return reached
