"""The COUNCIL image lane — decide + attach a visual to a council draft (BRD §13.6).

WHY this module exists: the council produces IDEAS and OPINIONS (rarely numbers),
so its visual policy differs from the news lane's number-driven card path. This
module owns the council-specific IMAGE DECISION that runs AFTER compose and
returns exactly one of:

  * ``none``                  — text-only (the safe default, most posts);
  * ``quote_card``            — a DETERMINISTIC Pillow card of the post's strong
                                one-line punchline (``render_quote_card``);
  * ``concept_illustration``  — a TEXT-FREE agy illustration for a more
                                atmospheric post (via ``BrahmastraImageClient``).

PRECISION RULE (BRD §13.6 / D10): anything with WORDS or NUMBERS is rendered
DETERMINISTICALLY — the quote card lays the punchline out pixel-precisely with
Pillow; agy is used ONLY for text-free concept art and its prompt HARD-DEMANDS
"no text, no words, no letters, no logos" so it can never bake copy into the
image. A quote is words, so it is ALWAYS a card, never diffusion.

POLICY IS CONFIG, NOT CODE (§22.6): whether the lane runs
(``COUNCIL_IMAGE_ENABLED`` + the global ``IMAGE_ENABLED`` kill-switch), HOW OFTEN
(``COUNCIL_IMAGE_EVERY_N`` rotation — the council is NOT image-heavy), the weekly
ceiling (``IMAGE_MAX_PER_WEEK``), and WHERE PNGs land (``COUNCIL_IMAGE_DIR``) are
all owner-editable knobs. A small JSON ledger (``COUNCIL_IMAGE_STATE_PATH``)
persists the rotation counter + a rolling 7-day window of generation timestamps
so the cap survives process restarts.

FAIL-CLOSED / DEGRADE-GRACEFULLY (§13.6 guardrail, §22.9): the image is a
best-effort enhancement — ANY failure (render blow-up, agy hiccup, disk error)
degrades the draft to text-only and NEVER blocks publishing. The lane raises
nothing the caller must handle; it mutates the draft's ``image_*`` fields in
place and leaves them at ``none`` when it cannot produce a real image.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from vision.brahmastra.errors import ImageGenerationError
from vision.brahmastra.image_client import BrahmastraImageClient
from vision.config import Settings, get_settings
from vision.council.compose import ContrastSpec
from vision.visuals.card_renderer import render_contrast_card, render_quote_card

logger = logging.getLogger(__name__)

# --- Image-type sentinels (the council's own vocabulary) --------------------
# WHY plain strings (not the news lane's ``ImageType`` enum): the task fixes the
# council draft's ``image_type`` values as 'none' | 'quote_card' |
# 'concept_illustration', which are the council's editorial outcomes — distinct
# from the synthesis lane's 'informative-card' / 'concept-illustration'. Named
# constants keep the literals in one auditable place (§22 naming).
IMAGE_TYPE_NONE = "none"
IMAGE_TYPE_QUOTE_CARD = "quote_card"
IMAGE_TYPE_CONCEPT = "concept_illustration"
IMAGE_TYPE_CONTRAST = "contrast_card"

# Provenance stamped on a deterministically-rendered card (mirrors the news lane's
# ``image_source = 'deterministic'`` so downstream provenance reads consistently).
_SOURCE_DETERMINISTIC = "deterministic"

# The window over which ``IMAGE_MAX_PER_WEEK`` is enforced. A rolling 7 days (not
# a calendar week) so the cap can never be gamed by a Sunday/Monday boundary.
_WEEK = timedelta(days=7)

# Ledger JSON keys. Named so a hand-inspected state file is self-describing and a
# key rename can't silently drift between read and write.
_LEDGER_KEY_COUNTER = "rotation_counter"
_LEDGER_KEY_STAMPS = "generated_at"

# --- Punchline heuristic geometry ------------------------------------------
# A "strong one-line punchline" is a SHORT, declarative first line with NO
# numbers (numbers → precision rule forbids putting them on a diffusion image and
# also reads poorly on a pull-quote). These bounds keep a quote card legible: too
# short is a fragment, too long overflows the card's auto-fit into tiny text.
_PUNCHLINE_MIN_CHARS = 12
_PUNCHLINE_MAX_CHARS = 120
# A hashtag/mention line is not a punchline; a digit run means numbers are
# present (deterministic-card-only territory, and not quotable prose).
_DIGIT_RE = re.compile(r"\d")
_HASHTAG_OR_MENTION_RE = re.compile(r"[#@]\w")

# The text-free concept-illustration prompt template. WHY the negatives live here
# too (the image client also appends them): the council prompt must be SELF-
# EVIDENTLY safe when logged/inspected, and belt-and-braces guarantees the
# precision rule (§13.6/D10) regardless of client internals. The atmospheric
# concept is derived from the post's opening so the art stays on-theme.
_CONCEPT_PROMPT_TEMPLATE = (
    "An abstract, atmospheric concept illustration evoking the mood of this "
    "reflection: {concept}. Muted, professional, contemplative. "
    "Strictly no text, no words, no letters, no numbers, no logos."
)
# Cap on how much of the post seeds the concept prompt — enough for theme, not so
# much that the prompt turns into an essay agy would ignore.
_CONCEPT_SEED_CHARS = 240


class _QuoteCardRenderer(Protocol):
    """The ``render_quote_card`` call shape the lane depends on (injected/mocked).

    Declaring the seam as a Protocol lets unit tests pass a tiny stub without
    importing the heavy Pillow/matplotlib renderer, and documents the exact
    contract: a quote in, PNG bytes out (keyword args are the renderer's brand
    options, which the lane leaves at their defaults).
    """

    def __call__(self, quote: str, **kwargs: Any) -> bytes: ...  # pragma: no cover


class _ImageClient(Protocol):
    """The subset of ``BrahmastraImageClient`` the lane uses (injected/mocked).

    Only ``illustrate`` is needed. A Protocol (not the concrete class) is the
    injection seam so tests supply a fake that never launches agy (§18/§22).
    """

    def illustrate(self, prompt: str, model: str | None = None) -> bytes: ...  # pragma: no cover


@dataclass(frozen=True)
class CouncilImageChoice:
    """The council image DECISION — an inert value object the caller acts on.

    ``image_type`` is one of ``IMAGE_TYPE_NONE`` / ``IMAGE_TYPE_QUOTE_CARD`` /
    ``IMAGE_TYPE_CONCEPT``. Exactly one of ``quote_line`` (for a quote card) or
    ``illustration_prompt`` (for a concept illustration) is populated for a
    non-none choice; both are ``None`` for ``none``. Frozen so a decision is a
    stable artefact that cannot be mutated after the fact.
    """

    image_type: str
    quote_line: str | None = None
    illustration_prompt: str | None = None
    # Populated only for IMAGE_TYPE_CONTRAST — the two labels + text-free scenes.
    contrast: "ContrastSpec | None" = None

    @classmethod
    def none(cls) -> "CouncilImageChoice":
        """Return the explicit 'skip' choice (text-only) — the safe default."""
        return cls(image_type=IMAGE_TYPE_NONE)


@dataclass
class _CouncilImageLedger:
    """Durable rotation counter + rolling weekly-cap window (JSON state file).

    Mirrors the fail-SOFT persistence pattern of
    :class:`~vision.council.formats.RecentFormatStore`: a missing/corrupt file
    reads as an empty ledger, and a write failure is logged (class only) and
    swallowed — the council's *content* must never crash on its own image
    bookkeeping. At worst a lost write lets one extra image through, never a
    blocked post.
    """

    #: Where the ledger persists (already expanduser'd).
    path: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> "_CouncilImageLedger":
        """Build a ledger from ``COUNCIL_IMAGE_STATE_PATH`` (expanduser'd)."""
        path = Path(os.path.expanduser(settings.council_image_state_path))
        return cls(path=path)

    def _read(self) -> dict[str, Any]:
        """Return the raw ledger dict, or an empty one on any read problem.

        Fail-soft: a first-run (missing) file, unreadable file, non-JSON, or a
        non-dict shape all read as ``{}`` so the lane never crashes on its own
        memory (§13.6). Specific exceptions only — no bare except (§22).
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("Council image ledger is not valid JSON; ignoring it.")
            return {}
        if not isinstance(data, dict):
            logger.warning("Council image ledger is not an object; ignoring it.")
            return {}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        """Persist ``data`` (creating parent dirs), swallowing write failures.

        A failure to stamp the ledger must not crash the council — it is logged
        (class only, never the path contents) and swallowed, matching the
        RecentFormatStore contract.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Council could not persist image ledger (%s); cap/rotation not stamped.",
                exc.__class__.__name__,
            )

    def next_rotation(self) -> int:
        """Advance and return the monotonic rotation counter (post index).

        WHY advance on DECISION (not on attach): the rotation must count EVERY
        eligible council post so 'every Nth' is honoured whether or not an image
        was ultimately produced — otherwise a run of failures would never rotate
        past a skipped slot.
        """
        data = self._read()
        counter = data.get(_LEDGER_KEY_COUNTER, 0)
        # Defensive: a corrupt non-int counter restarts from 0 rather than crash.
        counter = counter + 1 if isinstance(counter, int) else 1
        data[_LEDGER_KEY_COUNTER] = counter
        self._write(data)
        return counter

    def _recent_stamps(self, data: dict[str, Any], *, now: datetime) -> list[str]:
        """Return the ISO timestamps within the rolling 7-day window.

        Older stamps are pruned so the ledger cannot grow unbounded and the cap
        always reflects the last week only. Unparseable stamps are dropped
        (defensive) rather than trusted or crashing.
        """
        cutoff = now - _WEEK
        stamps_raw = data.get(_LEDGER_KEY_STAMPS, [])
        if not isinstance(stamps_raw, list):
            return []
        kept: list[str] = []
        for stamp in stamps_raw:
            if not isinstance(stamp, str):
                continue
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when >= cutoff:
                kept.append(stamp)
        return kept

    def images_this_week(self, *, now: datetime | None = None) -> int:
        """Return how many images were generated in the rolling last 7 days."""
        now = now or datetime.now(timezone.utc)
        return len(self._recent_stamps(self._read(), now=now))

    def record_generation(self, *, now: datetime | None = None) -> None:
        """Stamp one successful image generation into the rolling window.

        Prunes expired stamps as a side effect (bounded storage), then appends
        ``now`` and persists. Called ONLY after a real PNG has been written, so
        the weekly cap counts actual images, not attempts.
        """
        now = now or datetime.now(timezone.utc)
        data = self._read()
        kept = self._recent_stamps(data, now=now)
        kept.append(now.isoformat())
        data[_LEDGER_KEY_STAMPS] = kept
        self._write(data)


def _first_line(post_text: str) -> str:
    """Return the first non-blank line of ``post_text`` (stripped), or ''."""
    for line in post_text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _is_strong_punchline(line: str) -> bool:
    """True when ``line`` is a short, quotable, number-free declarative punchline.

    The heuristic (deterministic + auditable, §22.5): within the legible length
    bounds, carrying NO digits (numbers belong on a deterministic card, never a
    pull-quote or diffusion image — §13.6), and not merely a hashtag/mention
    line. Kept a pure predicate so the decision reads as intent.
    """
    if not (_PUNCHLINE_MIN_CHARS <= len(line) <= _PUNCHLINE_MAX_CHARS):
        return False
    if _DIGIT_RE.search(line):
        return False
    if _HASHTAG_OR_MENTION_RE.search(line):
        return False
    return True


def _concept_prompt_from(post_text: str) -> str:
    """Build the text-free concept-illustration prompt from the post's theme.

    Seeds the template with a trimmed slice of the post so the art stays on-theme
    without handing agy an essay. The mandatory "no text/words/letters/logos"
    negatives are baked into the template (belt-and-braces with the image client)
    so the precision rule (§13.6/D10) holds even when this prompt is logged.
    """
    seed = " ".join(post_text.split())[:_CONCEPT_SEED_CHARS].strip()
    return _CONCEPT_PROMPT_TEMPLATE.format(concept=seed)


def decide_council_image(
    post_text: str,
    *,
    contrast: ContrastSpec | None = None,
    settings: Settings | None = None,
) -> CouncilImageChoice:
    """Decide the council draft's image outcome AFTER compose (BRD §13.6).

    Policy (all config-driven, §22.6):

      1. The lane must be enabled — BOTH the council switch
         (``COUNCIL_IMAGE_ENABLED``) and the global kill-switch
         (``IMAGE_ENABLED``); either off ⇒ ``none``.
      2. Rotation — only every ``COUNCIL_IMAGE_EVERY_N``-th eligible post gets an
         image, so the council is NOT image-heavy. The rotation counter advances
         on every decision (even skipped ones) via the persistent ledger.
      3. Content heuristic — a strong one-line punchline ⇒ ``quote_card`` (that
         line, rendered DETERMINISTICALLY); otherwise an atmospheric post ⇒
         ``concept_illustration`` (a text-free agy prompt).

    This function is PURE w.r.t. image generation — it only reads config + the
    rotation ledger and returns a :class:`CouncilImageChoice`. Generation happens
    in :func:`attach_council_image`.

    Args:
        post_text: The composed, de-named council post body.
        settings: Config override (defaults to the process singleton).

    Returns:
        A :class:`CouncilImageChoice`; ``none`` whenever the lane is disabled,
        the rotation skips this post, or there is no usable text to base a visual
        on.
    """
    settings = settings or get_settings()

    # Rule 1: both switches must be on. The global IMAGE_ENABLED is the master
    # kill-switch shared with the news lane; the council switch scopes it further.
    if not settings.image_enabled or not settings.council_image_enabled:
        return CouncilImageChoice.none()

    body = post_text.strip()
    if not body:
        # Nothing to base a visual on — text-only (defensive; compose fails closed
        # on empty, but the lane must never assume that upstream).
        return CouncilImageChoice.none()

    # Rule 2: rotation. Advance the persistent counter and only proceed on the
    # boundary. ``max(1, ...)`` clamps a fat-fingered 0/negative to "every post"
    # rather than a divide-by-zero.
    every_n = max(1, settings.council_image_every_n)
    ledger = _CouncilImageLedger.from_settings(settings)
    counter = ledger.next_rotation()
    if counter % every_n != 0:
        logger.debug(
            "Council image rotation skip (post %d of every %d).", counter, every_n
        )
        return CouncilImageChoice.none()

    # Rule 3: content heuristic.
    # A genuine two-sided contrast → the anime contrast card (owner favourite).
    if contrast is not None:
        return CouncilImageChoice(image_type=IMAGE_TYPE_CONTRAST, contrast=contrast)
    # A strong punchline → deterministic quote card; otherwise a concept illustration.
    line = _first_line(body)
    if _is_strong_punchline(line):
        return CouncilImageChoice(image_type=IMAGE_TYPE_QUOTE_CARD, quote_line=line)

    return CouncilImageChoice(
        image_type=IMAGE_TYPE_CONCEPT,
        illustration_prompt=_concept_prompt_from(body),
    )


def _set_none(draft: dict[str, Any]) -> None:
    """Stamp the draft's image fields as text-only (the degrade target).

    Centralised so every degradation path (disabled, capped, render/gen failure)
    leaves the SAME clean text-only shape on the draft dict. Immutability note:
    we mutate the caller-owned draft dict in place BY DESIGN — the attach step's
    whole job is to populate the draft's ``image_*`` fields for the mailer +
    publisher (mirrors the news lane's ``_render_image``).
    """
    draft["image_type"] = IMAGE_TYPE_NONE
    draft["image_path"] = None
    draft["image_source"] = None
    draft["image_prompt"] = None


def _images_dir(settings: Settings) -> Path:
    """Resolve the council images directory (config over code, expanduser'd).

    Falls back to a temp-dir path when unset so a bare checkout works; prod points
    ``COUNCIL_IMAGE_DIR`` at a durable volume the publisher can read.
    """
    configured = settings.council_image_dir.strip()
    if configured:
        return Path(os.path.expanduser(configured))
    from tempfile import gettempdir

    return Path(gettempdir()) / "vision" / "council-images"


def attach_council_image(
    draft: dict[str, Any],
    *,
    settings: Settings | None = None,
    render_quote_card: _QuoteCardRenderer | None = None,
    image_client: _ImageClient | None = None,
    now: datetime | None = None,
) -> CouncilImageChoice:
    """Decide, generate, and ATTACH a council image to ``draft`` (BRD §13.6).

    The full lane in one call: run :func:`decide_council_image`, enforce the
    weekly cap (``IMAGE_MAX_PER_WEEK``), generate the chosen image (a
    DETERMINISTIC quote card via ``render_quote_card`` OR a text-free agy concept
    illustration via ``image_client``), write the PNG under
    ``COUNCIL_IMAGE_DIR``, and set the draft's ``image_type`` / ``image_path`` /
    ``image_source`` / ``image_prompt`` so the mailer + publisher pick it up.

    DEGRADE-GRACEFULLY (§13.6): ANY failure — a skipped rotation, the weekly cap,
    a render blow-up, an agy hiccup, or a disk error — leaves the draft text-only
    (``image_type == 'none'``) and NEVER raises. The image never blocks the post.

    Args:
        draft: The council draft dict (mutated in place — its ``image_*`` fields
            are the output; that mutation is the function's contract).
        settings: Config override (defaults to the process singleton).
        render_quote_card: The deterministic quote-card renderer (injected so
            tests mock it — no Pillow needed). Defaults to the real
            ``vision.visuals.card_renderer.render_quote_card``.
        image_client: The agy image client (injected so tests mock it — no agy /
            network). Defaults to a fresh ``BrahmastraImageClient``.
        now: Clock override for deterministic weekly-cap tests.

    Returns:
        The :class:`CouncilImageChoice` that was acted on (``none`` when the draft
        ends up text-only) — handy for the caller's manifest/logging.
    """
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)

    post_text = str(draft.get("post_text") or "")
    contrast = draft.get("contrast")
    contrast = contrast if isinstance(contrast, ContrastSpec) else None
    choice = decide_council_image(post_text, contrast=contrast, settings=settings)

    if choice.image_type == IMAGE_TYPE_NONE:
        _set_none(draft)
        return choice

    # Weekly cap: enforced HERE (not in decide) so the rotation counter still
    # advances on a capped post — a capped post is text-only, not a re-roll.
    ledger = _CouncilImageLedger.from_settings(settings)
    cap = settings.image_max_per_week
    if ledger.images_this_week(now=now) >= cap:
        logger.info(
            "Council image weekly cap reached (%d/%d); degrading to text-only.",
            cap,
            cap,
        )
        _set_none(draft)
        return CouncilImageChoice.none()

    # Generate the chosen image into bytes, degrading to text-only on any failure.
    png = _generate(choice, settings, render_quote_card, image_client)
    if png is None:
        _set_none(draft)
        return CouncilImageChoice.none()

    # Write the PNG and stamp the draft. A disk failure here also degrades — the
    # image is best-effort, never a blocker (§13.6).
    out_path = _write_png(png, settings, draft)
    if out_path is None:
        _set_none(draft)
        return CouncilImageChoice.none()

    _stamp_draft(draft, choice, out_path, settings)
    # Only a real, written image counts toward the weekly cap.
    ledger.record_generation(now=now)
    logger.info(
        "Council image attached.",
        extra={"image_type": choice.image_type, "image_path": str(out_path)},
    )
    return choice


def _generate(
    choice: CouncilImageChoice,
    settings: Settings,
    render_quote_card: _QuoteCardRenderer | None,
    image_client: _ImageClient | None,
) -> bytes | None:
    """Produce PNG bytes for ``choice``, or ``None`` on any generation failure.

    Routes a quote card to the DETERMINISTIC renderer (words are always a card,
    never diffusion — §13.6/D10) and a concept illustration to agy. Every failure
    class is caught and turned into ``None`` so the caller degrades to text-only
    (the image never blocks publishing).
    """
    if choice.image_type == IMAGE_TYPE_QUOTE_CARD:
        return _render_quote_card_safe(choice.quote_line or "", settings, render_quote_card)
    if choice.image_type == IMAGE_TYPE_CONCEPT:
        return _illustrate_safe(choice.illustration_prompt or "", settings, image_client)
    if choice.image_type == IMAGE_TYPE_CONTRAST and choice.contrast is not None:
        return _render_contrast_safe(choice.contrast, settings, image_client)
    return None


def _render_contrast_safe(
    contrast: ContrastSpec,
    settings: Settings,
    image_client: _ImageClient | None,
) -> bytes | None:
    """Generate BOTH anime panels + composite a contrast card, or ``None`` on failure.

    Both panels must render (a one-panel comparison is meaningless); ANY agy or
    layout failure degrades to text-only so the image never blocks the post (§13.6).
    """
    client = image_client or BrahmastraImageClient(settings)
    try:
        left = client.illustrate(contrast.left_scene)
        right = client.illustrate(contrast.right_scene)
    except ImageGenerationError as exc:
        logger.warning("Council contrast-card panel failed; degrading to text-only: %s", exc)
        return None
    try:
        return render_contrast_card(
            left, right, contrast.left_label, contrast.right_label, settings=settings
        )
    except (ValueError, OSError) as exc:
        logger.warning("Council contrast-card composite failed; degrading to text-only: %s", exc)
        return None


def _render_quote_card_safe(
    quote_line: str,
    settings: Settings,
    render_quote_card_fn: _QuoteCardRenderer | None,
) -> bytes | None:
    """Render a quote card, catching a blank quote / layout blow-up → ``None``.

    The renderer raises ``ValueError`` on a blank quote or an un-fittable layout;
    we degrade to text-only rather than let a card problem block the post (§13.6).
    The default renderer is the module-level ``render_quote_card`` (imported at
    module scope so it is a single, patchable seam); callers may inject a stub.
    """
    if not quote_line.strip():
        return None
    render = render_quote_card_fn if render_quote_card_fn is not None else render_quote_card
    try:
        return render(quote_line)
    except ValueError as exc:
        # Specific: the renderer's documented failure (blank/overflow). No bare
        # except (§22) — an unexpected error type SHOULD surface in dev.
        logger.warning("Council quote-card render failed; degrading to text-only: %s", exc)
        return None


def _illustrate_safe(
    prompt: str,
    settings: Settings,
    image_client: _ImageClient | None,
) -> bytes | None:
    """Generate a text-free concept illustration, catching failures → ``None``.

    agy failures surface as ``ImageGenerationError`` (the deliberate degrade
    signal); we log and return ``None`` so publishing proceeds text-only (§13.6).
    """
    if not prompt.strip():
        return None
    client = image_client or BrahmastraImageClient(settings)
    try:
        return client.illustrate(prompt)
    except ImageGenerationError as exc:
        logger.warning(
            "Council concept-illustration failed; degrading to text-only: %s", exc
        )
        return None


def _write_png(png: bytes, settings: Settings, draft: dict[str, Any]) -> Path | None:
    """Write ``png`` under the council images dir, or ``None`` on a disk failure.

    The filename is derived from the draft's id when present (stable, collision-
    resistant) else a UTC timestamp, so a rendered image is traceable back to its
    draft. A disk error degrades to text-only (best-effort, §13.6).
    """
    images_dir = _images_dir(settings)
    draft_id = draft.get("id")
    stem = str(draft_id) if draft_id else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    out_path = images_dir / f"council_{stem}.png"
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(png)
    except OSError as exc:
        logger.warning(
            "Council could not write image (%s); degrading to text-only.",
            exc.__class__.__name__,
        )
        return None
    return out_path


def _stamp_draft(
    draft: dict[str, Any],
    choice: CouncilImageChoice,
    out_path: Path,
    settings: Settings,
) -> None:
    """Set the draft's ``image_*`` fields for the mailer + publisher.

    A quote card is DETERMINISTIC provenance with no prompt; a concept
    illustration records the agy model id (``IMAGE_MODEL``) + the text-free prompt
    so the visual is auditable (§13.0). Mutates the caller-owned draft in place —
    that mutation is the attach step's contract.
    """
    draft["image_type"] = choice.image_type
    draft["image_path"] = str(out_path)
    if choice.image_type == IMAGE_TYPE_QUOTE_CARD:
        # A quote card is deterministic (label composited over solid brand colour);
        # a contrast card mixes agy panels with deterministic labels, but its TEXT
        # is deterministic too, so both record as deterministic provenance.
        draft["image_source"] = _SOURCE_DETERMINISTIC
        draft["image_prompt"] = None
    elif choice.image_type == IMAGE_TYPE_CONTRAST and choice.contrast is not None:
        draft["image_source"] = settings.image_model
        draft["image_prompt"] = (
            f"LEFT: {choice.contrast.left_scene} | RIGHT: {choice.contrast.right_scene}"
        )
    else:  # IMAGE_TYPE_CONCEPT
        draft["image_source"] = settings.image_model
        draft["image_prompt"] = choice.illustration_prompt
