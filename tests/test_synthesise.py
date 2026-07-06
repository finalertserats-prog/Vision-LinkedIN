"""Unit tests for the synthesis chain (generate -> critique -> verify -> image).

WHY these tests: BRD §18/§22 make tests part of "done" and forbid calling a real
model in a unit test. Every test here uses a MOCKED Brahmastra client that
returns canned JSON per pass, so the suite is fully hermetic. We cover the
contract-critical behaviours from the task:

  1. Happy path returns a Draft-shaped dict; ``quality_report`` matches §14.4.
  2. The grounding gate passes/fails against ``GROUNDING_MIN_PCT``.
  3. A banned voice phrase in the final post raises a tone flag.
  4. Schema drift (missing/extra field) fails loudly as ``BrahmastraError``.
  5. ``model_trace`` records every pass' lane.
  6. An unavailable primary lane degrades to a working lane, recorded as degraded.

All tests follow AAA (Arrange -> Act -> Assert). No subprocess, no network.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest
from pydantic import ValidationError

from vision.brahmastra.errors import BrahmastraError
from vision.config import Settings
from vision.synthesise.pipeline import synthesise
from vision.synthesise.prompts import PromptLibrary
from vision.synthesise.quality import (
    assemble_post_text,
    build_quality_report,
    find_banned_phrases,
    passes_grounding_gate,
)
from vision.synthesise.schemas import CritiqueOut, VerifyOut

# --- Canned pass outputs ----------------------------------------------------
# WHY module-level factories (returning deep copies): tests must not share mutable
# state (testing rules), so each test gets a fresh, independent set of responses.

_ITEMS: list[dict[str, Any]] = [
    {
        "source_item_id": "item-1",
        "title": "Hospital cuts claim-denials with automation",
        "url": "https://example.test/a",
        "source": "STAT News",
        "published_at": "2026-07-05T10:00:00Z",
        "summary": "A 200-bed hospital reduced denials by 18% using RCM automation.",
    },
    {
        "source_item_id": "item-2",
        "title": "New model benchmarks on clinical notes",
        "url": "https://example.test/b",
        "source": "Import AI",
        "published_at": "2026-07-05T11:00:00Z",
        "summary": "An open model matched prior systems on note summarisation.",
    },
]


def _generate_json() -> dict[str, Any]:
    """A valid Pass-1 GENERATE payload."""
    return {
        "hook": "Denials fell 18% at one hospital after RCM automation.",
        "body": "Here is what stood out today across the HC and AI lanes.",
        "takeaway": "Pilot one automation on your highest-denial payer this month.",
        "hashtags": ["#RevenueCycle", "#HealthcareAI", "#ClinicalOps"],
        "claims": [
            {"text": "denials fell 18%", "source_item_id": "item-1"},
            {"text": "an open model matched prior systems", "source_item_id": "item-2"},
        ],
    }


def _critique_json() -> dict[str, Any]:
    """A valid Pass-2 CRITIQUE payload wrapping the revised draft."""
    return {
        "revised": _generate_json(),
        "change_log": ["Sharpened the hook", "Trimmed one hedge"],
        "voice_flags": [],
    }


def _verify_json(grounding_pct: float = 100.0, body: str | None = None) -> dict[str, Any]:
    """A valid Pass-3 VERIFY payload; ``body``/``grounding_pct`` overridable."""
    return {
        "grounded": [
            {"text": "denials fell 18%", "source_item_id": "item-1", "verbatim_ok": True},
            {
                "text": "an open model matched prior systems",
                "source_item_id": "item-2",
                "verbatim_ok": True,
            },
        ],
        "unsupported": [],
        "revised_post": {
            "hook": "Denials fell 18% at one hospital after RCM automation.",
            "body": body if body is not None else "A grounded, hype-free walk-through.",
            "takeaway": "Pilot one automation on your highest-denial payer this month.",
            "hashtags": ["#RevenueCycle", "#HealthcareAI", "#ClinicalOps"],
        },
        "grounding_pct": grounding_pct,
        "confidence": 0.88,
    }


def _image_json() -> dict[str, Any]:
    """A valid Pass-4 IMAGE-DECISION payload (the common 'none' case)."""
    return {
        "image_type": "none",
        "rationale": "Narrative post with no single dominant stat.",
        "card_spec": None,
        "illustration_prompt": None,
    }


class _FakeBrahmastra:
    """A drop-in double for ``BrahmastraClient`` returning canned JSON per pass.

    WHY marker-based routing: the image pass rides ``critique`` (same method), so
    we disambiguate by the RAFT heading present in the rendered prompt
    ("IMAGE DECISION"). ``unavailable`` lets a test simulate a down lane so the
    pipeline's degrade path is exercised without any real subprocess.
    """

    def __init__(
        self,
        responses: dict[str, dict[str, Any]],
        *,
        unavailable: frozenset[str] = frozenset(),
    ) -> None:
        self._responses = responses
        self._unavailable = set(unavailable)
        self.calls: list[tuple[str, str | None]] = []

    def generate(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("generate", lane)

    def critique(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        # The image pass and the critique pass both call this method; the image
        # prompt carries the "IMAGE DECISION" heading, the critique prompt does not.
        key = "image" if "IMAGE DECISION" in prompt else "critique"
        return self._serve(key, lane)

    def verify(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return self._serve("verify", lane)

    def _serve(self, key: str, lane: str | None) -> dict[str, Any]:
        self.calls.append((key, lane))
        # Simulate a down lane exactly as the real client would: raise loudly.
        if lane in self._unavailable:
            raise BrahmastraError(f"lane {lane!r} unavailable")
        # Deep-copy so a test mutating the result can't corrupt the canned source.
        return copy.deepcopy(self._responses[key])


def _settings(grounding_min_pct: int = 100) -> Settings:
    """Pinned settings so lane routing + the grounding floor are deterministic."""
    return Settings(
        MODEL_GENERATE="gemini",
        MODEL_CRITIQUE="codex",
        MODEL_VERIFY="claude",
        GROUNDING_MIN_PCT=grounding_min_pct,
    )


def _prompts() -> PromptLibrary:
    """Real prompt library over the staged prep/ files (exercises prompts.py too)."""
    return PromptLibrary.default()


def _responses(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the full canned response set, allowing per-pass overrides."""
    base = {
        "generate": _generate_json(),
        "critique": _critique_json(),
        "verify": _verify_json(),
        "image": _image_json(),
    }
    base.update(overrides)
    return base


# --- 1. Happy path + Draft shape + §14.4 report ----------------------------


def test_synthesise_returns_draft_shaped_dict() -> None:
    # Arrange.
    client = _FakeBrahmastra(_responses())

    # Act.
    draft = synthesise("Revenue-cycle management", _ITEMS, client=client, settings=_settings(), prompts=_prompts())

    # Assert: the post text is assembled from hook + body + takeaway.
    assert "Denials fell 18%" in draft["post_text"]
    assert "walk-through" in draft["post_text"]
    assert "Pilot one automation" in draft["post_text"]
    assert draft["hashtags"] == ["#RevenueCycle", "#HealthcareAI", "#ClinicalOps"]
    assert draft["source_item_ids"] == ["item-1", "item-2"]
    assert draft["confidence"] == 0.88


def test_quality_report_shape_matches_brd_14_4() -> None:
    # Arrange.
    client = _FakeBrahmastra(_responses())

    # Act.
    draft = synthesise("Data & BI in healthcare", _ITEMS, client=client, settings=_settings(), prompts=_prompts())

    # Assert: exactly the eight §14.4 keys, no more, no fewer.
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
    assert draft["quality_report"]["grounding_pct"] == 100.0


# --- 2. Grounding gate pass/fail -------------------------------------------


def test_grounding_gate_passes_at_or_above_floor() -> None:
    # Arrange / Act / Assert: 100 >= 100 is eligible.
    assert passes_grounding_gate(100.0, _settings(grounding_min_pct=100)) is True


def test_grounding_gate_fails_below_floor() -> None:
    # Arrange / Act / Assert: 80 < 100 is not eligible.
    assert passes_grounding_gate(80.0, _settings(grounding_min_pct=100)) is False


def test_pipeline_marks_auto_eligible_false_when_grounding_below_floor() -> None:
    # Arrange: verifier reports only 80% grounding.
    client = _FakeBrahmastra(_responses(verify=_verify_json(grounding_pct=80.0)))

    # Act.
    draft = synthesise("AI in clinical operations", _ITEMS, client=client, settings=_settings(), prompts=_prompts())

    # Assert: below the floor → routed to manual approval, not auto-eligible.
    assert draft["auto_eligible"] is False
    assert draft["quality_report"]["grounding_pct"] == 80.0


def test_pipeline_marks_auto_eligible_true_at_full_grounding() -> None:
    # Arrange.
    client = _FakeBrahmastra(_responses())

    # Act.
    draft = synthesise("Frontier AI", _ITEMS, client=client, settings=_settings(), prompts=_prompts())

    # Assert.
    assert draft["auto_eligible"] is True


# --- 3. Banned-phrase tone flag --------------------------------------------


def test_banned_phrase_in_final_post_raises_tone_flag() -> None:
    # Arrange: the verified post body contains a banned hype phrase.
    hyped = _verify_json(body="Honestly this is a game changer for every hospital.")
    client = _FakeBrahmastra(_responses(verify=hyped))

    # Act.
    draft = synthesise("Leadership reflection", _ITEMS, client=client, settings=_settings(), prompts=_prompts())

    # Assert: the banned phrase surfaces as a tone flag.
    assert "game changer" in draft["quality_report"]["tone_flags"]


def test_find_banned_phrases_is_case_insensitive_and_deduped() -> None:
    # Arrange.
    text = "This REVOLUTIONARY, revolutionary tool will disrupt everything."
    banned = ["revolutionary", "disrupt", "10x"]

    # Act.
    flags = find_banned_phrases(text, banned)

    # Assert: case-insensitive match, each phrase reported once, order preserved.
    assert flags == ["revolutionary", "disrupt"]


# --- 4. Schema drift fails loudly ------------------------------------------


def test_generate_missing_required_field_raises_brahmastra_error() -> None:
    # Arrange: drop the required 'takeaway' from the generate output.
    broken = _generate_json()
    del broken["takeaway"]
    client = _FakeBrahmastra(_responses(generate=broken))

    # Act / Assert: schema drift is a hard, loud failure.
    with pytest.raises(BrahmastraError):
        synthesise("Pharma tech", _ITEMS, client=client, settings=_settings(), prompts=_prompts())


def test_verify_extra_field_raises_brahmastra_error() -> None:
    # Arrange: an un-modelled extra key must be rejected (extra='forbid').
    broken = _verify_json()
    broken["surprise"] = "not in the contract"
    client = _FakeBrahmastra(_responses(verify=broken))

    # Act / Assert.
    with pytest.raises(BrahmastraError):
        synthesise("Patient experience", _ITEMS, client=client, settings=_settings(), prompts=_prompts())


# --- 5. model_trace recorded -----------------------------------------------


def test_model_trace_records_every_pass_lane() -> None:
    # Arrange.
    client = _FakeBrahmastra(_responses())

    # Act.
    draft = synthesise("Data & BI", _ITEMS, client=client, settings=_settings(), prompts=_prompts())

    # Assert: all four passes traced, each on its configured lane, none degraded.
    trace = draft["model_trace"]
    assert trace["generate"] == {"lane": "gemini", "degraded": False}
    assert trace["critique"] == {"lane": "codex", "degraded": False}
    assert trace["verify"] == {"lane": "claude", "degraded": False}
    assert trace["image"] == {"lane": "codex", "degraded": False}


# --- 6. Degraded-lane fallback ---------------------------------------------


def test_unavailable_primary_lane_degrades_to_working_lane() -> None:
    # Arrange: the generate lane (gemini) is down; codex/claude are up.
    client = _FakeBrahmastra(_responses(), unavailable=frozenset({"gemini"}))

    # Act.
    draft = synthesise("Frontier AI", _ITEMS, client=client, settings=_settings(), prompts=_prompts())

    # Assert: generate degraded off gemini onto the next working configured lane,
    # and the degradation is recorded (BRD §13.0). The remaining passes stay put.
    generate_trace = draft["model_trace"]["generate"]
    assert generate_trace["degraded"] is True
    assert generate_trace["lane"] != "gemini"
    assert generate_trace["lane"] in {"codex", "claude"}
    assert draft["model_trace"]["verify"] == {"lane": "claude", "degraded": False}


def test_all_lanes_down_fails_loudly() -> None:
    # Arrange: every configured lane is unavailable.
    client = _FakeBrahmastra(
        _responses(), unavailable=frozenset({"gemini", "codex", "claude"})
    )

    # Act / Assert: a total outage must fail loudly, never fabricate a post.
    with pytest.raises(BrahmastraError):
        synthesise("Any focus", _ITEMS, client=client, settings=_settings(), prompts=_prompts())


# --- Pure-function coverage: quality helpers -------------------------------


def test_assemble_post_text_joins_parts_and_appends_hashtags() -> None:
    # Arrange / Act.
    text = assemble_post_text("Hook.", "Body para.", "Do this.", ["#A", "#B"])

    # Assert: paragraph-separated parts with a trailing hashtag line.
    assert text == "Hook.\n\nBody para.\n\nDo this.\n\n#A #B"


def test_build_quality_report_flags_out_of_range_hashtags_and_length() -> None:
    # Arrange: a too-short post with too few hashtags, over a real voice profile.
    voice = _prompts().voice
    verify = VerifyOut.model_validate(_verify_json())
    critique = CritiqueOut.model_validate(_critique_json())
    short_text = "tiny"

    # Act.
    report = build_quality_report(short_text, ["#Only"], verify, critique, voice)

    # Assert: both structural breaches show up as compliance flags.
    flags = report["compliance_flags"]
    assert any(flag.startswith("length_below_min") for flag in flags)
    assert any(flag.startswith("hashtag_count_out_of_range") for flag in flags)


# --- BUG 1: server-side grounding gate (BRD §13.5/NFR-01, fail-closed §22.9) -
# The gate must be computed SERVER-SIDE from the verifier's own counts, never
# trusted from the model's self-reported ``grounding_pct``. A model that claims
# 100% grounding while still returning unsupported claims (or a grounded claim
# that failed the verbatim check) must NEVER auto-publish.


def test_reported_100_pct_with_unsupported_claims_is_not_auto_eligible() -> None:
    # Arrange: the verifier LIES — grounding_pct=100 yet an unsupported claim
    # remains. Trusting the self-reported field would auto-publish an ungrounded
    # post; the server-side gate must catch the inconsistency.
    lying = _verify_json(grounding_pct=100.0)
    lying["unsupported"] = [
        {"text": "revenue tripled", "reason": "no source", "action": "flagged"}
    ]
    client = _FakeBrahmastra(_responses(verify=lying))

    # Act.
    draft = synthesise(
        "Frontier AI", _ITEMS, client=client, settings=_settings(), prompts=_prompts()
    )

    # Assert: unsupported claims present → not auto-eligible.
    assert draft["auto_eligible"] is False


def test_reported_100_pct_with_non_verbatim_claim_is_not_auto_eligible() -> None:
    # Arrange: grounding_pct=100 but a "grounded" claim failed the verbatim check.
    lying = _verify_json(grounding_pct=100.0)
    lying["grounded"][0]["verbatim_ok"] = False
    client = _FakeBrahmastra(_responses(verify=lying))

    # Act.
    draft = synthesise(
        "Frontier AI", _ITEMS, client=client, settings=_settings(), prompts=_prompts()
    )

    # Assert: not every grounded claim is verbatim-ok → not auto-eligible.
    assert draft["auto_eligible"] is False


# --- BUG 2: card numbers must trace to a grounded claim (RAFT §13.6) ---------


def test_card_datapoint_not_in_grounded_set_raises() -> None:
    # Arrange: an informative card cites a fabricated source_item_id that is NOT
    # among the verifier's grounded claim ids — a rendered number with no lineage.
    fabricated_card = {
        "image_type": "informative-card",
        "rationale": "One dominant stat worth a card.",
        "card_spec": {
            "title": "Denials down",
            "datapoints": [
                {"label": "Denials", "value": "-18%", "source_item_id": "item-999"}
            ],
        },
        "illustration_prompt": None,
    }
    client = _FakeBrahmastra(_responses(image=fabricated_card))

    # Act / Assert: a card number with no grounded lineage must fail loudly.
    with pytest.raises(BrahmastraError):
        synthesise(
            "Frontier AI",
            _ITEMS,
            client=client,
            settings=_settings(),
            prompts=_prompts(),
        )


# --- BUG 3: grounding_pct / confidence are bounded, finite floats -----------


def test_grounding_pct_above_100_rejected_by_schema() -> None:
    # Arrange / Act / Assert: 101% grounding is impossible and must fail loudly.
    payload = _verify_json()
    payload["grounding_pct"] = 101.0
    with pytest.raises(ValidationError):
        VerifyOut.model_validate(payload)


def test_confidence_infinity_rejected_by_schema() -> None:
    # Arrange / Act / Assert: a non-finite confidence must fail loudly.
    payload = _verify_json()
    payload["confidence"] = math.inf
    with pytest.raises(ValidationError):
        VerifyOut.model_validate(payload)
