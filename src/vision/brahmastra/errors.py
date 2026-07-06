"""Exception types for the Brahmastra adapter layer.

WHY this module exists: BRD §22.5 mandates *deterministic LLM contracts* — a
model pass that fails to return the agreed strict JSON must *fail loudly*, not
degrade silently. Isolating the adapter's exception hierarchy here lets every
caller (synthesis chain, image lane) catch exactly the failure class it can
handle — a text-generation contract breach vs. a best-effort image miss — and
keeps VISION coupled to the adapter's stable surface, never to Brahmastra's
internals (§13.0).
"""

from __future__ import annotations


class BrahmastraError(Exception):
    """Raised when a Brahmastra text pass cannot yield a valid JSON contract.

    WHY a dedicated type: the synthesis chain (generate → critique → verify)
    depends on strict JSON output. A non-JSON / empty / drifted response is a
    hard failure the pipeline must surface (fail-closed, §22.9) rather than
    guess around. Callers catch this specifically to abort the run and alert,
    never to paper over bad model output.
    """


class ImageGenerationError(Exception):
    """Raised when concept-illustration generation fails.

    WHY separate from ``BrahmastraError``: image generation is explicitly a
    *degrade-gracefully* path (BRD §13.6 / FR-23) — a failed illustration must
    never block publishing. Callers catch this to fall back to a text-only post,
    a fundamentally different recovery than a synthesis-contract breach.
    """
