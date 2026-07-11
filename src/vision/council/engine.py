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
from vision.council.diagram import DiagramWriter
from vision.council.hashtags import HashtagWriter
from vision.council.formats import RecentFormatStore
from vision.council.problems import ProblemQueue
from vision.council.topics import TopicEngine
from vision.council.visual import attach_council_image
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


def _frame_problem(problem: str) -> str:
    """Frame a real problem blob as the deliberation subject (grounded, no invention)."""
    return (
        "A real problem the author actually faced and worked through. Treat it as "
        "ground truth and do NOT invent facts:\n\n"
        f"{problem}\n\n"
        "What is the sharpest, most useful angle and the real, earned lesson here - "
        "the thing worth sharing so someone facing something similar gets value?"
    )


def _problem_label(problem: str) -> str:
    """A short human-glanceable label (the blob's first non-empty line, bounded)."""
    first = next((ln.strip() for ln in problem.splitlines() if ln.strip()), "a problem we solved")
    return first[:120]


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
              "image_type": str,        # 'none'|'quote_card'|'concept_illustration'|'contrast_card'|'diagram'
              "image_path": str | None, # written PNG (None when text-only)
              "image_source": str | None,  # 'deterministic' | '<model-id>'
              "image_prompt": str | None,  # concept prompt (None for a card)
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

    # 1. SOURCE (seeds-first, ADD-ON — the auto-topic path is unchanged when the
    #    problem inbox is empty). A queued real problem (the owner's freeform
    #    brain-dump) becomes the day's GROUNDED 'overcome story' before any auto
    #    topic. An explicit topic still wins over both.
    problem = None
    if not (topic and topic.strip()):
        problem = ProblemQueue(settings).consume_head()

    if problem is not None:
        chosen_topic = _problem_label(problem)
        logger.info("Council running on a seeded PROBLEM: %s", chosen_topic)
        deliberation = deliberator.deliberate(_frame_problem(problem))
    else:
        chosen_topic = topic if (topic and topic.strip()) else topic_engine.pick_topic()
        logger.info("Council running on topic: %s", chosen_topic)
        deliberation = deliberator.deliberate(chosen_topic)

    # 2. Fail-soft per voice, fail-closed on <2 takes (raised inside deliberate()).
    live = deliberation.live_voices()
    if len(live) < len(VOICE_ORDER):
        # Degraded-but-valid: log which voices are missing for observability.
        logger.warning(
            "Council degraded: %d of %d voices produced a take.",
            len(live),
            len(VOICE_ORDER),
        )

    # 3. COMPOSE. The problem lane writes the grounded overcome-story; otherwise the
    #    standard post. Both share every fail-closed gate. Fail-closed on empty.
    composed = (
        composer.compose_problem(deliberation, problem)
        if problem is not None
        else composer.compose(deliberation)
    )

    # 3.5 DIAGRAM (decoupled, in-sync). The inline DIAGRAM: contract is unreliable -
    #     the composing voice often returns just the post prose and drops the whole
    #     structured output. So when the diagram lane is on and compose produced no
    #     diagram, generate one FROM the finished post (a single-purpose prompt over
    #     the final text). Fail-soft: any miss leaves the post on its fallback image.
    if settings.council_diagram_enabled and composed.diagram is None:
        composed.diagram = DiagramWriter(voices=voices, settings=settings).diagram_for(
            composed.post_text
        )
        if composed.diagram is not None:
            logger.info("Attached an in-sync diagram generated from the post.")

    # 3.6 HASHTAGS (fallback). The compose prompt asks for 3-5 hashtags but the
    #     composing voice often drops them. When the post carries none, generate
    #     them FROM the finished post and APPEND to the body so they publish (the
    #     publisher renders post_text verbatim; the hashtags field is metadata).
    #     Fail-soft: a miss just ships the post without hashtags, as before.
    if settings.council_hashtags_enabled and not composed.hashtags:
        tags = HashtagWriter(voices=voices, settings=settings).hashtags_for(
            composed.post_text
        )
        if tags:
            composed.hashtags = tags
            composed.post_text = composed.post_text.rstrip() + "\n\n" + " ".join(tags)
            logger.info("Appended %d generated hashtags to the post.", len(tags))

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
    draft: dict[str, Any] = {
        "content_mode": _CONTENT_MODE,
        "topic": chosen_topic,
        "format": composed.format,
        "situation": composed.situation,
        "post_text": composed.post_text,
        "hashtags": composed.hashtags,
        "council_block": composed.council_block,
        "contrast": composed.contrast,  # optional; drives the contrast-card image lane
        "diagram": composed.diagram,  # optional; drives the deterministic diagram image lane
        "transcript": _build_transcript(deliberation),
        "model_trace": model_trace,
    }

    # 5. IMAGE LANE (BRD §13.6). A best-effort visual decision runs AFTER compose:
    #    it may attach a DETERMINISTIC quote card (a strong one-line punchline) or
    #    a TEXT-FREE agy concept illustration, respecting the weekly cap +
    #    rotation (config-driven, not every post). It DEGRADES GRACEFULLY — any
    #    failure leaves the draft text-only and NEVER blocks the post — and sets
    #    the draft's image_type/image_path/image_source/image_prompt in place so
    #    the mailer + publisher pick the image up.
    attach_council_image(draft, settings=settings)

    return draft
