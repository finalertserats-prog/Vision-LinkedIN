"""Visual-lane decision logic + its strict pydantic contract (BRD §13.6, D8/D10).

WHY this module exists: the synthesis chain's *IMAGE DECISION* pass (Pass 4,
``prep/raft_prompts.md``) emits a JSON blob choosing one of three visual
outcomes — ``none`` (default), ``informative-card`` (deterministic stat/chart),
or ``concept-illustration`` (text-free diffusion image). This module owns:

  1. The **pydantic schema** for that pass output (``ImageDecision`` and its
     nested ``CardSpec`` / ``Datapoint``), so the LLM contract is validated and
     fails loudly on drift (BRD §22.5 deterministic contracts). Defined here —
     not imported from ``synthesise`` — because the visuals lane must be
     usable/testable in isolation; ``synthesise/schemas.py`` (a later phase) can
     re-export these once it lands.
  2. The **precision-first gate** ``image_decision`` — the single place that
     turns a *proposed* decision into the *final* rendered outcome, enforcing the
     hard rule (BRD §13.6, D10): anything that must show numbers or words is
     rendered DETERMINISTICALLY as a card, NEVER handed to a diffusion model.

The gate is deliberately conservative: on any ambiguity or missing data it
degrades to ``none`` (text-only post) rather than risk an ungrounded visual.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# A "renderable number" is any digit run in the post/claim text. WHY a digit
# scan (not NLP): the rule is deterministic by design — the presence of a
# concrete figure is what forces the deterministic card path, and a regex is the
# most auditable, reproducible detector (§22 deterministic contracts).
_DIGIT_RE = re.compile(r"\d")


class ImageType(str, Enum):
    """The three mutually-exclusive visual outcomes (BRD §13.6).

    Using an enum (not free strings) means an out-of-range value fails loudly at
    validation time instead of silently mis-routing the renderer.
    """

    NONE = "none"  # safest default — text-only post
    INFORMATIVE_CARD = "informative-card"  # deterministic Pillow/matplotlib render
    CONCEPT_ILLUSTRATION = "concept-illustration"  # text-free diffusion image


class Datapoint(BaseModel):
    """One label/value pair to render on a card or chart, with its provenance.

    ``source_item_id`` is intentionally OPTIONAL at the schema level but REQUIRED
    by the renderer (``card_renderer`` asserts its presence). WHY split the
    enforcement: a model can be *constructed* from partial LLM output for
    inspection, but nothing is ever *rendered* without a traceable source
    (BRD §13.6 — every number on a card must trace to a grounded item).
    """

    # extra='forbid' → any unexpected key from the LLM is drift and fails loudly.
    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., description="Human label, e.g. 'FDA approvals 2024'.")
    # Kept as a string because the LLM contract emits values as strings (e.g.
    # '42%', '$1.2B'); ``numeric`` parses it on demand for charts.
    value: str = Field(..., description="Displayed value as a string, e.g. '42%'.")
    source_item_id: str | None = Field(
        default=None, description="Item id grounding this datapoint (required to render)."
    )

    def numeric(self) -> float:
        """Parse ``value`` into a float for chart plotting.

        Strips common presentation characters (``%``, ``$``, thousands commas,
        surrounding whitespace) before parsing. Raises ``ValueError`` — never
        guesses — when the value is not a plain number, so a chart request over
        non-numeric data fails loudly rather than plotting garbage.
        """
        cleaned = re.sub(r"[,$%\s]", "", self.value)
        try:
            return float(cleaned)
        except ValueError as exc:
            # Specific exception, explicit message — no bare except (§22).
            raise ValueError(
                f"Datapoint value {self.value!r} is not numeric and cannot be charted"
            ) from exc


class CardSpec(BaseModel):
    """The content of an informative card / chart (title + grounded datapoints)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., description="Card headline.")
    datapoints: list[Datapoint] = Field(
        default_factory=list, description="Grounded label/value pairs to render."
    )
    # Optional single quote/stat used by the quote-card variant.
    quote: str | None = Field(default=None, description="Optional pull-quote text.")
    # Optional chart hint: 'bar' | 'line' when the card should render as a chart.
    chart_type: str | None = Field(
        default=None, description="'bar' | 'line' to render a chart instead of a stat card."
    )
    # Attribution shown discreetly at the card footer (e.g. 'Source: STAT News').
    source_label: str | None = Field(
        default=None, description="Discreet attribution shown on the card."
    )


class ImageDecision(BaseModel):
    """Strict contract for the synthesis IMAGE-DECISION pass (Pass 4).

    Mirrors the JSON in ``prep/raft_prompts.md``. ``extra='forbid'`` makes any
    unexpected key a hard failure so model drift is caught at the boundary
    (BRD §22.5) rather than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    image_type: ImageType = Field(
        default=ImageType.NONE, description="Chosen visual outcome (default 'none')."
    )
    rationale: str | None = Field(default=None, description="One-sentence justification.")
    card_spec: CardSpec | None = Field(
        default=None, description="Populated only for informative-card outcomes."
    )
    illustration_prompt: str | None = Field(
        default=None, description="Text-free style prompt for concept-illustration."
    )


def _has_renderable_datapoints(decision: ImageDecision) -> bool:
    """True when the decision carries at least one datapoint to render.

    Extracted as a named predicate so the gate below reads as intent, not as a
    chain of attribute pokes.
    """
    return decision.card_spec is not None and len(decision.card_spec.datapoints) > 0


def _mentions_numbers(post: str, claims: Sequence[str]) -> bool:
    """True when the post or any claim contains a concrete number.

    WHY: the precision-first rule (BRD §13.6, D10) says a visual that would carry
    numbers must be deterministic, never diffusion. A single digit anywhere in
    the copy is enough to trip the conservative override.
    """
    if _DIGIT_RE.search(post):
        return True
    return any(_DIGIT_RE.search(claim) for claim in claims)


def image_decision(
    post: str,
    claims: Sequence[str],
    decision: ImageDecision | dict | None = None,
) -> ImageType:
    """Resolve the synthesis pass's *proposed* image decision into a final one.

    This is the precision-first gate (BRD §13.6, D8/D10). It takes the
    ``ImageDecision`` produced by the synthesis IMAGE-DECISION pass and returns
    the outcome the renderer must actually honour, applying two hard rules:

      * **Safe default** — if no decision is supplied, it is unparseable, or the
        chosen type is ``none``, the result is ``ImageType.NONE`` (text-only).
      * **Never diffusion for numbers/words** — if the post or claims contain a
        concrete number, a ``concept-illustration`` request is overridden: it
        becomes an ``informative-card`` when grounded datapoints exist, otherwise
        it degrades to ``none``. A diffusion model is never asked to render a
        figure it could get subtly wrong.

    Args:
        post: The final post text (used to detect numeric content).
        claims: The grounded claims backing the post (also scanned for numbers).
        decision: The synthesis pass output — an ``ImageDecision``, a raw dict
            (validated here), or ``None``. Invalid dicts degrade to ``none``.

    Returns:
        The ``ImageType`` the visuals lane must render (or ``NONE`` to skip).
    """
    # --- Normalise the (possibly absent / raw / malformed) input -----------
    if decision is None:
        return ImageType.NONE
    if isinstance(decision, dict):
        try:
            decision = ImageDecision.model_validate(decision)
        except ValueError as exc:
            # Malformed decision → degrade to text-only rather than fail the run;
            # the image lane is a best-effort enhancement (BRD §13.6), and we log
            # loudly so the drift is diagnosable.
            logger.warning("invalid image decision payload; defaulting to none: %s", exc)
            return ImageType.NONE

    proposed = decision.image_type

    # --- Rule 1: explicit skip / safe default ------------------------------
    if proposed is ImageType.NONE:
        return ImageType.NONE

    # --- Rule 2: informative-card must actually have data to render --------
    if proposed is ImageType.INFORMATIVE_CARD:
        if _has_renderable_datapoints(decision):
            return ImageType.INFORMATIVE_CARD
        # A card with nothing to show is meaningless → degrade to text-only.
        logger.warning("informative-card requested with no datapoints; defaulting to none")
        return ImageType.NONE

    # --- Rule 3: concept-illustration, with the never-diffusion override ---
    # (proposed is CONCEPT_ILLUSTRATION here — the enum has no other member.)
    if _mentions_numbers(post, claims) or _has_renderable_datapoints(decision):
        # Numbers present → precision-first forbids diffusion. Prefer a
        # deterministic card if we have grounded datapoints, else skip entirely.
        if _has_renderable_datapoints(decision):
            logger.info("overriding concept-illustration → informative-card (numbers present)")
            return ImageType.INFORMATIVE_CARD
        logger.info("numbers present but no datapoints; skipping illustration → none")
        return ImageType.NONE

    # Purely conceptual + a text-free prompt available → allow the illustration.
    if decision.illustration_prompt:
        return ImageType.CONCEPT_ILLUSTRATION

    # Conceptual but no prompt to generate from → nothing to do.
    logger.warning("concept-illustration requested without a prompt; defaulting to none")
    return ImageType.NONE
