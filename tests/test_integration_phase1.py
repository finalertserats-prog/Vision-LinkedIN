"""End-to-end INTEGRATION test for VISION's Phase-1 pipeline, fully offline.

WHY this test exists: the unit suites prove each stage (curate / synthesise /
visuals) in isolation. This test proves they compose — it drives the *real*
curate selector, the *real* synthesis orchestrator, and the *real* deterministic
card renderer against a hermetic in-memory SQLite corpus, with the ONLY external
dependency (the Brahmastra model CLI) replaced by a mock returning canned JSON
grounded in the seeded fixture items. No network, no subprocess, no real model
(BRD §18/§22 — mock external deps, tests are part of "done").

Flow proven (BRD §10.2 daily pipeline, minus ingest/email):

    seed both lanes (SQLite)  ->  curate.select_top  ->  synthesise (mocked LLM)
      ->  quality gate + §14.4 report + model_trace  ->  image-decision gate
      ->  deterministic card render (exact fixture numbers)

All assertions follow AAA (Arrange -> Act -> Assert).
"""

from __future__ import annotations

import copy
import io
from typing import Any

from PIL import Image

from vision.config import Settings, SignatureMode
from vision.curate.score import ScoringConfig
from vision.curate.select import select_top
from vision.synthesise.pipeline import synthesise
from vision.synthesise.prompts import PromptLibrary
from vision.visuals.card_renderer import LINKEDIN_LANDSCAPE, render_stat_card
from vision.visuals.decide import CardSpec, Datapoint, ImageType, image_decision

from tests.conftest import PHASE1_NOW, Phase1Fixture, SeededItem

# PNG 8-byte magic number — a rendered card must begin with this to be a real PNG.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Fixtures / doubles.
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    """Pinned settings so lane routing, grounding floor, and palette are fixed.

    WHY explicit construction (not the env singleton): the integration must be
    reproducible on any machine regardless of a developer's ``.env`` — every knob
    the pipeline reads is nailed down here.
    """
    return Settings(
        MODEL_GENERATE="gemini",
        MODEL_CRITIQUE="codex",
        MODEL_VERIFY="claude",
        GROUNDING_MIN_PCT=100,
        CARD_BRAND_PALETTE="navy=#0B1F3A;gold=#C9A24B",
        # OFF keeps the render independent of any watermark logo file on disk.
        POST_SIGNATURE_MODE=SignatureMode.OFF,
    )


class _FakeBrahmastra:
    """Drop-in double for ``BrahmastraClient`` returning canned JSON per pass.

    Mirrors the real adapter's surface (``generate`` / ``critique`` / ``verify``)
    and disambiguates the image pass — which also rides ``critique`` — by the RAFT
    heading present in the rendered prompt ("IMAGE DECISION"). Every response is
    deep-copied on the way out so a caller mutating a result cannot corrupt the
    canned source (tests must not share mutable state).
    """

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str | None]] = []

    def generate(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("generate", lane)

    def critique(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        # The image pass and the critique pass share this method; the image prompt
        # is the only one carrying the "IMAGE DECISION" RAFT heading.
        key = "image" if "IMAGE DECISION" in prompt else "critique"
        return self._serve(key, lane)

    def verify(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("verify", lane)

    def _serve(self, key: str, lane: str | None) -> dict[str, Any]:
        self.calls.append((key, lane))
        return copy.deepcopy(self._responses[key])


# ---------------------------------------------------------------------------
# Canned-response builder — grounded in the ACTUAL selected fixture items.
# ---------------------------------------------------------------------------


def _to_synth_item(seeded: SeededItem) -> dict[str, Any]:
    """Map a seeded ORM ``Item`` onto the dict shape the synthesis passes expect.

    The pipeline feeds the model a list of ``{source_item_id, title, url, ...}``
    dicts; ``source_item_id`` is the item's real UUID so the mocked model's claims
    can cite genuine provenance (which the grounding gate then verifies).
    """
    item = seeded.item
    return {
        "source_item_id": str(item.id),
        "title": item.title,
        "url": item.url,
        "source": getattr(item.source, "name", None),
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "summary": item.summary,
    }


def _build_responses(selected: list[SeededItem]) -> dict[str, dict[str, Any]]:
    """Build realistic canned generate/critique/verify/image JSON for ``selected``.

    Every claim, grounded verdict, and card datapoint cites a REAL selected item
    UUID and uses that item's exact fixture number — so the pipeline's grounding
    gate genuinely passes on real provenance, and the rendered card carries the
    exact figures the corpus promised (BRD §13.6 precision-first).
    """
    ids = [str(s.item.id) for s in selected]

    # One claim per selected item, phrased around its exact fixture figure.
    claims = [
        {"text": f"{s.label.lower()} was {s.number}", "source_item_id": str(s.item.id)}
        for s in selected
    ]

    # A body that mentions every figure so the post reads as grounded and the
    # image-decision gate detects concrete numbers.
    body = (
        "Two signals stood out across the healthcare and AI lanes today. "
        + " ".join(f"{s.label} landed at {s.number}." for s in selected)
        + " Both are operational wins a leader can act on, not hype."
    )
    hashtags = ["#HealthcareAI", "#RevenueCycle", "#DigitalHealth"]

    generate = {
        "hook": f"{selected[0].label} moved to {selected[0].number} — here is what it means.",
        "body": body,
        "takeaway": "Pilot one automation on your highest-friction workflow this month.",
        "hashtags": hashtags,
        "claims": claims,
    }
    critique = {
        "revised": copy.deepcopy(generate),
        "change_log": ["Sharpened the hook", "Cut one hedge"],
        "voice_flags": [],
    }
    grounded = [
        {"text": c["text"], "source_item_id": c["source_item_id"], "verbatim_ok": True}
        for c in claims
    ]
    verify = {
        "grounded": grounded,
        "unsupported": [],
        "revised_post": {
            "hook": generate["hook"],
            "body": body,
            "takeaway": generate["takeaway"],
            "hashtags": hashtags,
        },
        "grounding_pct": 100.0,
        "confidence": 0.9,
    }
    # Informative-card decision: one grounded datapoint per selected item, each
    # carrying the exact fixture number and its provenance id.
    datapoints = [
        {"label": s.label, "value": s.number, "source_item_id": str(s.item.id)}
        for s in selected
    ]
    image = {
        "image_type": "informative-card",
        "rationale": "Post centres on concrete, comparable figures — render deterministically.",
        "card_spec": {"title": "Today's grounded signals", "datapoints": datapoints},
        "illustration_prompt": None,
    }

    # Sanity: the canned provenance really does reference the selected items.
    assert {c["source_item_id"] for c in claims} == set(ids)
    return {"generate": generate, "critique": critique, "verify": verify, "image": image}


def _visuals_card_spec(card_spec: dict[str, Any]) -> CardSpec:
    """Adapt the synthesis image ``card_spec`` dict into a visuals ``CardSpec``.

    The synthesis and visuals lanes define structurally-identical card schemas;
    this bridge re-validates the synthesised spec through the renderer's own
    model (label/value/source_item_id), keeping the render strictly grounded.
    """
    return CardSpec(
        title=str(card_spec["title"]),
        datapoints=[
            Datapoint(
                label=str(point["label"]),
                value=str(point["value"]),
                source_item_id=str(point["source_item_id"]),
            )
            for point in card_spec["datapoints"]
        ],
    )


# ---------------------------------------------------------------------------
# The end-to-end test.
# ---------------------------------------------------------------------------


def test_phase1_pipeline_runs_end_to_end_offline(phase1_fixture: Phase1Fixture) -> None:
    # --- Arrange: pinned config + the real curate selector over both lanes ---
    settings = _settings()
    all_items = [s.item for s in phase1_fixture.seeded]

    # --- Act 1: CURATE — lane-balanced top-2 selection ----------------------
    selection = select_top(
        all_items,
        k=2,
        config=ScoringConfig.load(settings),
        session=phase1_fixture.session,
        now=PHASE1_NOW,
    )

    # --- Assert 1: both lanes represented, and the DB rows were marked -------
    selected_lanes = {item.lane for item in selection.selected}
    assert selected_lanes == {"hc", "ai"}  # blended HC x AI post (BRD §13.2)
    assert len(selection.selected) == 2
    assert all(item.selected is True for item in selection.selected)

    # Re-associate each selected ORM item with its seeded figure for later asserts.
    by_id = {str(s.item.id): s for s in phase1_fixture.seeded}
    selected_seeded = [by_id[str(item.id)] for item in selection.selected]
    synth_items = [_to_synth_item(s) for s in selected_seeded]

    # --- Act 2: SYNTHESISE — generate -> critique -> verify -> image ---------
    # The ONLY external dependency (Brahmastra) is mocked; its canned JSON is
    # grounded in the exact selected item UUIDs.
    client = _FakeBrahmastra(_build_responses(selected_seeded))
    draft = synthesise(
        "Revenue-cycle management",
        synth_items,
        client=client,
        settings=settings,
        prompts=PromptLibrary.default(),
    )

    # --- Assert 2a: a well-formed Draft-shaped dict -------------------------
    expected_keys = {
        "lane_focus",
        "post_text",
        "hashtags",
        "source_item_ids",
        "quality_report",
        "confidence",
        "auto_eligible",
        "model_trace",
        "image_type",
        "image_prompt",
        "image_decision",
    }
    assert expected_keys <= set(draft)
    assert draft["lane_focus"] == "Revenue-cycle management"
    assert isinstance(draft["post_text"], str) and draft["post_text"].strip()
    # Provenance ids are the real selected item UUIDs (the grounded set).
    assert set(draft["source_item_ids"]) == {str(s.item.id) for s in selected_seeded}

    # --- Assert 2b: 100% grounding gate passes ------------------------------
    assert draft["quality_report"]["grounding_pct"] == 100.0
    assert draft["auto_eligible"] is True  # 100 >= GROUNDING_MIN_PCT

    # --- Assert 2c: quality_report matches the BRD §14.4 shape --------------
    assert set(draft["quality_report"]) == {
        "char_count",
        "has_hook",
        "grounding_pct",
        "unsupported_claims",
        "tone_flags",
        "compliance_flags",
        "hashtags",
        "confidence",
    }
    assert draft["quality_report"]["has_hook"] is True
    assert draft["quality_report"]["unsupported_claims"] == []

    # --- Assert 2d: model_trace present, one entry per pass on its lane ------
    trace = draft["model_trace"]
    assert set(trace) == {"generate", "critique", "verify", "image"}
    assert trace["generate"] == {"lane": "gemini", "degraded": False}
    assert trace["critique"] == {"lane": "codex", "degraded": False}
    assert trace["verify"] == {"lane": "claude", "degraded": False}
    assert trace["image"] == {"lane": "codex", "degraded": False}

    # --- Act 3: IMAGE-DECISION gate -> deterministic card render ------------
    decision = draft["image_decision"]
    assert draft["image_type"] == "informative-card"

    grounded_claim_texts = [
        f"{point['label']} {point['value']}"
        for point in decision["card_spec"]["datapoints"]
    ]
    final_type = image_decision(draft["post_text"], grounded_claim_texts, decision)
    # Numbers present + grounded datapoints -> precision-first card (never diffusion).
    assert final_type is ImageType.INFORMATIVE_CARD

    card_spec = _visuals_card_spec(decision["card_spec"])
    png = render_stat_card(card_spec, settings=settings)

    # --- Assert 3: a non-empty PNG at exact LinkedIn dims, exact figures -----
    assert png.startswith(_PNG_MAGIC)
    assert len(png) > 1000  # a real rendered card is substantial, never empty
    with Image.open(io.BytesIO(png)) as rendered:
        assert rendered.size == LINKEDIN_LANDSCAPE  # (1200, 627)

    # Every rendered value is exactly a seeded fixture number for a selected item.
    rendered_values = {point.value for point in card_spec.datapoints}
    expected_numbers = {s.number for s in selected_seeded}
    assert rendered_values == expected_numbers
    # And each rendered figure is traceable to a real selected item (provenance).
    assert {point.source_item_id for point in card_spec.datapoints} == {
        str(s.item.id) for s in selected_seeded
    }
