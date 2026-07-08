"""NotebookLM 'source pack' generator — the automatable half of the NotebookLM
video bridge (BRD §23 video lane).

NotebookLM has NO public API (verified 2026-07-08), so a Video Overview cannot be
triggered or fetched programmatically. But its output quality is driven entirely
by its SOURCES + the steering prompt. This module produces a tight, opinionated
source document from a finished VISION post so that, when the owner points
NotebookLM at it (the doc syncs to Drive with the rest of ``notebook/``), the
generated overview stays SHORT, on-message, and presentable — instead of the
verbose 3-8 min explainer NotebookLM defaults to.

Flow (semi-manual, on purpose):
  1. VISION writes this pack into ``notebook/video_packs/`` (-> Drive -> NotebookLM).
  2. Owner generates a Video Overview in NotebookLM using the STEERING PROMPT below.
  3. Owner downloads the MP4; VISION uploads + posts it (reuses publish/video upload).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# The prompt the owner pastes into NotebookLM's "customise" box when generating the
# Video Overview — the lever that forces a tight, on-message, <60s result.
STEERING_PROMPT = (
    "Make a polished VERTICAL video overview UNDER 60 SECONDS. Deliver EXACTLY the "
    "one message stated in the source, nothing more. No intro fluff, no 'in this "
    "video', no recap. Punchy, presentable, first-person, confident. Plain spoken."
)


def build_source_pack(title: str, post_text: str, one_line_message: str) -> str:
    """Return a NotebookLM-optimized source document (markdown) for one post.

    The doc leads with the ONE message and a hard length target so NotebookLM's
    overview stays tight and on-point; the full post follows as supporting detail.
    """
    return (
        f"# {title.strip()}\n\n"
        "## THE ONE MESSAGE (the video must land exactly this, and only this)\n"
        f"{one_line_message.strip()}\n\n"
        "## TARGET\n"
        "A polished vertical (9:16) video overview UNDER 60 SECONDS. Tight and "
        "presentable, no filler or preamble. First-person, confident, plain spoken.\n\n"
        "## STEERING PROMPT (paste into NotebookLM's customise box)\n"
        f"{STEERING_PROMPT}\n\n"
        "## THE FULL PIECE (supporting detail — do not read verbatim)\n"
        f"{post_text.strip()}\n"
    )


def _slug(title: str) -> str:
    """A filesystem-safe slug from a title (lowercase, hyphens, bounded)."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (s or "video-pack")[:60]


def write_source_pack(
    title: str, post_text: str, one_line_message: str, *, out_dir: str | os.PathLike[str]
) -> Path:
    """Write the source pack under ``out_dir`` and return its path.

    Point ``out_dir`` at a Drive-synced folder (e.g. ``notebook/video_packs``) so
    NotebookLM can pick it up as a source after the next sync.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_slug(title)}.md"
    path.write_text(build_source_pack(title, post_text, one_line_message), encoding="utf-8")
    return path
