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

import json
import logging
import os
import random
import re
from dataclasses import dataclass
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
    "single sharp sentence. For THIS round, focus the topics on: {domain}. Stay "
    "concrete, current, and specific (not vague philosophy); avoid clichés and "
    "over-discussed angles. Output ONE topic per line, no numbering, no extra prose."
)

# The owner's positioning is a TECH-LEANING cross-domain generalist (2026-07-08):
# ~60% of rounds steer toward technology/AI/building, ~40% toward the human /
# cross-domain range. One domain is drawn per proposal round; the weighting is the
# repetition below. This is the deliberate counterweight to the models' natural
# drift into abstract philosophy — without it every post trends humanities-essay.
_DOMAINS: tuple[str, ...] = (
    # Tech-leaning (~60%) — the builder/technologist lens, explored with a wide mind.
    "artificial intelligence: where it is really heading and what it quietly changes",
    "software engineering and systems design — the craft and the hard truths of building",
    "product and design decisions that reveal something deeper about people",
    "data, algorithms, and what our tools are actually optimizing for",
    "the economics, incentives, and power dynamics of technology and AI",
    "the frontier of AI/tech and its second- and third-order effects",
    # Human / cross-domain (~40%). REWEIGHTED 2026-08-04 (owner: "we are posting too
    # much of this"): healthcare/institutional-failure pieces had come to dominate
    # and every post read grave. Healthcare keeps ONE slot instead of leaning on the
    # owner's day job, and the lighter registers get real weight so the feed varies
    # in MOOD, not just subject.
    "human behavior — how we actually think, decide, and connect",
    "a lighter, funnier, or more surprising everyday observation",
    "something small and human you noticed this week that turned out to say a lot",
    "a curiosity from science, history, or craft that reframes how you see a problem",
    "culture and society, and how technology is quietly reshaping them",
    "healthcare and care systems — ONLY with a genuinely fresh, non-obvious angle",
)


@dataclass(frozen=True)
class RecentTopicStore:
    """Durable memory of recently-debated TOPICS (most-recent-first, capped).

    Mirrors :class:`~vision.council.formats.RecentFormatStore`'s contract and
    fail-soft discipline, but for subjects rather than shapes: a missing/corrupt
    file reads as no history and a write failure is logged and swallowed, because
    variety bookkeeping must never crash the council's content.
    """

    #: Where the recent-topic history is persisted (already expanduser'd).
    path: Path
    #: How many recent topics to remember and steer away from.
    window: int = 8

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RecentTopicStore":
        """Build a store from ``COUNCIL_TOPIC_STATE_PATH`` + the topic window."""
        settings = settings or get_settings()
        path = Path(os.path.expanduser(settings.council_topic_state_path))
        window = max(1, settings.council_recent_topic_window)
        return cls(path=path, window=window)

    def recent(self) -> list[str]:
        """Return recently-debated topics, most recent first (``[]`` on any problem)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("Council recent-topic state is not valid JSON; ignoring it.")
            return []
        if not isinstance(data, list):
            logger.warning("Council recent-topic state is not a list; ignoring it.")
            return []
        return [t for t in data if isinstance(t, str)][: self.window]

    def remember(self, topic: str) -> None:
        """Record ``topic`` as most-recently-debated, bounded to ``window``."""
        updated = ([topic] + self.recent())[: self.window]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(updated), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Council could not persist recent topics (%s); variety not stamped.",
                exc.__class__.__name__,
            )


_PREAMBLE_RE = re.compile(r"^(here (are|is)|topics?\b|sure|okay|below are)\b", re.IGNORECASE)


def _looks_like_preamble(line: str) -> bool:
    """True for a meta-scaffolding line the model leaked instead of a topic."""
    return bool(_PREAMBLE_RE.match(line.strip()))


# Words too common to signal a shared subject — comparing on these would make every
# topic look like every other one.
_THEME_STOPWORDS: frozenset[str] = frozenset(
    {
        "about", "after", "again", "against", "because", "been", "before", "being",
        "between", "both", "could", "does", "doing", "from", "have", "having", "into",
        "just", "keeps", "like", "made", "make", "makes", "many", "more", "most",
        "much", "must", "never", "only", "other", "over", "really", "same", "should",
        "since", "some", "still", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "thing", "things", "this", "those", "through",
        "under", "until", "very", "what", "when", "where", "which", "while", "with",
        "without", "would", "your",
    }
)

# How many significant words two topics must share before we call them the same
# theme. Two is the sweet spot: one shared word fires on coincidence ("AI"), three
# almost never fires on genuine repeats.
_THEME_OVERLAP_MIN = 2


def _theme_words(topic: str) -> set[str]:
    """Reduce a topic to its significant lowercase words (4+ chars, non-stopword)."""
    words = re.findall(r"[a-z]{4,}", topic.casefold())
    return {w for w in words if w not in _THEME_STOPWORDS}


def _shares_theme(candidate: str, recent_topics: list[str]) -> bool:
    """True when ``candidate`` covers the same ground as any recent topic.

    Compares significant-word overlap rather than exact strings: a proposer never
    repeats a topic verbatim, it repeats the SUBJECT ("hospitals ... infusion
    pumps" then "hospitals ... legacy systems"). Overlap of
    :data:`_THEME_OVERLAP_MIN` significant words counts as the same theme.
    """
    candidate_words = _theme_words(candidate)
    if not candidate_words:
        return False
    for recent in recent_topics:
        if len(candidate_words & _theme_words(recent)) >= _THEME_OVERLAP_MIN:
            return True
    return False


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
        # Draw a domain for this round (weighted ~60% tech via _DOMAINS repetition)
        # so topics rotate across the owner's tech-leaning cross-domain range.
        prompt = _PROPOSE_PROMPT_TEMPLATE.format(n=n, domain=random.choice(_DOMAINS))
        raw = self._voices.ask(CLAUDE, prompt)
        # One topic per non-empty line; strip stray numbering/bullets defensively.
        candidates = [
            line.strip().lstrip("-*0123456789. ").strip()
            for line in raw.splitlines()
            if line.strip()
        ]
        # Drop a leaked preamble line ("Here are 3 topics for this round:") that the
        # model sometimes prepends despite the "no extra prose" instruction — a line
        # that is meta-scaffolding, not a topic (ends with a colon or announces a list).
        candidates = [
            c
            for c in candidates
            if c and not (c.endswith(":") or _looks_like_preamble(c))
        ]
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
        # Prefer a candidate that shares no THEME with a recent topic. Exact-string
        # matching alone was useless here (a proposer never repeats a topic word for
        # word, it repeats the subject), which is how the feed drifted into the same
        # territory post after post.
        for candidate in candidates:
            if candidate.casefold() not in recent and not _shares_theme(
                candidate, recent_topics or []
            ):
                return candidate
        for candidate in candidates:
            if candidate.casefold() not in recent:
                logger.info("Every proposed topic echoed a recent theme; taking the freshest.")
                return candidate
        # Every candidate was a recent repeat — better to reuse than to invent, so
        # take the first proposed candidate.
        logger.info("All proposed topics were recent repeats; using the first candidate.")
        return candidates[0]
