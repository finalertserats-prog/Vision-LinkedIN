"""Synthesis orchestration: generate -> critique -> verify -> image (BRD §13.1).

WHY this module exists: it is the one place that wires the three (plus image)
Brahmastra passes together into a Draft-shaped dict the rest of VISION persists.
It owns three responsibilities and nothing else:

  1. Route each pass to its configured lane (``MODEL_GENERATE/CRITIQUE/VERIFY``)
     for genuine cross-model checking (§13.1), degrading to another working lane
     if the primary is unavailable — and recording that degradation in
     ``model_trace`` (§13.0) so the provenance of every post is auditable.
  2. Validate every pass output against its strict schema, failing loudly on
     drift (§22.5) by raising ``BrahmastraError``.
  3. Compute the quality report + grounding eligibility (delegated to
     ``quality``) and assemble the final Draft dict.

It performs NO I/O beyond the injected ``BrahmastraClient`` — persistence is the
caller's job — which keeps it unit-testable with a mocked client.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from vision.brahmastra.client import BrahmastraClient
from vision.brahmastra.errors import BrahmastraError
from vision.config import Settings, get_settings
from vision.synthesise.prompts import PromptLibrary
from vision.synthesise.quality import (
    assemble_post_text,
    build_quality_report,
    is_auto_eligible,
)
from vision.synthesise.schemas import (
    CritiqueOut,
    GenerateOut,
    ImageDecision,
    VerifyOut,
)

logger = logging.getLogger(__name__)

# A CLI-lane invocation: takes (prompt, lane) and returns the parsed dict. All
# three ``BrahmastraClient`` methods share this shape, which lets ``_run_pass``
# treat every pass uniformly.
_LaneCall = Callable[..., dict[str, Any]]


def _lane_candidates(settings: Settings) -> list[str]:
    """Return the configured lanes, de-duplicated in generate->verify order.

    WHY this order + dedup: the fallback logic tries the primary lane first, then
    the remaining *configured* lanes. Preserving configuration order (generate,
    critique, verify) means degradation prefers a lane the owner already trusts,
    and dedup avoids retrying the identical lane when passes share one.
    """
    ordered = [settings.model_generate, settings.model_critique, settings.model_verify]
    seen: set[str] = set()
    unique: list[str] = []
    for lane in ordered:
        if lane not in seen:
            seen.add(lane)
            unique.append(lane)
    return unique


def _invoke_with_fallback(
    call: _LaneCall,
    prompt: str,
    primary_lane: str,
    candidates: list[str],
    pass_name: str,
) -> tuple[dict[str, Any], str, bool]:
    """Run ``call`` on ``primary_lane``, degrading to another lane if it fails.

    BRD §13.0 requires the pipeline to keep running when a lane is unavailable by
    degrading to a single working lane — WITH the pass's own distinct prompt (the
    prompt is already pass-specific, so the fallback lane still receives genuine
    critique/verify instructions, not a copy of the generate pass).

    Returns ``(raw_dict, lane_used, degraded)``. Raises ``BrahmastraError`` only
    when EVERY candidate lane fails — a total outage the run cannot paper over.
    """
    # Primary first, then the other configured lanes in order — never the same
    # lane twice.
    lanes_to_try = [primary_lane] + [lane for lane in candidates if lane != primary_lane]

    last_error: BrahmastraError | None = None
    for index, lane in enumerate(lanes_to_try):
        try:
            raw = call(prompt, lane=lane)
        except BrahmastraError as exc:
            # A lane-level failure (unavailable/empty/timeout) is recoverable if
            # another lane works; record it and try the next candidate.
            last_error = exc
            logger.warning(
                "synthesis lane unavailable; attempting fallback",
                extra={"pass": pass_name, "lane": lane, "error": str(exc)},
            )
            continue
        # index > 0 means we did NOT get the primary lane — a degraded pass.
        return raw, lane, index > 0

    # Every candidate lane failed — fail loudly (fail-closed, §22.9).
    raise BrahmastraError(
        f"all lanes exhausted for {pass_name} pass ({last_error})"
    ) from last_error


def _run_pass(
    call: _LaneCall,
    prompt: str,
    primary_lane: str,
    candidates: list[str],
    schema: type[BaseModel],
    pass_name: str,
    trace: dict[str, Any],
) -> BaseModel:
    """Execute one pass end-to-end: invoke (with fallback) + validate + trace.

    Validation drift is converted into ``BrahmastraError`` so callers handle a
    single contract-breach type whether the model returned non-JSON (raised in
    the client) or JSON of the wrong shape (raised here) — both are the same
    class of failure: the pass did not honour its contract (§22.5).
    """
    raw, lane_used, degraded = _invoke_with_fallback(
        call, prompt, primary_lane, candidates, pass_name
    )
    try:
        validated = schema.model_validate(raw)
    except ValidationError as exc:
        # Schema drift is a hard failure — surface it, never publish around it.
        raise BrahmastraError(
            f"{pass_name} pass returned JSON violating {schema.__name__}: {exc}"
        ) from exc

    # Record provenance per BRD §13.0: which lane served the pass and whether it
    # was a degraded fallback.
    trace[pass_name] = {"lane": lane_used, "degraded": degraded}
    return validated


def _grounded_item_ids(verify: VerifyOut, items: list[dict[str, Any]]) -> list[str]:
    """Derive the draft's provenance ids from the verifier's grounded claims.

    WHY prefer grounded claims over the raw item feed: the published post cites
    only the claims that survived verification, so the true provenance is the set
    of ``source_item_id`` on grounded claims. If the verifier grounded nothing
    (degenerate case), fall back to the supplied item ids so provenance is never
    empty. Order is preserved and ids de-duplicated.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for claim in verify.grounded:
        if claim.source_item_id and claim.source_item_id not in seen:
            seen.add(claim.source_item_id)
            ids.append(claim.source_item_id)
    if ids:
        return ids

    # Fallback: no grounded claims → use the input item ids for traceability.
    for item in items:
        item_id = item.get("source_item_id")
        if item_id and item_id not in seen:
            seen.add(item_id)
            ids.append(item_id)
    return ids


def _assert_card_datapoints_grounded(image: ImageDecision, verify: VerifyOut) -> None:
    """Reject any card datapoint whose ``source_item_id`` is not grounded (§13.6).

    WHY (RAFT precision-first contract, fail-closed §22.9): a deterministic
    informative card renders numbers straight from ``card_spec.datapoints``. Every
    such number MUST trace to a claim the verifier grounded — otherwise the card
    could publish a fabricated figure with no lineage, the exact failure the whole
    grounding guarantee (NFR-01) exists to prevent. We check the id membership
    here (not in the schema) because the grounded set lives in a *different* pass'
    output than the image pass, so only the pipeline has both in hand.
    """
    if image.card_spec is None:
        return

    grounded_ids = {claim.source_item_id for claim in verify.grounded}
    for datapoint in image.card_spec.datapoints:
        if datapoint.source_item_id not in grounded_ids:
            # Fail loudly: a rendered number with no grounded source is a
            # contract breach on par with schema drift.
            raise BrahmastraError(
                "image card datapoint cites a non-grounded source_item_id "
                f"{datapoint.source_item_id!r}; every card number must trace to a "
                f"grounded claim (grounded ids: {sorted(grounded_ids)})"
            )


def synthesise(
    focus: str,
    items: list[dict[str, Any]],
    *,
    client: BrahmastraClient | None = None,
    settings: Settings | None = None,
    prompts: PromptLibrary | None = None,
) -> dict[str, Any]:
    """Run the full synthesis chain and return a Draft-shaped dict (§13.1/§11.4).

    Args:
        focus: The day's rotating focus that anchors the post (§13.2).
        items: Selected source items, each a dict with at least
            ``source_item_id`` (plus title/url/source/summary for the model).
        client: Brahmastra adapter; injected in tests so no real model is called.
        settings: Config source for lanes + grounding floor; defaults to the
            process singleton.
        prompts: Loaded RAFT + voice config; defaults to the staged ``prep/`` files.

    Returns:
        A dict shaped for ``drafts`` persistence: ``post_text``, ``hashtags``,
        ``source_item_ids``, ``quality_report``, ``confidence``, ``auto_eligible``,
        ``model_trace``, plus the image-lane decision.

    Raises:
        BrahmastraError: if any pass yields no usable output on any lane, or its
            output violates its schema (fail loudly, §22.5/§22.9).
    """
    settings = settings or get_settings()
    client = client or BrahmastraClient(settings)
    prompts = prompts or PromptLibrary.default()
    candidates = _lane_candidates(settings)

    # ``model_trace`` accumulates per-pass provenance as each pass runs (§13.0).
    trace: dict[str, Any] = {}

    # -- Pass 1: GENERATE --------------------------------------------------
    generate = _run_pass(
        client.generate,
        prompts.generate_prompt(focus, items),
        settings.model_generate,
        candidates,
        GenerateOut,
        "generate",
        trace,
    )
    assert isinstance(generate, GenerateOut)  # narrow the type for mypy/readers

    # -- Pass 2: CRITIQUE --------------------------------------------------
    critique = _run_pass(
        client.critique,
        prompts.critique_prompt(focus, items, generate.model_dump()),
        settings.model_critique,
        candidates,
        CritiqueOut,
        "critique",
        trace,
    )
    assert isinstance(critique, CritiqueOut)

    # -- Pass 3: VERIFY ----------------------------------------------------
    verify = _run_pass(
        client.verify,
        prompts.verify_prompt(focus, items, critique.revised.model_dump()),
        settings.model_verify,
        candidates,
        VerifyOut,
        "verify",
        trace,
    )
    assert isinstance(verify, VerifyOut)

    # -- Pass 4: IMAGE DECISION -------------------------------------------
    # Short pass; routed to the critique lane (§13.6) with the same fallback so a
    # single-lane degrade still produces an image decision.
    grounded = [claim.model_dump() for claim in verify.grounded]
    image = _run_pass(
        client.critique,
        prompts.image_prompt(verify.revised_post.model_dump(), grounded),
        settings.model_critique,
        candidates,
        ImageDecision,
        "image",
        trace,
    )
    assert isinstance(image, ImageDecision)

    # Every number on an informative card must trace to a grounded claim (§13.6);
    # reject fabricated card provenance before assembling the Draft.
    _assert_card_datapoints_grounded(image, verify)

    # -- Assemble the Draft dict ------------------------------------------
    final = verify.revised_post
    post_text = assemble_post_text(final.hook, final.body, final.takeaway, final.hashtags)
    quality_report = build_quality_report(
        post_text, list(final.hashtags), verify, critique, prompts.voice
    )

    return {
        "lane_focus": focus,
        "post_text": post_text,
        "hashtags": list(final.hashtags),
        "source_item_ids": _grounded_item_ids(verify, items),
        "quality_report": quality_report,
        "confidence": verify.confidence,
        # Eligibility for auto-publish, computed SERVER-SIDE from the verifier's
        # own claim counts — NOT its self-reported grounding_pct (§13.5/NFR-01,
        # fail-closed §22.9). Below the floor the draft still exists but is routed
        # to manual approval.
        "auto_eligible": is_auto_eligible(verify, settings),
        "model_trace": trace,
        # Image lane outputs mapped onto the Draft's image columns (§13.6); the
        # full decision (incl. card_spec) is kept for the deterministic renderer.
        "image_type": image.image_type,
        "image_prompt": image.illustration_prompt,
        "image_decision": image.model_dump(),
    }
