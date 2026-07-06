"""Concept-illustration wrapper — degrade-gracefully over Brahmastra (BRD §13.6/FR-23).

WHY this thin module exists: the visuals lane needs a *single, safe* entry point
for the ``concept-illustration`` outcome that (a) prepends the fixed, text-free
style guide so the diffusion model never bakes words/numbers into the image
(precision-first, §13.6), and (b) turns the underlying client's hard failure
into a soft ``None`` so the caller can degrade to a text-only post. Image
generation must NEVER block publishing (§13.6 guardrail) — this contract makes
that impossible to get wrong at the call site.

The actual subprocess/model work lives in ``BrahmastraImageClient`` (already
built and unit-tested). This wrapper adds only prompt hardening + the
raise→``None`` degradation, and is trivially mockable in tests (no real
generation ever runs in a unit test, §22).
"""

from __future__ import annotations

import logging

from vision.brahmastra.errors import ImageGenerationError
from vision.brahmastra.image_client import BrahmastraImageClient
from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _build_prompt(illustration_prompt: str, style_guide: str) -> str:
    """Compose the final prompt: caller's concept + the enforced style guide.

    The style guide (``IMAGE_STYLE_GUIDE``, e.g. 'no text, no logos') is appended
    on EVERY call so a text-free, on-brand aesthetic is guaranteed regardless of
    what the synthesis pass proposed — the config, not the model, owns house
    style (§22.6 config over code).
    """
    concept = illustration_prompt.strip()
    guide = style_guide.strip()
    # Explicit separator keeps the two intents legible to the model and to a
    # human auditing the logged prompt.
    return f"{concept}\n\nStyle: {guide}"


def generate_illustration(
    illustration_prompt: str,
    *,
    client: BrahmastraImageClient | None = None,
    settings: Settings | None = None,
) -> bytes | None:
    """Generate a text-free concept illustration, or ``None`` on any failure.

    Args:
        illustration_prompt: The conceptual, text-free prompt from the synthesis
            IMAGE-DECISION pass. Must be non-empty — an empty prompt is a caller
            bug and returns ``None`` (nothing to generate) after logging.
        client: The image client to use. Injected for testing so ``Brahmastra
            ImageClient`` is mocked and no real model is ever called (§22).
            Defaults to a fresh client wired to ``settings``.
        settings: Config source (style guide + image model). Defaults to the
            process-wide singleton.

    Returns:
        Raw PNG/JPEG bytes on success, or ``None`` when generation is skipped or
        fails — the caller MUST treat ``None`` as "post without an image"
        (graceful degradation, BRD §13.6). This function never raises for a
        generation failure.
    """
    settings = settings or get_settings()

    # An empty concept prompt has nothing to render — skip rather than call the
    # model with a blank prompt.
    if not illustration_prompt or not illustration_prompt.strip():
        logger.warning("empty illustration prompt; skipping concept-illustration")
        return None

    client = client or BrahmastraImageClient(settings)
    prompt = _build_prompt(illustration_prompt, settings.image_style_guide)

    try:
        return client.illustrate(prompt)
    except ImageGenerationError as exc:
        # The one failure class the client raises → degrade to text-only. We log
        # loudly (the run continues) but never propagate, so publishing proceeds.
        logger.warning("concept-illustration failed; degrading to text-only: %s", exc)
        return None
