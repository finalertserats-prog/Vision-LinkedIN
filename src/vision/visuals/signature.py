"""Post signature application per ``POST_SIGNATURE_MODE`` (BRD §15.6, D9).

WHY this module exists: VISION can sign each published post as "curated via
Brahmastra" in one of four config-selected ways (D9) — and that choice must be
applied in exactly one place so the email preview, the LinkedIn commentary, and
any audit all agree on what was signed:

    * ``off``            — no signature at all.
    * ``card_watermark`` — the discreet BRAHMASTRA mark is already rendered ONTO
                           the card by ``card_renderer._draw_watermark``; this
                           module therefore leaves both the text and the image
                           untouched (the signature lives on the pixels).
    * ``text_footer``    — append ``settings.POST_SIGNATURE_TEXT`` to the post
                           body as a footer line.
    * ``both``           — the watermark (already on the card) *plus* the text
                           footer.

The function is PURE and config-driven: it performs no I/O, mutates nothing it is
given, and returns fresh values (immutability, §22). It never re-draws the
watermark — that is the renderer's job and doing it twice would double-mark the
card — it only decides whether the *text* footer is appended.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from vision.config import Settings, SignatureMode, get_settings

logger = logging.getLogger(__name__)

# Modes whose behaviour includes appending the textual footer. Kept as a set so
# the membership test reads as intent and new modes are a one-line change.
_TEXT_FOOTER_MODES: frozenset[SignatureMode] = frozenset(
    {SignatureMode.TEXT_FOOTER, SignatureMode.BOTH}
)

# Separator between the post body and the signature footer. Two newlines render
# as a blank line on LinkedIn, visually detaching the footer from the copy.
_FOOTER_SEPARATOR = "\n\n"


class SignedPost(NamedTuple):
    """The signed result: possibly-footered text plus the (unchanged) card bytes.

    Returned as a named tuple so callers read ``result.post_text`` /
    ``result.card_bytes`` rather than positional indices. ``card_bytes`` is
    passed through verbatim — the watermark, if any, was baked in at render time.
    """

    post_text: str
    card_bytes: bytes | None


def _append_footer(post_text: str, footer: str) -> str:
    """Return ``post_text`` with ``footer`` appended, idempotently.

    WHY idempotent: a draft may be re-processed (edit → re-approve), and appending
    the footer twice would double the signature. If the text already ends with the
    exact footer, it is returned unchanged. A blank footer is a no-op so a
    misconfigured empty ``POST_SIGNATURE_TEXT`` never adds a dangling separator.
    Builds a NEW string — the input is never mutated (§22 immutability).
    """
    clean_footer = footer.strip()
    if not clean_footer:
        # Nothing meaningful to sign with — leave the copy exactly as-is.
        return post_text

    body = post_text.rstrip()
    if body.endswith(clean_footer):
        # Already signed (idempotent re-processing) — do not stack a second copy.
        return post_text
    return f"{body}{_FOOTER_SEPARATOR}{clean_footer}"


def apply_signature(
    post_text: str,
    card_bytes: bytes | None,
    mode: SignatureMode | None = None,
    *,
    settings: Settings | None = None,
) -> SignedPost:
    """Apply the configured signature to a post's text and card (§15.6, D9).

    Pure and config-driven. The card's watermark (for ``card_watermark`` /
    ``both``) is applied by the renderer, so this function only decides whether to
    append the ``POST_SIGNATURE_TEXT`` footer to the copy; the card bytes always
    pass through unchanged.

    Args:
        post_text: The approved post body.
        card_bytes: The rendered card/image bytes, or ``None`` for a text-only
            post. Returned unchanged (the watermark is already on the pixels).
        mode: Override the signature mode; defaults to
            ``settings.POST_SIGNATURE_MODE`` when ``None`` so callers can rely on
            config while tests pass an explicit mode.
        settings: Config source (mode + footer text); defaults to the singleton.

    Returns:
        A ``SignedPost`` with the (possibly footered) text and the pass-through
        card bytes.
    """
    settings = settings or get_settings()
    # Resolve the effective mode: explicit arg wins, else config (§22).
    effective_mode = mode if mode is not None else settings.post_signature_mode

    if effective_mode in _TEXT_FOOTER_MODES:
        signed_text = _append_footer(post_text, settings.post_signature_text)
    else:
        # off / card_watermark → the copy is untouched (watermark, if any, is on
        # the card, not in the text).
        signed_text = post_text

    logger.debug(
        "applied signature mode=%s footer_appended=%s",
        effective_mode.value,
        signed_text != post_text,
    )
    # card_bytes is passed straight through: never re-watermarked here.
    return SignedPost(post_text=signed_text, card_bytes=card_bytes)
