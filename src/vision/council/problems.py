"""The owner's freeform 'problem' inbox — grounding for the overcome-story series.

The owner brain-dumps real problems (what broke, what they tried, how they solved
it, the lesson) into ``COUNCIL_PROBLEM_QUEUE_PATH``, one blob per entry separated
by a ``---`` line. Seeds-first: a queued problem becomes the day's GROUNDED post
before any auto-topic. This is the single biggest authenticity lever — the raw
material is real, so the post is grounded in what actually happened.

The queue is FIFO and consumed exactly once: reading a problem for a run also
removes it from the file (so a re-run never re-posts the same problem), mirroring
the topic queue's consume semantics.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)

# A separator line of at least three dashes/equals/underscores between problems.
_DELIMITER = re.compile(r"^\s*[-=_]{3,}\s*$", re.MULTILINE)


class ProblemQueue:
    """Reads + consumes freeform problem blobs from the owner's inbox file."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _path(self) -> Path:
        # expanduser so a '~'-based path works regardless of the process cwd.
        return Path(os.path.expanduser(self._settings.council_problem_queue_path))

    def _read_blocks(self) -> list[str]:
        """Return non-empty problem blobs in file order, or [] if none/no file."""
        path = self._path()
        if not path.is_file():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("problem queue unreadable (%s); treating as empty.", exc.__class__.__name__)
            return []
        # Skip an HTML-comment block so the inbox file can carry a usage header
        # that is never mistaken for a problem.
        return [
            block.strip()
            for block in _DELIMITER.split(text)
            if block.strip() and not block.strip().startswith("<!--")
        ]

    def peek(self) -> str | None:
        """Return the next problem blob WITHOUT consuming it, or None."""
        blocks = self._read_blocks()
        return blocks[0] if blocks else None

    def consume_head(self) -> str | None:
        """Pop + return the first problem blob (FIFO), rewriting the file without it.

        Returns None when the inbox is empty. The write is best-effort: if it fails,
        the blob is still returned (the run proceeds) but may reappear next run — a
        duplicate is a far better failure than losing the owner's problem.
        """
        blocks = self._read_blocks()
        if not blocks:
            return None
        head, rest = blocks[0], blocks[1:]
        path = self._path()
        try:
            path.write_text(("\n\n---\n\n".join(rest) + "\n") if rest else "", encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "could not rewrite problem queue after consume (%s); blob may re-run.",
                exc.__class__.__name__,
            )
        return head
