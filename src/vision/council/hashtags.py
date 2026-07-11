"""Generate hashtags from a finished post when the composer dropped them.

WHY this module exists: the compose prompt asks for 3-5 hashtags, but the
headless composing voice frequently returns only the post prose and drops the
whole structured output (format, council block, AND hashtags). The post still
publishes, but with NO hashtags - a real reach hit on LinkedIn. This step is the
same reliable, decoupled pattern as :mod:`vision.council.diagram`: read the
ALREADY-WRITTEN post and ask a voice, with a single-purpose prompt, for a short
line of hashtags. Because it is one simple task over the final text, it reliably
produces relevant tags.

FAIL-SOFT: hashtags are a best-effort enhancement. An empty post, a voice
hiccup, an unusable reply, or every candidate being filtered all return ``[]`` -
the post then ships exactly as it does today (no hashtags), never blocked.

DE-NAMING: a candidate that embeds an AI vendor/brand ('#Gemini', '#GPT') is
dropped via the same :func:`~vision.council.compose.find_forbidden_name` gate the
published text uses, so a hashtag can never leak the machinery.
"""

from __future__ import annotations

import logging
import re

from vision.config import Settings, get_settings
from vision.council.compose import find_forbidden_name
from vision.council.voices import CLAUDE, Voices

logger = logging.getLogger(__name__)

# A '#hashtag' token in the voice's reply (letters/digits/underscore after '#').
_HASHTAG_RE = re.compile(r"#\w+")

# Cap the number of tags: LinkedIn best practice is a handful, and the compose
# prompt itself asks for 3-5. More than this reads as spam.
_MAX_HASHTAGS = 5

# Generic, low-signal tags that add no reach and read as filler. Dropped even if a
# voice emits them (the prompt discourages them, this is belt-and-braces). Compared
# case-insensitively WITHOUT the leading '#'.
_GENERIC_DENYLIST: frozenset[str] = frozenset(
    {
        "motivation", "motivational", "inspiration", "inspirational", "success",
        "hustle", "grind", "love", "life", "goals", "mindset", "growth",
        "networking", "follow", "like", "share", "viral", "trending",
    }
)


def _build_hashtag_prompt(post_text: str) -> str:
    """Assemble the single-purpose 'post -> 3-5 hashtags' prompt."""
    return (
        "Give 3 to 5 LinkedIn hashtags for the post below, most relevant first.\n\n"
        "RULES:\n"
        "- Each hashtag is ONE token: '#Word' or '#CamelCase' for a multi-word tag "
        "(no spaces, no punctuation inside).\n"
        "- SPECIFIC to this post's actual subject - name the real topic, tools, or "
        "field. NO generic filler (#motivation, #success, #growth, #mindset).\n"
        "- NO AI vendor/brand names (#Gemini, #Codex, #Claude, #GPT, #OpenAI).\n"
        "- Reply with ONLY the hashtags on a single line, space-separated. No other "
        "words, no explanation.\n\n"
        f"POST:\n{post_text}\n\n"
        "Hashtags:"
    )


def _parse_hashtags(raw: str) -> list[str]:
    """Extract clean, de-named, de-duped hashtags from a voice reply (capped).

    Order-preserving dedupe (case-insensitive), drops generic filler and any tag
    that trips the de-naming gate, and caps at :data:`_MAX_HASHTAGS`. Returns
    ``[]`` when nothing usable survives.
    """
    seen: set[str] = set()
    out: list[str] = []
    for tag in _HASHTAG_RE.findall(raw or ""):
        word = tag[1:]  # strip the leading '#'
        key = word.lower()
        if key in seen or key in _GENERIC_DENYLIST:
            continue
        # A hashtag that embeds a vendor/brand name must never publish.
        if find_forbidden_name(word) is not None:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= _MAX_HASHTAGS:
            break
    return out


class HashtagWriter:
    """Produces hashtags from a finished post (or ``[]``), fail-soft.

    The :class:`Voices` transport is injected so unit tests MOCK the voice and no
    real model runs. Single public method :meth:`hashtags_for`.
    """

    def __init__(
        self,
        voices: Voices | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._voices = voices or Voices(self._settings)

    def hashtags_for(self, post_text: str) -> list[str]:
        """Return 3-5 relevant hashtags for ``post_text``, or ``[]``.

        ``[]`` whenever the post is empty, the voice fails, or no usable tag
        survives filtering - the caller then ships the post without hashtags,
        exactly as before. Never raises for a content reason; an unexpected
        transport error is logged and swallowed (fail-soft).
        """
        body = (post_text or "").strip()
        if not body:
            return []
        try:
            raw = self._voices.ask(CLAUDE, _build_hashtag_prompt(body))
        except Exception:  # noqa: BLE001 - hashtags are best-effort, never a blocker
            logger.warning("Hashtag-writer voice failed; post ships without hashtags.")
            return []
        return _parse_hashtags(raw)
