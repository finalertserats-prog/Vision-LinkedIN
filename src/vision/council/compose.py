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


# The compose model sometimes appends a chat OUTRO offering to edit the post
# ("Want me to tighten it further?", "Shall I make it shorter?"). Strip a trailing
# assistant-offer line so it never publishes. Kept CONSERVATIVE (explicit offer
# phrasings only) so a genuine reader CTA like "What do you think?" survives.
_OUTRO_RE = re.compile(
    r"^(want me to|shall i|should i|would you like me to|i can (also )?"
    r"(tighten|shorten|rewrite|adjust|make|produce|draft)|"
    r"happy to (tighten|adjust|rewrite|shorten|help)|hope this helps|"
    r"let me know if you.?d like me to)\b",
    re.IGNORECASE,
)


def _strip_trailing_outro(post_text: str) -> str:
    """Drop a trailing assistant-offer line ('Want me to tighten it?') from the post."""
    lines = post_text.split("\n")
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if _OUTRO_RE.match(last.lower()):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class ContrastSpec:
    """A two-sided contrast for a contrast-card image (optional, §13.6 + owner req).

    Present ONLY when the post rests on a clear this-vs-that / naive-vs-wise
    metaphor. The two scenes are TEXT-FREE anime prompts (agy draws them); the
    short ALL-CAPS labels are composited crisply over each panel.
    """

    left_label: str  # 1-3 words, the flawed/naive side
    left_scene: str  # text-free anime scene for the left panel
    right_label: str  # 1-3 words, the wiser side
    right_scene: str  # text-free anime scene for the right panel


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
    contrast: ContrastSpec | None = None  # a contrast-card spec, when the post is binary
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


def _parse_contrast(value: str) -> ContrastSpec | None:
    """Parse a 'CONTRAST:' value into a :class:`ContrastSpec`, or None if malformed.

    Expected shape (optional line): ``<LEFT_LABEL> ~ <left scene> || <RIGHT_LABEL>
    ~ <right scene>``. A missing delimiter / empty part -> None (the post is simply
    treated as having no contrast card — never an error).
    """
    if "||" not in value:
        return None
    left_raw, right_raw = value.split("||", 1)

    def _side(side: str) -> tuple[str, str] | None:
        if "~" not in side:
            return None
        label, scene = side.split("~", 1)
        label, scene = _strip_em_dashes(label.strip()), scene.strip()
        return (label, scene) if label and scene else None

    left = _side(left_raw)
    right = _side(right_raw)
    if left is None or right is None:
        return None
    return ContrastSpec(
        left_label=left[0], left_scene=left[1], right_label=right[0], right_scene=right[1]
    )


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
    contrast: ContrastSpec | None = None  # optional contrast-card spec
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
            if key == "contrast":
                # Optional single line; split on the raw value to keep '~' and '||'.
                contrast = _parse_contrast(norm.split(":", 1)[1])
                section = None
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

    post_text = _strip_em_dashes(
        _strip_trailing_outro(_strip_leading_preamble("\n".join(post_lines).strip()))
    )
    council_block = _strip_em_dashes("\n".join(council_lines).strip())
    hashtags = _HASHTAG_RE.findall(post_text)
    return ComposedPost(
        format=fmt,
        situation=situation,
        post_text=post_text,
        council_block=council_block,
        hashtags=hashtags,
        contrast=contrast,
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
            "You are a GHOST-WRITER for the owner's personal LinkedIn. You write in the "
            f"owner's OWN first-person voice as {VOICE_PROFILE}\n\n"
            f"Topic: {delib.topic}\n\n"
            "Below is a PRIVATE deliberation among several minds — it is your RAW MATERIAL "
            "ONLY. Mine it for the sharpest insight, the real tension, and any genuine change "
            "of view, then write the owner's own post. The reader must NEVER see that a "
            "deliberation happened or that more than one mind was involved:\n"
            f"{transcript}\n\n"
            "HARD RULES:\n"
            "- NEVER narrate the machinery. No AI/model names ('Gemini', 'Codex', 'Claude', "
            "'GPT', 'the model'). Do NOT mention a 'council', 'voices', 'one voice', "
            "'another', 'a third', 'some argued', 'the room', a 'debate' or a 'deliberation'. "
            "NEVER write 'I watched a council change its mind' or any variant. The post is "
            "simply the OWNER thinking out loud, and it must START straight from the idea.\n"
            "- If the thinking genuinely SHIFTED, express it as the owner's OWN mind changing "
            "('I used to think X; I don't anymore, and here is why'), NEVER as a group that "
            "shifted or as something the owner 'watched' happen.\n"
            "- Match TONE to the topic: provocative and searching for weighty topics; warm, "
            "playful, curious, even funny for lighter ones. This is a thought COMMUNITY, not "
            "a debate club — sometimes it just muses or laughs.\n"
            "- RELEVANCE VARIETY (important): do NOT default to the author's job or "
            "credentials. NEVER open with or lean on 'I run a hospital' / operator-flexing — "
            "it is repetitive and cringe. Vary how the post connects run-to-run, and OFTEN "
            "let the IDEA carry it with NO personal anchor at all. A personal detail is earned "
            "only when it genuinely sharpens the point, never a reflex. For THIS post, lean "
            f"toward this way in: {random.choice(_LENSES)}, but the idea always comes first.\n"
            "- TECH TOPICS STAY GROUNDED: if the topic is technical (AI, engineering, "
            "product, data, systems), keep it concrete and specific about how things "
            "actually work, get built, or fail — a technologist's real insight connected to "
            "a bigger idea, NOT abstract philosophy dressed up as tech. The owner is a "
            "tech-leaning cross-domain thinker; sound like someone who builds AND thinks "
            "widely, never like an armchair essayist.\n"
            "- NO EM-DASHES (owner rule, important): do NOT use em-dashes (the long dash) "
            "anywhere in the post. Overusing them is a well-known tell that an AI wrote the "
            "text and it reads as inauthentic. Use a plain hyphen, a comma, a colon, "
            "parentheses, or simply two shorter sentences instead. Target ZERO em-dashes.\n\n"
            "TASK:\n"
            "1. HONESTY GATE (private judgement, NOT for the post): judge whether the raw "
            "material genuinely DISAGREED, AGREED, or SHIFTED. Never manufacture a tension "
            "that isn't there. This only shapes the post; it is never referenced in it.\n"
            f"2. Pick the ONE format that best fits (avoid recently-used: {avoid}). A format "
            "is the SHAPE of the owner's own post, never a report on a deliberation:\n"
            + "\n".join(f"   - {k}: {v}" for k, v in menu.items())
            + "\n3. Write a LinkedIn post (700-1600 chars) as the owner's OWN natural "
            "first-person reflection on the idea — open straight from the thought, no "
            "meta-frame. Make people feel something: think, smile, or reconsider. 3-5 "
            "hashtags. No AI names, no council talk.\n"
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
            "Powered by Brahmastra\n"
            "OPTIONAL - only if the post rests on a clear TWO-SIDED contrast (this vs "
            "that, naive vs wise, before vs after), add ONE more line for a two-panel "
            "comparison image. LEFT = the flawed/naive side, RIGHT = the wiser side. "
            "Each side is a 1-3 word ALL-CAPS label plus a TEXT-FREE visual scene "
            "(describe an image with NO words in it). If the post is not a clear "
            "binary, OMIT this line entirely:\n"
            "CONTRAST: <LEFT_LABEL> ~ <left scene, text-free> || <RIGHT_LABEL> ~ "
            "<right scene, text-free>"
        )

    def _build_problem_prompt(self, delib: Deliberation, problem: str) -> str:
        """Assemble the compose prompt for a real-problem 'overcome story'.

        ``problem`` is GROUND TRUTH; the deliberation is private sharpening material.
        Reuses the SAME output contract as :meth:`_build_prompt` so ``_parse_composition``
        and every gate behave identically for both lanes.
        """
        transcript = "\n\n".join(
            f"{voice} (round 1): {delib.round1[voice]}\n{voice} (round 2): {delib.round2[voice]}"
            for voice in VOICE_ORDER
        )
        return (
            "You are a GHOST-WRITER for the owner's personal LinkedIn. You write in the "
            f"owner's OWN natural first-person voice as {VOICE_PROFILE}\n\n"
            "This is the 'problems and how we overcame them' series. Below is a REAL "
            "problem the owner actually faced and worked through. Treat EVERY detail as "
            "GROUND TRUTH: never invent, exaggerate, or contradict it. If a detail is "
            "not given, keep it general rather than making it up:\n\n"
            f"{problem}\n\n"
            "Below is a PRIVATE deliberation that sharpened the angle and lesson — RAW "
            "MATERIAL only, never shown or referenced:\n"
            f"{transcript}\n\n"
            "HARD RULES:\n"
            "- Tell the STORY: the problem, what was tried, the turn where it got "
            "solved, and the earned lesson. Concrete and specific to what actually "
            "happened - a builder sharing a real war story, not an abstract essay.\n"
            "- NEVER narrate the machinery. No AI/model names; do NOT mention a "
            "'council', 'voices', a 'debate' or a 'deliberation'. It is the OWNER "
            "recounting their own experience, and it must START straight from the story.\n"
            "- Keep any tech CONCRETE: how it actually broke, worked, or got fixed - "
            "never vague philosophy.\n"
            "- NO EM-DASHES (owner rule): use a plain hyphen, comma, colon, or two "
            "shorter sentences instead. Target ZERO em-dashes.\n\n"
            "TASK:\n"
            "1. HONESTY GATE (private, NOT for the post): note whether this was a clean "
            "win, a partial fix, or mostly a lesson learned. It shapes the post only.\n"
            "2. Write a LinkedIn post (700-1600 chars) as the owner's OWN natural "
            "first-person account of the problem and how it was overcome, ending on the "
            "earned lesson. Make it useful to someone facing something similar. 3-5 "
            "hashtags. No AI names, no machinery talk.\n\n"
            "OUTPUT FORMAT (strict): PLAIN TEXT only. Do NOT use Markdown - no **bold**, "
            "no ## headings, no '---' rules, no backticks. Do NOT write any preamble or "
            "explanation. Your reply MUST begin with the literal characters 'FORMAT:' "
            "and include the literal headers FORMAT:, SITUATION:, POST:, and COUNCIL: "
            "each on its own line:\n"
            "FORMAT: problem_solved\n"
            "SITUATION: <clean win|partial fix|lesson> - <one line why>\n"
            "POST:\n<the story>\n"
            "COUNCIL:\n- <angle 1>\n- <angle 2>\n- <angle 3>\n"
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
        return self._compose_from_prompt(self._build_prompt(delib))

    def compose_problem(self, delib: Deliberation, problem: str) -> ComposedPost:
        """Compose the 'problem & how we overcame it' post, grounded in ``problem``.

        Same parsing + fail-closed gates as :meth:`compose`, but the prompt tells the
        story of a REAL problem the owner faced (``problem`` is GROUND TRUTH — never
        invented or contradicted); the deliberation only sharpens the angle + lesson.
        """
        return self._compose_from_prompt(self._build_problem_prompt(delib, problem))

    def _compose_from_prompt(self, prompt: str) -> ComposedPost:
        """Run the compose voice on ``prompt`` with retries + fail-closed gates.

        Shared by :meth:`compose` and :meth:`compose_problem` so BOTH content lanes
        get identical de-naming / em-dash / parse-miss guarantees (§13.0).
        """
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

            # The contrast labels/scenes get RENDERED INTO the published image, so
            # they need the same de-naming guard (Codex review — publish-safety). A
            # leak here DROPS the optional card (keep the good post, lose the risky
            # image) rather than failing the whole compose.
            if composed.contrast is not None:
                c = composed.contrast
                card_leak = (
                    find_forbidden_name(c.left_label)
                    or find_forbidden_name(c.left_scene)
                    or find_forbidden_name(c.right_label)
                    or find_forbidden_name(c.right_scene)
                )
                if card_leak is not None:
                    logger.warning(
                        "Contrast card names a forbidden AI/model; dropping the card.",
                        extra={"forbidden_match": card_leak},
                    )
                    composed.contrast = None

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
