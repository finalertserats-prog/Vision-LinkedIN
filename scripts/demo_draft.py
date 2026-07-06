"""Human-readable Phase-1 demo: print a mocked draft + save a rendered card.

WHY this script exists: it produces a *tangible* sample of what VISION's daily
pipeline emits — a finished LinkedIn draft plus its deterministic informative
card — WITHOUT touching any model, network, or database (BRD §22 CLI/no-key
posture). The Brahmastra model calls are mocked with canned JSON grounded in two
in-memory fixture items, so the output is fully reproducible.

Run:
    .venv/Scripts/python scripts/demo_draft.py

Output:
    * a human-readable draft printed to stdout, and
    * a rendered stat card written to ``prep/sample_card.png``.

Note on ``print``: the pipeline's operational logs go through the ``logging``
module (BRD §22). The final draft block is written with ``print`` deliberately —
it is this demo's *user-facing deliverable*, not debug output.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from vision.config import Settings, SignatureMode
from vision.logging_setup import configure_logging, get_logger
from vision.synthesise.pipeline import synthesise
from vision.synthesise.prompts import PromptLibrary
from vision.visuals.card_renderer import render_stat_card
from vision.visuals.decide import CardSpec, Datapoint, ImageType, image_decision

logger: logging.Logger = get_logger("vision.scripts.demo_draft")

# Where the rendered sample card is written (repo ``prep/`` staging dir).
_OUTPUT_PATH: Path = Path(__file__).resolve().parents[1] / "prep" / "sample_card.png"

# Two fixed fixture items (one per lane) with concrete, groundable figures. Fixed
# ids let the mocked model cite genuine-looking provenance the gate can verify.
_ITEMS: list[dict[str, Any]] = [
    {
        "source_item_id": "demo-hc-1",
        "title": "Hospital cuts claim denials with revenue-cycle automation",
        "url": "https://example.test/hc/denials-automation",
        "source": "STAT News",
        "published_at": "2026-07-06T06:00:00+00:00",
        "summary": "A 200-bed hospital reduced claim denials by 18% after deploying RCM automation.",
    },
    {
        "source_item_id": "demo-ai-1",
        "title": "Open model matches prior systems on clinical-note summarisation",
        "url": "https://example.test/ai/clinical-notes-model",
        "source": "Import AI",
        "published_at": "2026-07-06T02:00:00+00:00",
        "summary": "An open model cleared 223 evaluation cases on note summarisation this quarter.",
    },
]

# The exact grounded figures each item is allowed to surface on a card (§13.6).
_FIGURES: dict[str, tuple[str, str]] = {
    "demo-hc-1": ("Claim-denial reduction", "18%"),
    "demo-ai-1": ("Eval cases cleared", "223"),
}


def _settings() -> Settings:
    """Pinned settings so the demo renders identically on any machine."""
    return Settings(
        MODEL_GENERATE="gemini",
        MODEL_CRITIQUE="codex",
        MODEL_VERIFY="claude",
        GROUNDING_MIN_PCT=100,
        CARD_BRAND_PALETTE="navy=#0B1F3A;gold=#C9A24B",
        # A discreet BRAHMASTRA wordmark; no logo file needed (§15.6, D9).
        POST_SIGNATURE_MODE=SignatureMode.CARD_WATERMARK,
    )


class _MockBrahmastra:
    """Canned-JSON stand-in for ``BrahmastraClient`` — no model, no network.

    Routes the image pass (which shares the ``critique`` method) by the RAFT
    "IMAGE DECISION" heading in the rendered prompt, exactly like the real chain.
    """

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses

    def generate(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self._responses["generate"])

    def critique(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        key = "image" if "IMAGE DECISION" in prompt else "critique"
        return copy.deepcopy(self._responses[key])

    def verify(self, prompt: str, lane: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self._responses["verify"])


def _canned_responses() -> dict[str, dict[str, Any]]:
    """Build grounded generate/critique/verify/image JSON over ``_ITEMS``."""
    claims = [
        {
            "text": f"{_FIGURES[item['source_item_id']][0].lower()} was "
            f"{_FIGURES[item['source_item_id']][1]}",
            "source_item_id": item["source_item_id"],
        }
        for item in _ITEMS
    ]
    body = (
        "Two operational signals stood out across the healthcare and AI lanes today. "
        "A 200-bed hospital cut claim denials by 18% after moving revenue-cycle "
        "checks to automation. In parallel, an open clinical-note model cleared 223 "
        "evaluation cases — matching prior systems without the licence cost. "
        "The common thread is unglamorous: put automation where the friction and the "
        "denials actually are, then measure the delta. Neither result is hype; both "
        "are the kind of concrete win a healthcare leader can pressure-test this week."
    )
    hashtags = ["#HealthcareAI", "#RevenueCycle", "#DigitalHealth"]
    generate = {
        "hook": "Claim denials fell 18% at one hospital after revenue-cycle automation.",
        "body": body,
        "takeaway": "Pilot one automation on your highest-denial payer this month and measure the delta.",
        "hashtags": hashtags,
        "claims": claims,
    }
    critique = {
        "revised": copy.deepcopy(generate),
        "change_log": ["Sharpened the hook", "Cut one hedge", "Tightened the takeaway"],
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
    datapoints = [
        {
            "label": _FIGURES[item["source_item_id"]][0],
            "value": _FIGURES[item["source_item_id"]][1],
            "source_item_id": item["source_item_id"],
        }
        for item in _ITEMS
    ]
    image = {
        "image_type": "informative-card",
        "rationale": "Post centres on two concrete, comparable figures — render deterministically.",
        "card_spec": {"title": "Today's grounded signals", "datapoints": datapoints},
        "illustration_prompt": None,
    }
    return {"generate": generate, "critique": critique, "verify": verify, "image": image}


def _render_human_readable(draft: dict[str, Any]) -> str:
    """Format the draft dict into a readable block for stdout."""
    report = draft["quality_report"]
    lines = [
        "=" * 72,
        "PROJECT VISION — SAMPLE DRAFT (mocked models, no network)",
        "=" * 72,
        f"Focus          : {draft['lane_focus']}",
        f"Auto-eligible  : {draft['auto_eligible']}  (grounding {report['grounding_pct']}%)",
        f"Confidence     : {draft['confidence']}",
        f"Image type     : {draft['image_type']}",
        f"Source items   : {', '.join(draft['source_item_ids'])}",
        "-" * 72,
        "POST TEXT:",
        "",
        draft["post_text"],
        "",
        "-" * 72,
        "QUALITY REPORT (BRD §14.4):",
        f"  char_count        : {report['char_count']}",
        f"  has_hook          : {report['has_hook']}",
        f"  grounding_pct     : {report['grounding_pct']}",
        f"  unsupported_claims: {report['unsupported_claims']}",
        f"  tone_flags        : {report['tone_flags']}",
        f"  compliance_flags  : {report['compliance_flags']}",
        f"  hashtags          : {report['hashtags']}",
        f"  confidence        : {report['confidence']}",
        "-" * 72,
        "MODEL TRACE:",
    ]
    for pass_name, info in draft["model_trace"].items():
        lines.append(f"  {pass_name:<9}: lane={info['lane']:<7} degraded={info['degraded']}")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> int:
    """Build a mocked draft, print it, and save its rendered card. Returns 0 on ok."""
    configure_logging()
    settings = _settings()

    client = _MockBrahmastra(_canned_responses())
    draft = synthesise(
        "Revenue-cycle management",
        _ITEMS,
        client=client,  # type: ignore[arg-type]  # structural stand-in for BrahmastraClient
        settings=settings,
        prompts=PromptLibrary.default(),
    )

    # The demo's deliverable: a human-readable draft (see module docstring on print).
    print(_render_human_readable(draft))

    # Resolve + render the informative card through the precision-first gate.
    decision = draft["image_decision"]
    claim_texts = [f"{p['label']} {p['value']}" for p in decision["card_spec"]["datapoints"]]
    if image_decision(draft["post_text"], claim_texts, decision) is not ImageType.INFORMATIVE_CARD:
        logger.warning("image gate did not resolve to a card; skipping render")
        return 0

    card_spec = CardSpec(
        title=str(decision["card_spec"]["title"]),
        datapoints=[
            Datapoint(
                label=str(p["label"]), value=str(p["value"]), source_item_id=str(p["source_item_id"])
            )
            for p in decision["card_spec"]["datapoints"]
        ],
    )
    png = render_stat_card(card_spec, settings=settings)

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_bytes(png)
    logger.info("sample card written", extra={"path": str(_OUTPUT_PATH), "bytes": len(png)})
    print(f"\nRendered card saved to: {_OUTPUT_PATH}  ({len(png):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
