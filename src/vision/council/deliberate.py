"""The two-round council deliberation (BRD §5, verbatim from the prototype).

WHY this module exists: genuine thought leadership needs genuine deliberation, not
a single model's monologue. The council runs TWO honest rounds:

  * **Round 1 — independent takes.** Each voice gives its OWN distinctive position
    (clear side, no hedging) without seeing the others.
  * **Round 2 — responding to each other.** Each voice sees the others' round-1
    takes and decides whether to hold, sharpen, or CHANGE its position.

The result is a :class:`Deliberation` carrying both rounds per voice — the raw
material the compose step turns into a post (and the honesty gate reads to decide
whether the council genuinely disagreed / agreed / shifted).

The deliberation/response PROMPTS below are VERBATIM from the proven, owner-
approved prototype (``scripts/council.py`` ``deliberate()``) — do NOT reword them;
their wording is part of the proven content path. Only the plumbing (config-driven
voices, fail-closed on too-few-takes) is refactored around them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vision.config import Settings, get_settings
from vision.council.voices import VOICE_ORDER, Voices

logger = logging.getLogger(__name__)

# Fail-closed threshold: with fewer than TWO round-1 takes there is no council —
# a single voice is a monologue, and there is nothing to deliberate. The engine
# turns this into a hard error rather than publishing a solo "council" post.
_MIN_TAKES = 2


@dataclass
class Deliberation:
    """The raw two-round debate: round1/round2 answer per voice.

    ``round1`` and ``round2`` map voice name → that voice's answer text (``""``
    for a voice that failed that round, so downstream code can detect degradation).
    This is the un-edited transcript; it is NEVER published as-is — compose reads
    it and writes the de-named post.
    """

    topic: str
    round1: dict[str, str]
    round2: dict[str, str]

    def live_voices(self) -> list[str]:
        """Return the voices that produced a non-empty round-1 take.

        Used by the engine's fail-closed check: a council needs at least
        :data:`_MIN_TAKES` live voices to be a real deliberation.
        """
        return [voice for voice in VOICE_ORDER if self.round1.get(voice)]


class Deliberator:
    """Runs the two-round deliberation over the three voices.

    The :class:`Voices` transport is injected so unit tests MOCK ``ask`` and no
    real model is ever called. Voices are iterated in the canonical
    :data:`VOICE_ORDER` so rounds are deterministic.
    """

    def __init__(
        self,
        voices: Voices | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Resolve the voice transport (and its config)."""
        self._settings = settings or get_settings()
        self._voices = voices or Voices(self._settings)

    def deliberate(self, topic: str) -> Deliberation:
        """Run round 1 (independent) then round 2 (responding), return the debate.

        Fail-soft per voice (a dead voice yields ``""`` for its round), fail-CLOSED
        overall: if fewer than :data:`_MIN_TAKES` voices produced a round-1 take,
        raise :class:`RuntimeError` — there is no honest council to compose from.

        Args:
            topic: The subject the council debates.

        Returns:
            A :class:`Deliberation` with both rounds per voice.

        Raises:
            RuntimeError: when too few voices produced a round-1 take.
        """
        # --- Round 1: independent takes (prompt VERBATIM from the prototype) ---
        r1_prompt = (
            "You are ONE of three AIs on a public thought-leadership council. Give YOUR "
            "genuine, distinctive position in 4-6 sentences — take a clear side, no "
            "'it depends' hedging, no balanced summary. State your strongest view and the "
            f"core reason. Topic: {topic}"
        )
        logger.info("Council round 1 — independent takes.")
        round1 = {voice: self._voices.ask(voice, r1_prompt) for voice in VOICE_ORDER}

        # Fail-closed: too few live takes means no real deliberation. We check
        # HERE (before wasting round-2 calls) so a broken council fails fast.
        live = [voice for voice in VOICE_ORDER if round1.get(voice)]
        if len(live) < _MIN_TAKES:
            raise RuntimeError(
                f"Council deliberation failed: only {len(live)} voice(s) produced a take, "
                f"need at least {_MIN_TAKES}."
            )

        # --- Round 2: respond to each other (prompt VERBATIM from the prototype) ---
        logger.info("Council round 2 — responding to each other.")
        round2: dict[str, str] = {}
        for voice in VOICE_ORDER:
            # Build the "others said" block from the OTHER voices' non-empty takes.
            others = "\n\n".join(
                f"{other} said: {round1[other]}"
                for other in round1
                if other != voice and round1[other]
            )
            r2_prompt = (
                f"You are {voice} on a three-AI council. Topic: {topic}\n\nYour first take was:\n"
                f"{round1[voice]}\n\nThe other two said:\n{others}\n\nIn 3-5 sentences: do you hold, "
                "sharpen, or CHANGE your position? Engage their strongest point directly — "
                "agree where they're right, push back where they're wrong. Be honest, not polite."
            )
            round2[voice] = self._voices.ask(voice, r2_prompt)

        return Deliberation(topic=topic, round1=round1, round2=round2)
