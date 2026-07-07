"""Validate-then-upload boundary for the IMAGE lane (BRD §13.6, §15.2/§15.6).

WHY this thin module exists: attaching an image to a post is a *best-effort
enhancement* — it must NEVER block publishing (§13.6 guardrail). This module is
the single safe entry point that (a) validates image bytes against the LinkedIn
dimension/format/size contract BEFORE spending an upload round-trip, and (b)
turns every failure — bad image OR a LinkedIn upload error — into a soft ``None``
so the caller degrades to a text-only post. The publisher therefore never has to
reason about image failures; it just checks whether it got an ``image_urn`` back.

The real HTTP work lives in ``LinkedInClient.upload_image`` (already built and
mocked in tests). This wrapper adds only pre-validation + the failure→``None``
degradation, and is trivially mockable (no real network ever runs in a unit
test, §22). Access tokens are passed straight to the client and NEVER logged.
"""

from __future__ import annotations

import logging

import httpx

from vision.publish.errors import LinkedInError
from vision.publish.linkedin import LinkedInClient
from vision.visuals.style_guide import ImageValidationError, validate_linkedin_image

logger = logging.getLogger(__name__)


def prepare_and_upload(
    client: LinkedInClient,
    access_token: str,
    image_bytes: bytes | None,
    *,
    owner_urn: str | None = None,
) -> str | None:
    """Validate ``image_bytes`` then upload it, returning the image URN or ``None``.

    Contract (BRD §13.6 — image never blocks publishing):
      * ``None``/empty bytes → ``None`` (nothing to upload).
      * Bytes failing the LinkedIn contract (dims/format/size) → validation is
        rejected here, logged, and ``None`` is returned. The client is NOT called.
      * A LinkedIn-side upload failure (any ``LinkedInError``) → logged and
        ``None`` returned, so the caller degrades to a text-only post rather than
        failing the publish. The typed error is deliberately swallowed at this
        image boundary; the publish call has its own §15.4 error handling.

    Args:
        client: The (real or mocked) LinkedIn client performing the upload.
        access_token: Bearer token for the upload. Passed to the client only;
            never logged (§22 / threat model — no tokens in logs).
        image_bytes: The rendered/generated image, or ``None``.
        owner_urn: The member URN that owns the image; forwarded to the client so
            it can skip a redundant userinfo round-trip when already known.

    Returns:
        The ``urn:li:image:...`` on success, or ``None`` to signal "publish
        text-only".
    """
    # Nothing to do for a text-only draft — skip straight to the text path.
    if not image_bytes:
        logger.debug("no image bytes supplied; publishing text-only")
        return None

    # Validate BEFORE touching the network so a malformed image never costs an
    # upload attempt and never reaches LinkedIn.
    try:
        spec = validate_linkedin_image(image_bytes)
    except ImageValidationError as exc:
        # Soft failure: log the reason (no token, no bytes) and degrade.
        logger.warning("image failed validation; degrading to text-only: %s", exc)
        return None

    try:
        image_urn = client.upload_image(access_token, image_bytes, owner_urn=owner_urn)
    except (LinkedInError, httpx.HTTPError, OSError) as exc:
        # Upload-side failure must NEVER block the post — the image lane is strictly
        # best-effort (BRD §13.6). We catch not only the typed ``LinkedInError`` but
        # also any RAW transport failure the client might surface (``httpx.HTTPError``
        # covers timeouts and connection/transport errors) and local IO errors
        # (``OSError``), so a slow or dropped image upload degrades to a text-only
        # post instead of propagating and failing the publish. Log only the error
        # CLASS (never the token or a URL) and degrade.
        logger.warning(
            "image upload failed; degrading to text-only: %s", exc.__class__.__name__
        )
        return None

    logger.info(
        "image uploaded (%dx%d %s); attaching to post",
        spec.width,
        spec.height,
        spec.image_format,
    )
    return image_urn
