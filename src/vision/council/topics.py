"""The council topic engine (BRD §5 council-content-vision).

WHY this module exists: an autonomous thought community needs a steady supply of
*novel, thought-provoking* topics across ANY domain — tech, ethics, healthcare,
leadership, culture, everyday-life, humour — while (a) respecting an owner-editable
EXCLUSION guardrail (topics the council must never touch) and (b) honouring an
owner topic QUEUE that lets the owner steer the day's subject. This module is the
single seam that decides *what* the council debates:

  * :func:`propose_topics` — asks the council to PROPOSE N candidate topics.
  * exclusion filtering — drops any candidate/queued topic hitting the guardrail.
  * :func:`load_owner_queue` — reads the owner's FIFO topic file (one per line).
  * :func:`pick_topic` — owner-queue FIRST, else propose-and-pick a non-recent one.

Mood/tone variety is NOT this module's job — the compose step already ranges from
provocative to warm/funny per topic (§5). Here we only choose the subject.

Config over code (§22.6): the queue file path, the exclusion list, and the
recent-topic window all come from :class:`~vision.config.Settings`. The queue path
is expanduser'd so a '~/...'  path resolves on every OS.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from vision.config import Settings, get_settings
from vision.council.voices import CLAUDE, Voices

logger = logging.getLogger(__name__)

# The prompt that asks a single voice to propose novel topics. Kept close to the
# prototype's spirit (novel, thought-provoking, cross-domain) and explicit about
# the output shape so parsing is robust.
_PROPOSE_PROMPT_TEMPLATE = (
    "You are the programming committee of a public thought community that thinks "
    "out loud. Propose {n} NOVEL, thought-provoking discussion topics — each a "
    "single sharp sentence — spanning ANY domain: technology, ethics, healthcare, "
    "leadership, culture, everyday life, even humour. Avoid clichés and anything "
    "already over-discussed. Output ONE topic per line, no numbering, no extra prose."
)


def _is_excluded(topic: str, exclusions: list[str]) -> bool:
    """Return True if ``topic`` trips the guardrail (case-insensitive substring).

    A substring match (not exact) means an owner can exclude a THEME ("politics")
    and catch every phrasing of it, which is the useful guardrail semantics for a
    free-text topic. Empty/whitespace exclusion entries are ignored so a stray
    blank line in config can't blackhole every topic.
    """
    haystack = topic.casefold()
    return any(
        term.strip() and term.strip().casefold() in haystack for term in exclusions
    )


def _filter_exclusions(topics: list[str], exclusions: list[str]) -> list[str]:
    """Drop every topic that trips the exclusion guardrail, preserving order."""
    return [t for t in topics if not _is_excluded(t, exclusions)]


class TopicEngine:
    """Chooses what the council debates: owner queue first, else propose-and-pick.

    Dependencies (the :class:`Voices` transport and :class:`Settings`) are injected
    so unit tests MOCK the voices and point the queue path at a temp file — no real
    model call and no dependence on the developer's environment.
    """

    def __init__(
        self,
        voices: Voices | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Resolve config + the voice transport once."""
        self._settings = settings or get_settings()
        self._voices = voices or Voices(self._settings)

    @property
    def exclusions(self) -> list[str]:
        """The owner-editable list of forbidden topic themes (from settings)."""
        return list(self._settings.council_exclusions)

    def propose_topics(self, n: int = 5) -> list[str]:
        """Ask the council to propose ``n`` novel topics, exclusion-filtered.

        A single voice (Claude, the editorial voice) proposes; each non-empty line
        becomes a candidate. The guardrail is applied HERE so an excluded theme can
        never reach :func:`pick_topic`. Returns ``[]`` if the voice failed or every
        candidate was excluded — the caller decides how to degrade (fail-closed).

        Args:
            n: How many topics to request (a hint to the model; the actual count
                returned may differ and is bounded only by the guardrail).
        """
        if n < 1:
            # A non-positive request is a caller bug; degrade to a single topic
            # rather than sending a nonsensical prompt.
            n = 1
        prompt = _PROPOSE_PROMPT_TEMPLATE.format(n=n)
        raw = self._voices.ask(CLAUDE, prompt)
        # One topic per non-empty line; strip stray numbering/bullets defensively.
        candidates = [
            line.strip().lstrip("-*0123456789. ").strip()
            for line in raw.splitlines()
            if line.strip()
        ]
        candidates = [c for c in candidates if c]
        filtered = _filter_exclusions(candidates, self.exclusions)
        if not filtered:
            logger.warning("Council proposed no usable topics after exclusion filtering.")
        return filtered

    def load_owner_queue(self) -> list[str]:
        """Return the owner's FIFO topic queue (one topic per line), exclusion-filtered.

        Reads ``COUNCIL_TOPIC_QUEUE_PATH`` (expanduser'd). Blank lines and lines
        starting with ``#`` (comments) are skipped. A missing/unreadable file is a
        NORMAL state (no queue today) → returns ``[]`` fail-soft. The guardrail is
        applied so an excluded theme in the queue is quietly dropped, never posted.
        """
        raw_path = self._settings.council_topic_queue_path
        if not raw_path:
            return []
        path = Path(os.path.expanduser(raw_path))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # No queue file today — a normal, expected state, not an error.
            return []
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return _filter_exclusions(lines, self.exclusions)

    def consume_owner_queue_head(self) -> str | None:
        """Pop and return the FIRST queued owner topic (FIFO), rewriting the file.

        WHY consume-on-read: the queue is a to-do list — a topic used today must
        not resurface tomorrow. We read the exclusion-filtered queue, take its
        head, and rewrite the file WITHOUT that head (preserving the tail). A
        rewrite failure is logged (class only) and the head is still returned, so a
        transient write error at most re-uses one topic rather than skipping it.

        Returns the head topic, or ``None`` when the queue is empty/absent.
        """
        raw_path = self._settings.council_topic_queue_path
        if not raw_path:
            return None
        path = Path(os.path.expanduser(raw_path))
        # Read the RAW lines (unfiltered) so we can faithfully rewrite the tail,
        # but pick the head from the exclusion-FILTERED view so an excluded head is
        # skipped over rather than served.
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None

        raw_lines = [line.rstrip("\n") for line in text.splitlines()]
        # Find the first non-blank, non-comment, non-excluded line to serve.
        chosen_index: int | None = None
        for index, line in enumerate(raw_lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _is_excluded(stripped, self.exclusions):
                continue
            chosen_index = index
            break

        if chosen_index is None:
            return None

        head = raw_lines[chosen_index].strip()
        # Rewrite the file without the consumed line (keep everything else, incl.
        # comments/blank lines the owner left, so their file structure survives).
        remaining = raw_lines[:chosen_index] + raw_lines[chosen_index + 1 :]
        try:
            path.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Council could not rewrite the topic queue (%s); topic may repeat.",
                exc.__class__.__name__,
            )
        return head

    def pick_topic(self, recent_topics: list[str] | None = None, propose_n: int = 5) -> str:
        """Choose the council's topic: owner queue FIRST, else propose-and-pick.

        Order of preference (§5 council):
          1. The owner's queued topic (FIFO) — the owner's steer always wins.
          2. Otherwise the council proposes ``propose_n`` topics and we pick the
             first that is NOT a recent repeat (variety), falling back to the first
             candidate if all are recent.

        Args:
            recent_topics: Recently-used topics to avoid repeating (case-insensitive).
            propose_n: How many topics to request when proposing.

        Returns:
            The chosen topic string.

        Raises:
            RuntimeError: fail-closed when neither the queue nor a proposal yields
                any usable topic (a dead voice + empty queue) — the engine must not
                fabricate a subject out of nothing.
        """
        # 1. Owner queue first — a queued topic is consumed FIFO.
        queued = self.consume_owner_queue_head()
        if queued:
            logger.info("Council using owner-queued topic.")
            return queued

        # 2. Propose and pick a non-recent candidate.
        recent = {t.casefold() for t in (recent_topics or [])}
        candidates = self.propose_topics(propose_n)
        if not candidates:
            # Fail-closed: no queue and no proposal → nothing honest to say.
            raise RuntimeError(
                "Council could not obtain a topic: owner queue empty and no topics proposed."
            )
        for candidate in candidates:
            if candidate.casefold() not in recent:
                return candidate
        # Every candidate was a recent repeat — better to reuse than to invent, so
        # take the first proposed candidate.
        logger.info("All proposed topics were recent repeats; using the first candidate.")
        return candidates[0]
