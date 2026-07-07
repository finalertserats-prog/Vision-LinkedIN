"""The compose step: turn a deliberation into a de-named LinkedIn post (BRD §5).

WHY this module exists: the raw debate is never published. The composer (the
editorial Claude voice) reads the two-round deliberation and writes ONE LinkedIn
post plus an unnamed 'Council' block, obeying the owner-approved HARD RULES:

  * **De-named voices** — the post/council block NEVER name Gemini/Codex/Claude/
    GPT/'the model'; the only attribution anywhere is the final line
    'Powered by Brahmastra'.
  * **Honesty gate** — the composer first judges whether the council genuinely
    DISAGREED, AGREED, or one voice SHIFTED, and never manufactures a fight that
    didn't happen.
  * **Format variety** — it picks the ONE format that honestly fits, avoiding the
    recently-used ones (from :class:`~vision.council.formats.RecentFormatStore`).
  * **Tonal range** — provocative for weighty topics, warm/playful/funny for
    lighter ones (a thought COMMUNITY, not a debate club).

The COMPOSE PROMPT below is VERBATIM from the proven, owner-approved prototype
(``scripts/council.py`` ``compose()``) — do NOT reword it. This module adds
structured PARSING of the model's fixed-shape output into a
:class:`ComposedPost` (format / situation / post_text / council_block / hashtags)
so the rest of VISION consumes a typed result, and a FAIL-CLOSED de-naming gate:
if a forbidden model/vendor name (or 'the model') leaks into the post or council
block, :meth:`Composer.compose` raises :class:`ForbiddenNameError` and never
returns the leaking post. The same detector is re-run at the publish end
(:mod:`vision.publish.worker`) on the exact bytes about to hit LinkedIn, so the
#1 rule (NO AI names in published text) is enforced, not merely advised.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field

from vision.config import Settings, get_settings
from vision.council.deliberate import Deliberation
from vision.council.formats import FORMATS, RecentFormatStore
from vision.council.voices import CLAUDE, VOICE_ORDER, Voices

logger = logging.getLogger(__name__)

# The author profile the composer ghost-writes for. Reframed 2026-07-07 per owner
# feedback: the previous "hospital owner" phrasing made EVERY post open with "I run
# a hospital" (repetitive/cringe). The persona is now a cross-domain thinker so the
# post's relevance can VARY — see _LENSES and the RELEVANCE VARIETY rule below.
VOICE_PROFILE = (
    "Vishnu Dattu Kurnuthala — a builder and healthcare operator who thinks across "
    "domains (technology, science, ethics, business, and everyday life). Pragmatic, "
    "curious, non-hype, evidence-grounded, willing to sit with a hard question. No "
    "clickbait, no emoji-spam, no fabricated quotes or stats, no medical advice."
)

# Relevance lenses — HOW a post connects to the reader. Rotated run-to-run so the
# author's job/credentials are NOT the default anchor. "No personal anchor" is
# weighted first because letting the idea stand on its own is usually the strongest,
# least self-referential choice.
_LENSES = (
    "no personal anchor at all — let the idea carry the whole post",
    "a specific, concrete scene or moment (NOT a job title or credential)",
    "a builder's / technologist's lens",
    "a plain, curious-human reflection",
    "a sharp contrarian observation",
    "a question the author genuinely can't answer",
    "history, science, or culture as the way in",
)

# The final attribution line — the ONLY attribution allowed anywhere in output.
_BRAHMASTRA_SIGNATURE = "Powered by Brahmastra"

# AI/model/vendor names (and the generic "the model") that must NEVER appear in
# the published post or council block. WHY a HARD guard even though the prompt
# forbids them: the composing model occasionally slips (its own comment says so),
# and de-naming is the #1 published-text rule — so this is the single source of
# truth wired into a FAIL-CLOSED gate at BOTH the compose end (below) and the
# publish end (:mod:`vision.publish.worker`), never advisory-only.
#
# Matched CASE-INSENSITIVELY with WORD BOUNDARIES so lowercase/possessive/vendor
# variants the prompt itself forbids are caught ('gemini', "gemini's", 'gpt-4',
# 'chatgpt', 'openai', 'anthropic', 'bard'), while ordinary words that merely
# CONTAIN a token are NOT false positives — 'clause' must not trip 'claude',
# 'gospel' must not trip 'gpt'. The boundary is expressed per-token so 'gpt-4' /
# 'gpt4' still match (a following digit/hyphen is part of the model name, not a
# boundary that hides it).
_FORBIDDEN_NAME_TOKENS: tuple[str, ...] = (
    "gemini",
    "codex",
    "claude",
    "gpt",
    "chatgpt",
    "openai",
    "anthropic",
    "bard",
)

# \b before the token (so 'clause' does not match 'claude'); a trailing lookahead
# that rejects a following WORD char ONLY when it is a letter — a digit or hyphen
# after the token ('gpt-4', 'gpt4') is still a model reference and must match.
# 'the model' / 'the models' is matched separately as a whole phrase.
_FORBIDDEN_NAME_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(tok) for tok in _FORBIDDEN_NAME_TOKENS)
    + r")(?![A-Za-z])"
    + r"|\bthe models?\b",
    re.IGNORECASE,
)

# Matches a "#hashtag" token (letters/digits/underscore after the hash).
_HASHTAG_RE = re.compile(r"#\w+")


@dataclass
class ComposedPost:
    """The structured, de-named result of the compose step.

    Fields mirror the fixed output shape the compose prompt emits, parsed into
    typed values the rest of VISION consumes without re-parsing the raw blob.
    """

    format: str  # chosen format name (a FORMATS key, or 'unknown' if unparsable)
    situation: str  # 'disagreed'|'agreed'|'shifted' — one line (honesty gate)
    post_text: str  # the LinkedIn post body (de-named)
    council_block: str  # the unnamed 3-bullet 'Council' block + signature line
    hashtags: list[str] = field(default_factory=list)  # hashtags parsed from the post
    raw: str = ""  # the composer's full raw output (for transcript/debug)


class ForbiddenNameError(RuntimeError):
    """A published surface named an AI/model/vendor — de-naming FAILED CLOSED.

    Raised by :meth:`Composer.compose` (and re-checked at the publish end) when a
    forbidden token leaks into the post/council block despite the HARD RULES. It
    is fatal by design: the #1 rule is that NO model name reaches published text,
    so a leak aborts rather than quietly ships. Carries the offending ``match`` for
    the log/alert (the surrounding text is NOT logged — it may be unrelated).
    """

    def __init__(self, match: str) -> None:
        self.match = match
        super().__init__(f"published text names a forbidden AI/model: {match!r}")


def find_forbidden_name(text: str) -> str | None:
    """Return the first AI/model/vendor token ``text`` leaks, or ``None`` if clean.

    Case-INSENSITIVE with word boundaries (see :data:`_FORBIDDEN_NAME_RE`): catches
    lowercase/possessive/vendor variants ('gemini', "gemini's", 'gpt-4', 'chatgpt',
    'openai', 'anthropic', 'bard') and the generic 'the model', while an ordinary
    word that merely contains a token ('clause') is NOT a false positive. Returns
    the matched substring (for logging/alerting) so the caller can fail closed.
    """
    match = _FORBIDDEN_NAME_RE.search(text)
    return match.group(0) if match else None


def contains_forbidden_name(text: str) -> bool:
    """Return True if ``text`` names any AI/model/vendor (a de-naming violation).

    Thin boolean wrapper over :func:`find_forbidden_name` (the single source of
    truth). Used by tests to assert de-naming; the engine/publisher use the gate.
    """
    return find_forbidden_name(text) is not None


def _parse_composition(raw: str) -> ComposedPost:
    """Parse the composer's fixed-shape output into a :class:`ComposedPost`.

    The compose prompt emits EXACTLY:

        FORMAT: <name>
        SITUATION: <disagreed|agreed|shifted> — <why>
        POST:
        <post body...>
        COUNCIL:
        • <viewpoint 1>
        • <viewpoint 2>
        • <viewpoint 3>
        Powered by Brahmastra

    We scan line-by-line: single-line headers (FORMAT/SITUATION) are captured
    directly; POST and COUNCIL are multi-line sections that run until the next
    header. Robust to leading/trailing whitespace and to the model omitting a
    section (missing pieces come back empty rather than raising here — the
    fail-closed de-naming gate in :meth:`Composer.compose` then vets the result).
    """
    fmt = "unknown"
    situation = ""
    post_lines: list[str] = []
    council_lines: list[str] = []
    section: str | None = None  # which multi-line section we're accumulating

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("FORMAT:"):
            fmt = stripped.split(":", 1)[1].strip()
            section = None
            continue
        if stripped.startswith("SITUATION:"):
            situation = stripped.split(":", 1)[1].strip()
            section = None
            continue
        if stripped.startswith("POST:"):
            # A 'POST:' header may carry inline text after the colon; keep it.
            section = "post"
            inline = stripped.split(":", 1)[1].strip()
            if inline:
                post_lines.append(inline)
            continue
        if stripped.startswith("COUNCIL:"):
            section = "council"
            inline = stripped.split(":", 1)[1].strip()
            if inline:
                council_lines.append(inline)
            continue
        # Accumulate body lines into the active section (preserve original line,
        # not the stripped one, so post formatting/indentation survives).
        if section == "post":
            post_lines.append(line)
        elif section == "council":
            council_lines.append(line)

    post_text = "\n".join(post_lines).strip()
    council_block = "\n".join(council_lines).strip()
    hashtags = _HASHTAG_RE.findall(post_text)
    return ComposedPost(
        format=fmt,
        situation=situation,
        post_text=post_text,
        council_block=council_block,
        hashtags=hashtags,
        raw=raw,
    )


class Composer:
    """Runs the compose step and returns a structured, de-named result.

    The :class:`Voices` transport and the :class:`RecentFormatStore` are injected
    so unit tests MOCK the composing voice and use a temp state file — no real
    model, no real filesystem coupling. On a successful compose whose chosen
    format is a known :data:`FORMATS` key, the store is updated so the NEXT run
    avoids repeating it (the format-variety loop).
    """

    def __init__(
        self,
        voices: Voices | None = None,
        recent_store: RecentFormatStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Resolve the voice transport + recent-format store (both config-driven)."""
        self._settings = settings or get_settings()
        self._voices = voices or Voices(self._settings)
        self._recent = recent_store or RecentFormatStore.from_settings(self._settings)

    def _build_prompt(self, delib: Deliberation) -> str:
        """Assemble the compose prompt (VERBATIM from the prototype).

        The ``avoid`` list (recent formats) and the filtered ``menu`` come from the
        injected store so variety is honoured, but the INSTRUCTION WORDING is the
        proven, owner-approved text — unchanged.
        """
        avoid = self._recent.recent()
        menu = self._recent.menu_avoiding_recent()
        # Transcript block: both rounds per voice, in canonical order.
        transcript = "\n\n".join(
            f"{voice} (round 1): {delib.round1[voice]}\n{voice} (round 2): {delib.round2[voice]}"
            for voice in VOICE_ORDER
        )
        # --- The compose prompt is VERBATIM from scripts/council.py compose() ---
        return (
            "You are the editor of the BRAHMASTRA THOUGHT COMMUNITY — a council of minds "
            f"that thinks out loud in public. You ghost-write for {VOICE_PROFILE}\n\n"
            f"Topic: {delib.topic}\n\nHere is the real deliberation among the council:\n"
            f"{transcript}\n\n"
            "HARD RULES:\n"
            "- NEVER name the individual AIs or any model (no 'Gemini', 'Codex', 'Claude', "
            "'GPT', 'the model', etc.). Refer to them ONLY as 'the council', 'one voice', "
            "'another', 'a third', 'some argued'. The ONLY attribution anywhere is the final "
            "line 'Powered by Brahmastra'.\n"
            "- Match TONE to the topic: provocative and searching for weighty topics; warm, "
            "playful, curious, even funny for lighter ones. This is a thought COMMUNITY, not "
            "a debate club — sometimes it just muses or laughs.\n"
            "- RELEVANCE VARIETY (important): do NOT default to the author's job or "
            "credentials. NEVER open with or lean on 'I run a hospital' / operator-flexing — "
            "it is repetitive and cringe. Vary how the post connects run-to-run, and OFTEN "
            "let the IDEA carry it with NO personal anchor at all. A personal detail is earned "
            "only when it genuinely sharpens the point, never a reflex. For THIS post, lean "
            f"toward this way in: {random.choice(_LENSES)} — but the idea always comes first.\n\n"
            "TASK:\n"
            "1. HONESTY GATE: judge whether the council genuinely DISAGREED, AGREED, or one "
            "voice SHIFTED. Never manufacture a fight that didn't happen.\n"
            f"2. Pick the ONE format that most honestly fits (avoid recently-used: {avoid}):\n"
            + "\n".join(f"   - {k}: {v}" for k, v in menu.items())
            + "\n3. Write a LinkedIn post (700-1600 chars) in the owner's first-person voice. "
            "Make people feel something — think, smile, or reconsider. 3-5 hashtags. Use NO "
            "AI names.\n"
            "4. Write a 'Council' block: exactly 3 short bullet lines capturing the distinct "
            "viewpoints — NO names, just the positions. Then a final standalone line: "
            "'Powered by Brahmastra'.\n\n"
            "OUTPUT EXACTLY in this shape:\n"
            "FORMAT: <chosen_format_name>\n"
            "SITUATION: <disagreed|agreed|shifted> — <one line why>\n"
            "POST:\n<the post>\n"
            "COUNCIL:\n• <viewpoint 1>\n• <viewpoint 2>\n• <viewpoint 3>\n"
            "Powered by Brahmastra"
        )

    def compose(self, delib: Deliberation) -> ComposedPost:
        """Compose the de-named post from ``delib`` and return the parsed result.

        Runs the editorial voice, parses the fixed-shape output, and — if the
        chosen format is a known :data:`FORMATS` key — records it so the next run
        avoids repeating it. De-naming is FAIL-CLOSED: if a forbidden model/vendor
        name (or 'the model') leaks into the post OR council block despite the HARD
        RULES, this raises :class:`ForbiddenNameError` and NEVER returns a leaking
        post — the #1 published-text rule wins over shipping. Composing is also
        fail-closed on empty output.

        Raises:
            RuntimeError: when the composing voice returns nothing usable — there
                is no post to publish.
            ForbiddenNameError: when the composed post or council block names any
                AI/model/vendor — the leak aborts the compose (never published).
        """
        prompt = self._build_prompt(delib)
        logger.info("Council composing (editorial voice).")
        raw = self._voices.ask(CLAUDE, prompt)
        if not raw.strip():
            raise RuntimeError("Council compose produced no output; nothing to publish.")

        composed = _parse_composition(raw)

        # Format-variety bookkeeping: only remember a real, known format so a
        # parse miss ('unknown') doesn't pollute the recent history.
        if composed.format in FORMATS:
            self._recent.remember(composed.format)

        # De-naming gate — FAIL CLOSED. A forbidden name in EITHER published
        # surface aborts: we never return a leaking post. Log the offending token
        # (only the token, not the surrounding text) for the alert, then raise.
        leak = find_forbidden_name(composed.post_text) or find_forbidden_name(
            composed.council_block
        )
        if leak is not None:
            logger.error(
                "Composed post/council block names a forbidden AI/model; aborting.",
                extra={"forbidden_match": leak},
            )
            raise ForbiddenNameError(leak)

        return composed
