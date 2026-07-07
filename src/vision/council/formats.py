"""The format-variety library + recent-format tracking (BRD §5 council).

WHY this module exists: the council must NEVER publish the same shape twice in a
row — a thought community that always "shows the split" becomes formulaic. The
composer picks the ONE format that most honestly fits what actually happened in
the debate, but must AVOID the recently-used ones. This module owns (a) the menu
of formats (:data:`FORMATS`, verbatim from the proven prototype) and (b) durable
recent-format memory so variety survives across process restarts.

Persistence design (learning from the prototype's hard-coded ``prep/`` path):
recent formats live in a small JSON state file whose location is
*configurable* (``COUNCIL_STATE_PATH``) and expanduser'd, NOT baked to ``prep/``.
The :class:`RecentFormatStore` is a tiny, injectable seam so unit tests can use a
temp path (or an in-memory fake) and never touch a developer's real state file.
A read/write failure fails SOFT toward variety: on a corrupt/missing file we
treat history as empty (so we simply don't over-suppress), never crashing the
council over its own memory.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)

# --- The format menu (VERBATIM from the proven prototype) -------------------
# WHY verbatim: these descriptions are owner-approved content, tuned so the
# composer picks the honest shape. Do NOT reword — add new entries freely, but
# the existing wording is part of the proven path.
FORMATS: dict[str, str] = {
    "show_the_split": "Surface the genuine disagreement: name who argued what and why the tension matters.",
    "rare_consensus": "Use ONLY if all three genuinely converged: frame the rare agreement as itself the signal.",
    "steelman_both": "Give the strongest case for each opposing side, let the reader sit in the tension.",
    "one_changed_mind": "Use ONLY if a voice actually shifted its position: tell that story.",
    "provocation": "Open with one sharp question, give the three answers in a line each, end on the reader.",
    "uncomfortable_middle": "Synthesise a non-obvious THIRD position none of the three fully held.",
    "what_they_missed": "Argue what all three AIs overlooked — leaving a clear slot for the human's lived-experience counter.",
    "quiet_observation": "No debate framing at all: publish the single sharpest insight as a plain, human reflection.",
}


@dataclass
class RecentFormatStore:
    """Durable memory of recently-used formats, persisted to a JSON state file.

    The store keeps a most-recent-first list of format names, capped at
    ``window`` entries, so the composer can avoid repeating the last ~N shapes.
    The path is config-driven and expanduser'd (never hard-coded to ``prep/``).

    Fail-soft on I/O: a missing or corrupt file reads as an empty history (we
    simply don't suppress anything), and a write failure is logged (class only)
    and swallowed — the council's *content* must never crash on its own variety
    bookkeeping.
    """

    #: Where the recent-format history is persisted (already expanduser'd).
    path: Path
    #: How many most-recent formats to remember/avoid (the variety window).
    window: int = 4

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RecentFormatStore":
        """Build a store from :class:`~vision.config.Settings`.

        Reads ``COUNCIL_STATE_PATH`` (expanduser'd — a '~/...'  path resolves on
        every OS) and ``COUNCIL_RECENT_WINDOW``. This is the single place the
        config → store wiring lives, so callers just do
        ``RecentFormatStore.from_settings()``.
        """
        settings = settings or get_settings()
        path = Path(os.path.expanduser(settings.council_state_path))
        # A non-positive window would disable variety entirely; clamp to 1 so a
        # fat-fingered 0 can't turn the council into a broken-record.
        window = max(1, settings.council_recent_window)
        return cls(path=path, window=window)

    def recent(self) -> list[str]:
        """Return the most-recent-first list of recently-used format names.

        Fail-soft: a missing file, unreadable file, or non-list/corrupt JSON all
        read as ``[]`` so the council never crashes on its own memory and simply
        doesn't over-suppress. The result is capped at ``window`` defensively in
        case an older file held more.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            # No state file yet (first run) or unreadable — treat as empty history.
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            # Corrupt JSON — log the class, treat as empty rather than crashing.
            logger.warning("Council recent-format state is not valid JSON; ignoring it.")
            return []
        if not isinstance(data, list):
            # Wrong shape on disk — ignore rather than trust it.
            logger.warning("Council recent-format state is not a list; ignoring it.")
            return []
        # Keep only string entries (defensive) and cap to the window.
        return [name for name in data if isinstance(name, str)][: self.window]

    def remember(self, name: str) -> None:
        """Record ``name`` as the most-recently-used format (bounded to ``window``).

        Prepends the new format and truncates to the variety window, then persists
        (creating parent dirs as needed). A write failure is logged (class only)
        and swallowed — a failure to *stamp* variety must not crash the council;
        at worst the next run repeats a format one time.
        """
        updated = ([name] + self.recent())[: self.window]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(updated), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Council could not persist recent formats (%s); variety not stamped.",
                exc.__class__.__name__,
            )

    def menu_avoiding_recent(self) -> dict[str, str]:
        """Return the FORMATS menu with recently-used shapes removed.

        WHY fall back to the full menu when the filter would empty it: if every
        format is 'recent' (a tiny window vs. few formats, or a long history), an
        empty menu would leave the composer with nothing to pick — so we return
        the full :data:`FORMATS` rather than an impossible empty choice (mirrors
        the prototype's ``menu or FORMATS``).
        """
        avoid = set(self.recent())
        filtered = {name: desc for name, desc in FORMATS.items() if name not in avoid}
        return filtered or dict(FORMATS)
