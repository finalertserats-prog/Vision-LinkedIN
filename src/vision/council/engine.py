"""The council orchestrator: pick_topic -> deliberate -> compose (BRD §5).

WHY this module exists: it is the single public entry point for the council
content mode. :func:`run_council` wires the four stages together — choose a topic
(owner queue first, else propose), run the two-round deliberation, compose the
de-named post — and returns a **Draft-shaped dict** the rest of VISION already
knows how to persist/route (mirrors the ``drafts`` table fields:
``post_text``/``hashtags``/``model_trace`` etc., §11.4).

Fail-closed policy (§22.9): a single dead voice DEGRADES the council (the
deliberation still runs on the survivors); but if the deliberation cannot produce
at least two round-1 takes, or the topic engine can find no topic, or compose
returns nothing, :func:`run_council` raises — VISION must never publish a hollow
"council" post that no council actually produced.
"""

from __future__ import annotations

import logging
from typing import Any

from vision.config import Settings, get_settings
from vision.council.compose import Composer
from vision.council.deliberate import Deliberation, Deliberator
from vision.council.formats import RecentFormatStore
from vision.council.topics import TopicEngine
from vision.council.voices import VOICE_ORDER, Voices

logger = logging.getLogger(__name__)

# Marks this Draft as coming from the council path so downstream routing (email,
# image lane, publish) can branch on content mode without sniffing the shape.
_CONTENT_MODE = "council"


def _build_transcript(delib: Deliberation) -> dict[str, dict[str, str]]:
    """Return the raw two-round debate as a plain, JSON-serialisable dict.

    Shape: ``{voice: {"round1": ..., "round2": ...}}`` for every voice — the
    un-edited debate, stored on the Draft for provenance/audit but NEVER published
    (compose already produced the de-named public text).
    """
    return {
        voice: {"round1": delib.round1.get(voice, ""), "round2": delib.round2.get(voice, "")}
        for voice in VOICE_ORDER
    }


def run_council(
    topic: str | None = None,
    *,
    settings: Settings | None = None,
    voices: Voices | None = None,
) -> dict[str, Any]:
    """Run the full council and return a Draft-shaped dict.

    Orchestration: if ``topic`` is not supplied, the :class:`TopicEngine` picks one
    (owner queue FIFO first, else propose-and-pick a non-recent topic). The
    :class:`Deliberator` runs the two honest rounds; the :class:`Composer` writes
    the de-named post. The pieces are assembled into a dict whose keys line up with
    the ``drafts`` table plus the council-specific extras.

    Args:
        topic: An explicit topic to debate. ``None`` => let the topic engine choose
            (respecting the owner queue + exclusion guardrail + recent-topic variety).
        settings: Config override (defaults to the cached singleton).
        voices: Voice-transport override — the seam unit tests MOCK so no real
            model is called. A single shared instance is passed to every stage so
            they hit the same (mocked) transport.

    Returns:
        A Draft-shaped dict::

            {
              "content_mode": "council",
              "topic": str,
              "format": str,
              "situation": str,
              "post_text": str,
              "hashtags": list[str],
              "council_block": str,
              "transcript": {voice: {"round1", "round2"}},  # raw, never published
              "model_trace": {...},                          # per-stage provenance
            }

    Raises:
        RuntimeError: fail-closed when no topic can be obtained, fewer than two
            voices deliberate, or compose yields nothing.
    """
    settings = settings or get_settings()
    # One shared transport across every stage so a test's mock is honoured
    # end-to-end and real runs reuse the same resolved council-dir/timeout.
    voices = voices or Voices(settings)

    # A single recent-format store threaded through topic-picking (avoid recent
    # repeats is a compose concern; topic variety is separate) and compose.
    recent_store = RecentFormatStore.from_settings(settings)

    topic_engine = TopicEngine(voices=voices, settings=settings)
    deliberator = Deliberator(voices=voices, settings=settings)
    composer = Composer(voices=voices, recent_store=recent_store, settings=settings)

    # 1. TOPIC. An explicit topic wins; otherwise the engine chooses (fail-closed
    #    inside pick_topic if it can find nothing).
    chosen_topic = topic if (topic and topic.strip()) else topic_engine.pick_topic()
    logger.info("Council running on topic: %s", chosen_topic)

    # 2. DELIBERATE. Fail-soft per voice, fail-closed on <2 takes (raises).
    deliberation = deliberator.deliberate(chosen_topic)
    live = deliberation.live_voices()
    if len(live) < len(VOICE_ORDER):
        # Degraded-but-valid: log which voices are missing for observability.
        logger.warning(
            "Council degraded: %d of %d voices produced a take.",
            len(live),
            len(VOICE_ORDER),
        )

    # 3. COMPOSE. Fail-closed on empty output (raises).
    composed = composer.compose(deliberation)

    # 4. ASSEMBLE the Draft-shaped dict. ``model_trace`` records per-stage
    #    provenance (which voices were live, the chosen format/situation) so the
    #    Draft carries an auditable trail (§13.0) without leaking the raw debate
    #    into the published text.
    model_trace: dict[str, Any] = {
        "content_mode": _CONTENT_MODE,
        "live_voices": live,
        "format": composed.format,
        "situation": composed.situation,
    }
    return {
        "content_mode": _CONTENT_MODE,
        "topic": chosen_topic,
        "format": composed.format,
        "situation": composed.situation,
        "post_text": composed.post_text,
        "hashtags": composed.hashtags,
        "council_block": composed.council_block,
        "transcript": _build_transcript(deliberation),
        "model_trace": model_trace,
    }
