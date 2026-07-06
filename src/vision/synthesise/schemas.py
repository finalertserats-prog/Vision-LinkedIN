"""Strict pydantic contracts for each synthesis pass (BRD §13.4, §22.5).

WHY this module exists: every Brahmastra pass MUST return the exact JSON shape
its RAFT prompt promises (see ``prep/raft_prompts.md``). Parsing that output into
these models is the single point where *drift fails loudly* — a missing field, a
wrong type, or an unexpected extra key raises ``pydantic.ValidationError`` instead
of silently flowing a malformed post downstream. ``extra="forbid"`` on every model
means a model that hallucinates additional keys is rejected, not quietly accepted
(§22.9 fail-closed).

The models mirror, 1:1, the four ``Format`` blocks in the RAFT contracts:
  * ``GenerateOut``   — Pass 1 GENERATE
  * ``CritiqueOut``   — Pass 2 CRITIQUE / EDIT
  * ``VerifyOut``     — Pass 3 VERIFY
  * ``ImageDecision`` — Pass 4 IMAGE DECISION
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Base for every synthesis contract: reject unknown keys, fail loudly.

    WHY a shared base: BRD §22.5 requires deterministic LLM contracts. Setting
    ``extra="forbid"`` in ONE place guarantees no pass output can smuggle an
    un-modelled field past validation — the whole point of validating drift.
    """

    model_config = ConfigDict(extra="forbid")


# --- Shared value objects ---------------------------------------------------


class Claim(_StrictModel):
    """A single factual/numeric claim mapped to the source item that grounds it.

    ``source_item_id`` is the provenance link the verify pass re-checks; keeping
    it on every claim is what makes 100%-grounding auditable (BRD §13.5/NFR-01).
    """

    text: str  # the claim exactly as written in the post
    source_item_id: str  # id of the item that supports it (provenance)


# --- Pass 1: GENERATE -------------------------------------------------------


class GenerateOut(_StrictModel):
    """Pass-1 draft: hook + body + takeaway + hashtags + grounded claims.

    Reused as the ``revised`` payload of the critique pass because the critique
    pass returns the SAME draft shape (only edited) — see ``CritiqueOut``.
    """

    hook: str  # one specific opening sentence, no throat-clearing
    body: str  # 3-5 short paragraphs blending HC + AI signals
    takeaway: str  # one concrete action for a healthcare leader/builder
    hashtags: list[str]  # 3-5 tags selected from the voice profile's pool
    claims: list[Claim]  # every factual/numeric claim + its source mapping


# --- Pass 2: CRITIQUE / EDIT ------------------------------------------------


class CritiqueOut(_StrictModel):
    """Pass-2 output: the edited draft plus an audit trail of the edits.

    ``revised`` is a full ``GenerateOut`` because the editor returns the whole
    draft (hook/body/takeaway/hashtags/claims) after sharpening it — the source
    mappings must survive intact (BRD §13.4 Pass-2 Action).
    """

    revised: GenerateOut  # the improved draft, same shape as Pass 1
    change_log: list[str]  # one short bullet per edit made (auditability)
    voice_flags: list[str]  # residual tone/compliance concerns, or empty


# --- Pass 3: VERIFY ---------------------------------------------------------


class GroundedClaim(_StrictModel):
    """A claim the verifier confirmed against its source, verbatim-checked."""

    text: str
    source_item_id: str
    verbatim_ok: bool  # True only if numbers/dates/entities match exactly


class UnsupportedClaim(_StrictModel):
    """A claim the verifier could NOT ground — removed or flagged, never hidden."""

    text: str
    reason: str  # why it fails (missing source, number mismatch, ...)
    # Constrained so an unknown disposition is rejected, not silently accepted.
    action: Literal["removed", "flagged"]


class FinalPost(_StrictModel):
    """The publish-ready post the verifier recomputes to contain only grounded
    claims (no ``claims`` array — provenance now lives in ``VerifyOut.grounded``)."""

    hook: str
    body: str
    takeaway: str
    hashtags: list[str]


class VerifyOut(_StrictModel):
    """Pass-3 output: grounding verdict + the recomputed publish-ready post.

    ``grounding_pct`` drives the auto-eligibility gate (BRD §13.5): only a post
    at ``>= GROUNDING_MIN_PCT`` may auto-publish; anything less is surfaced in
    the approval email, never silently shipped.
    """

    grounded: list[GroundedClaim]
    unsupported: list[UnsupportedClaim]
    revised_post: FinalPost
    # float (not int) so a verifier emitting 99.5 validates; the gate compares
    # numerically against the int ``GROUNDING_MIN_PCT`` either way.
    #
    # WHY the bounds (BRD §22.9 fail-loudly): grounding is a percentage, so any
    # value outside [0, 100] — or a non-finite inf/nan — is a physically
    # impossible verdict that must be rejected at the boundary, never copied into
    # the Draft or fed to the gate. ``allow_inf_nan=False`` closes the nan/inf
    # hole that a bare ``float`` leaves open (nan silently defeats ``>=`` gates).
    grounding_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    # 0-1 overall verifier confidence (§13.5); same finite-range guarantee so a
    # bogus confidence can never propagate downstream.
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


# --- Pass 4: IMAGE DECISION -------------------------------------------------


class DataPoint(_StrictModel):
    """One label/value pair rendered on an informative card, traced to a source.

    Each datapoint MUST cite a grounded claim (``source_item_id``) so numbers on
    a card are as accountable as numbers in the text (BRD §13.6 precision-first).
    """

    label: str
    value: str
    source_item_id: str


class CardSpec(_StrictModel):
    """Exact content for the DETERMINISTIC card renderer (never a diffusion model).

    Present only when ``image_type == 'informative-card'``; otherwise ``None``.
    """

    title: str
    datapoints: list[DataPoint]


class ImageDecision(_StrictModel):
    """Pass-4 output: whether/what image accompanies the post (BRD §13.6).

    ``card_spec`` is populated for ``informative-card`` (deterministic render);
    ``illustration_prompt`` for ``concept-illustration`` (text-free style prompt).
    Both default to ``None`` for the common ``none`` decision.
    """

    # Literal keeps the three legal decisions the only representable values.
    image_type: Literal["none", "informative-card", "concept-illustration"]
    rationale: str  # one-sentence justification for the decision
    card_spec: CardSpec | None = None
    illustration_prompt: str | None = None
