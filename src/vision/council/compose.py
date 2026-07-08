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

# Owner rule (2026-07-08): em-dashes are a well-known tell that AI wrote the text.
# Map em-dash and en-dash to a plain hyphen so published posts read as human-authored.
# A spaced em-dash (" — ") becomes " - "; a tight one ("a—b") becomes "a-b".
_EM_DASHES = str.maketrans({"—": "-", "–": "-"})


def _strip_em_dashes(text: str) -> str:
    """Replace em/en dashes with a plain hyphen (owner authenticity rule)."""
    return text.translate(_EM_DASHES)


# The compose model non-deterministically prettifies output with Markdown (bold
# headers like '**Format:**', a '---' rule, backtick-wrapped values) instead of
# the plain FORMAT:/POST:/COUNCIL: markers the prompt asks for. The parser below
# normalizes this so a well-written post is never dropped for arriving dressed in
# Markdown (see _parse_composition).
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")  # a Markdown horizontal rule
_SIGNATURE_LINE = "powered by brahmastra"
_FORMAT_KEYS = frozenset({"format", "format chosen"})
_SITUATION_KEYS = frozenset({"situation", "honesty gate"})

# The compose prompt asks for a 700-1600 char post. A parsed body shorter than
# this floor is not a real post but a sentinel/preamble/refusal — treated as a
# parse miss so the Markdown-salvage path can never publish a short non-post.
_MIN_POST_CHARS = 200


def _demarkdown(line: str) -> str:
    """Strip Markdown header dressing (**bold**, ##, >, `code`) from ``line``."""
    return line.strip().lstrip("#>").strip().strip("*_` ").strip()


# The compose model sometimes prefixes the body with a chat preamble ("Here is
# the post.", "Sure, here you go:") despite the prompt forbidding it. Strip a
# LEADING preamble line so it never reaches the published post — but only when it
# clearly reads as meta (mentions 'post', ends with a colon, or is very short),
# so a genuine opener like "Here is what I learned..." is never eaten.
_PREAMBLE_RE = re.compile(
    r"^(here'?s|here is|here you go|sure|okay|ok|certainly|absolutely)\b", re.IGNORECASE
)


def _strip_leading_preamble(post_text: str) -> str:
    """Drop a leading chat-preamble line ('Here is the post.') from ``post_text``."""
    lines = post_text.split("\n")
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        low = first.lower()
        looks_meta = "post" in low or first.endswith(":") or len(first) <= 24
        if _PREAMBLE_RE.match(low) and looks_meta:
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


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
        # Detect headers on a Markdown-normalized copy, but accumulate the ORIGINAL
        # line so the post's own formatting/indentation survives.
        norm = _demarkdown(line)
        low = norm.lower()

        if _HR_RE.match(line):
            # A horizontal rule usually divides metadata from the body when the
            # model drops an explicit 'POST:' marker — enter the post there.
            if section is None:
                section = "post"
            continue

        # Colon headers (plain or Markdown-dressed): FORMAT/SITUATION/POST/COUNCIL
        # plus the model's habitual aliases 'Format chosen'/'Honesty gate'.
        if ":" in norm:
            key = low.split(":", 1)[0].strip()
            value = _demarkdown(norm.split(":", 1)[1])
            if key in _FORMAT_KEYS:
                if value:
                    fmt = value
                section = None
                continue
            if key in _SITUATION_KEYS:
                situation = value
                section = None
                continue
            if key == "post":
                section = "post"
                if value:
                    post_lines.append(value)
                continue
            if key == "council":
                section = "council"
                if value:
                    council_lines.append(value)
                continue
        # Bare heading lines with no colon (e.g. a Markdown '## POST').
        elif low in {"post", "council"}:
            section = "post" if low == "post" else "council"
            continue

        if low == _SIGNATURE_LINE:
            continue

        # Untagged prose: Format-B output has no 'POST:' marker, so the first real
        # body line starts the post (header lines above already 'continue'd).
        if section is None and norm:
            section = "post"
        if section == "post":
            post_lines.append(line)
        elif section == "council":
            council_lines.append(line)

    post_text = _strip_em_dashes(_strip_leading_preamble("\n".join(post_lines).strip()))
    council_block = _strip_em_dashes("\n".join(council_lines).strip())
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
            f"toward this way in: {random.choice(_LENSES)}, but the idea always comes first.\n"
            "- NO EM-DASHES (owner rule, important): do NOT use em-dashes (the long dash) "
            "anywhere in the post. Overusing them is a well-known tell that an AI wrote the "
            "text and it reads as inauthentic. Use a plain hyphen, a comma, a colon, "
            "parentheses, or simply two shorter sentences instead. Target ZERO em-dashes.\n\n"
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
            "OUTPUT FORMAT (strict): PLAIN TEXT only. Do NOT use Markdown - no **bold**, "
            "no ## headings, no '---' rules, no backticks. Do NOT write any preamble, "
            "reasoning, or explanation. Your reply MUST begin with the literal characters "
            "'FORMAT:' and include the literal headers FORMAT:, SITUATION:, POST:, and "
            "COUNCIL: each on its own line - ALWAYS, for EVERY format including "
            "quiet_observation (the post body goes under the POST: header even when it "
            "reads as a plain reflection):\n"
            "FORMAT: <chosen_format_name>\n"
            "SITUATION: <disagreed|agreed|shifted> - <one line why>\n"
            "POST:\n<the post>\n"
            "COUNCIL:\n- <viewpoint 1>\n- <viewpoint 2>\n- <viewpoint 3>\n"
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
        last_error = "no attempts made"
        # Up to 3 attempts: the composing voice is non-deterministic, so a one-off
        # parse miss (the editor dropped the POST:/COUNCIL: markers — seen on the
        # 'quiet_observation' format) is usually cured by a re-ask. A LEAK, by
        # contrast, is a hard fail-closed and is NEVER retried.
        for attempt in range(1, 4):
            logger.info("Council composing (editorial voice).", extra={"attempt": attempt})
            raw = self._voices.ask(CLAUDE, prompt)
            if not raw.strip():
                last_error = "empty output"
                continue

            composed = _parse_composition(raw)

            # De-naming gate FIRST — FAIL CLOSED, no retry. A forbidden name in
            # EITHER published surface aborts immediately: we never return (or retry
            # away) a leaking post — the #1 rule wins over shipping.
            leak = find_forbidden_name(composed.post_text) or find_forbidden_name(
                composed.council_block
            )
            if leak is not None:
                logger.error(
                    "Composed post/council block names a forbidden AI/model; aborting.",
                    extra={"forbidden_match": leak},
                )
                raise ForbiddenNameError(leak)

            # Parse-miss guard — a missing/tiny POST body means the editor didn't emit
            # a usable post; retry rather than return an empty draft (silent failure).
            # The prompt asks for a 700-1600 char post, so a body under _MIN_POST_CHARS
            # is not a real post but a sentinel/preamble/refusal — this also stops the
            # Markdown-salvage path from ever grabbing a short non-post as the body.
            # The COUNCIL block is now OPTIONAL: the public post no longer includes it
            # (owner removed it), so a dropped Council section is NOT a failure — we
            # just proceed without it (the email loses that context, but the post is
            # fine). Only a missing/too-short POST body is a real parse miss.
            if len(composed.post_text.strip()) < _MIN_POST_CHARS:
                last_error = "parse miss: empty or too-short post body"
                # Log a bounded preview of what the editor actually returned so a
                # parse miss is diagnosable instead of silent (the editor usually
                # dropped the 'POST:' marker or answered conversationally).
                logger.warning(
                    "Council compose parse miss; retrying.",
                    extra={
                        "attempt": attempt,
                        "raw_len": len(raw),
                        "raw_preview": raw.strip()[:400],
                    },
                )
                continue
            if not composed.council_block.strip():
                logger.info("Council block empty (optional) — proceeding without it.")

            # Success: record the format (only real, known formats) and return.
            if composed.format in FORMATS:
                self._recent.remember(composed.format)
            return composed

        # All attempts exhausted without a usable post → fail closed, never publish
        # an empty draft.
        raise RuntimeError(
            f"Council compose produced no usable post after 3 attempts ({last_error})."
        )
