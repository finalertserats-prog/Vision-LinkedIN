"""Brahmastra Council engine (BRD §5 council-content-vision).

A genuine three-AI deliberation → de-named LinkedIn post, promoted VERBATIM from
the proven, owner-approved prototype (``scripts/council.py``). Three headless
voices (Gemini / Codex / Claude) run two honest rounds; an editorial pass composes
a de-named post with a honesty gate, format-variety, tonal range, an unnamed
'Council' block, and the sole 'Powered by Brahmastra' attribution.

Public surface:
  * :func:`~vision.council.engine.run_council` — the single entry point; returns a
    Draft-shaped dict.
  * :class:`~vision.council.voices.Voices` — the headless voice transport.
  * :class:`~vision.council.topics.TopicEngine` — topic selection (queue + propose).
  * :class:`~vision.council.deliberate.Deliberator` / :class:`Deliberation`.
  * :class:`~vision.council.compose.Composer` / :class:`ComposedPost`.
  * :data:`~vision.council.formats.FORMATS` / :class:`RecentFormatStore`.
"""

from __future__ import annotations

from vision.council.compose import ComposedPost, Composer
from vision.council.deliberate import Deliberation, Deliberator
from vision.council.engine import run_council
from vision.council.formats import FORMATS, RecentFormatStore
from vision.council.topics import TopicEngine
from vision.council.voices import CLAUDE, CODEX, GEMINI, VOICE_ORDER, Voices

__all__ = [
    "run_council",
    "Voices",
    "VOICE_ORDER",
    "GEMINI",
    "CODEX",
    "CLAUDE",
    "TopicEngine",
    "Deliberator",
    "Deliberation",
    "Composer",
    "ComposedPost",
    "FORMATS",
    "RecentFormatStore",
]
