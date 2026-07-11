"""Generate an IN-SYNC diagram from a finished post (owner req 2026-07-11).

WHY this module exists: the inline ``DIAGRAM:`` section in the compose contract
proved unreliable - the composing voice frequently returns just the post prose
and drops the whole structured contract (FORMAT/COUNCIL/DIAGRAM), so no diagram
ever reaches the renderer. This step is DECOUPLED: it reads the ALREADY-WRITTEN
post and asks a voice for one small mermaid diagram that captures the post's own
decision flow / architecture, IF the post has such a structure. Because it is a
single-purpose prompt over the final text, it reliably produces a diagram that is
genuinely in sync with what the reader is about to read.

The diagram is rendered DETERMINISTICALLY downstream by the mermaid CLI (precision
rule §13.6/D10), and this step reuses the exact validation + de-naming gate the
inline path used, so a leaked AI/model name can never reach the published image.

FAIL-SOFT: a diagram is a best-effort enhancement. A voice hiccup, a 'NONE'
reply, non-mermaid junk, or a de-naming leak all return ``None`` - the post then
falls back to the amplifying concept illustration and is NEVER blocked.
"""

from __future__ import annotations

import logging

from vision.config import Settings, get_settings
from vision.council.compose import (
    DiagramSpec,
    _parse_diagram,
    _strip_code_fence,
    find_forbidden_name,
)
from vision.council.voices import CLAUDE, Voices

logger = logging.getLogger(__name__)

# The composing voice sometimes answers 'NONE' (no diagram warranted). Compared
# case-insensitively against the first token so 'none.', 'NONE' both count.
_NO_DIAGRAM_SENTINEL = "none"


def _build_diagram_prompt(post_text: str) -> str:
    """Assemble the single-purpose 'post -> mermaid diagram or NONE' prompt."""
    return (
        "You turn a finished LinkedIn post into ONE small diagram, but only when a "
        "picture genuinely helps the reader.\n\n"
        "Read the post below. If it describes a PROCESS, DECISION FLOW, "
        "ARCHITECTURE, or a set of STEPS or ROUTES, produce a single compact "
        "mermaid diagram that captures THAT structure so a reader grasps it at a "
        "glance. If the post is a reflection or opinion with no such structure, "
        "reply with exactly the word NONE.\n\n"
        "RULES:\n"
        "- Draw ONLY what the post itself says - do not invent steps.\n"
        "- Max ~7 nodes. Labels short, plain, taken from the post's own words.\n"
        "- NO AI/model/vendor names (no 'Gemini', 'Codex', 'Claude', 'GPT').\n"
        "- NO front-matter or config block, NO '---' lines, NO code fences.\n"
        "- Start DIRECTLY with 'flowchart TD' (or 'flowchart LR' / "
        "'sequenceDiagram').\n\n"
        f"POST:\n{post_text}\n\n"
        "Reply with ONLY the raw mermaid source, or the single word NONE."
    )


def _parse_diagram_reply(raw: str) -> DiagramSpec | None:
    """Turn a diagram-writer reply into a validated, de-named :class:`DiagramSpec`.

    Returns ``None`` for an empty reply, an explicit 'NONE', non-mermaid text, or
    a source that leaks a forbidden AI/model name (fail-soft - the post keeps its
    fallback image). Reuses :func:`_parse_diagram` so validation matches the
    inline path exactly.
    """
    src = _strip_code_fence((raw or "").strip())
    if not src:
        return None
    first = next((ln.strip() for ln in src.splitlines() if ln.strip()), "")
    if first.lower().rstrip(".!").startswith(_NO_DIAGRAM_SENTINEL):
        return None
    spec = _parse_diagram(src.splitlines())
    if spec is None:
        return None
    leak = find_forbidden_name(spec.mermaid)
    if leak is not None:
        logger.warning(
            "Post-based diagram names a forbidden AI/model; dropping it.",
            extra={"forbidden_match": leak},
        )
        return None
    return spec


class DiagramWriter:
    """Produces an in-sync mermaid diagram from a finished post (or ``None``).

    The :class:`Voices` transport is injected so unit tests MOCK the voice and no
    real model runs. Single public method :meth:`diagram_for`; everything is
    fail-soft so a diagram problem never blocks the post.
    """

    def __init__(
        self,
        voices: Voices | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._voices = voices or Voices(self._settings)

    def diagram_for(self, post_text: str) -> DiagramSpec | None:
        """Return a diagram capturing ``post_text``'s flow, or ``None``.

        ``None`` whenever the post has no diagrammable structure (voice replies
        NONE), the reply is unusable, or a de-naming leak is caught - the caller
        then keeps the fallback image. Never raises for a content reason; an
        unexpected transport error is logged and swallowed (fail-soft).
        """
        body = (post_text or "").strip()
        if not body:
            return None
        try:
            raw = self._voices.ask(CLAUDE, _build_diagram_prompt(body))
        except Exception:  # noqa: BLE001 - a diagram is best-effort, never a blocker
            logger.warning("Diagram-writer voice failed; post keeps its fallback image.")
            return None
        return _parse_diagram_reply(raw)
