"""Deterministic, on-brand card & chart renderer (BRD §13.6, D8/D10 precision-first).

WHY this module exists: informative visuals in VISION must be *exactly right* —
every number legible and traceable — so they are rendered DETERMINISTICALLY
here, never by a diffusion model (BRD §13.6). Two engines, both headless (NO
browser, per D10):

  * **Pillow** draws stat cards and quote cards (pixel-precise text layout).
  * **matplotlib** (Agg backend) draws simple bar/line charts, post-processed
    back through Pillow for the shared brand watermark.

Hard invariants enforced here:
  * Output PNGs are always at the exact LinkedIn dimensions
    (``1200x627`` landscape for stat/quote cards, ``1200x1200`` square for
    charts) — asserted before return.
  * Every rendered number originates from ``CardSpec.datapoints``, and each
    datapoint MUST carry a ``source_item_id`` (provenance). A datapoint without
    one raises ``ValueError`` — the render fails loudly rather than emit an
    ungrounded figure.
  * The brand palette (navy / gold) comes from ``settings.CARD_BRAND_PALETTE``
    (config over code, §22.6), never hard-coded literals in the draw calls.
  * A discreet BRAHMASTRA wordmark watermark is applied when
    ``POST_SIGNATURE_MODE`` is ``card_watermark`` or ``both`` (§15.6, D9).
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from functools import lru_cache

import matplotlib

# Force the non-interactive Agg backend BEFORE importing pyplot: charts are
# rendered headless in a cron job with no display (D10 — no browser, no GUI).
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow the backend selection)
from matplotlib import font_manager  # noqa: E402
from PIL import Image, ImageColor, ImageDraw, ImageFont  # noqa: E402

from vision.config import SignatureMode, Settings, get_settings  # noqa: E402
from vision.visuals.decide import CardSpec  # noqa: E402

logger = logging.getLogger(__name__)

# --- LinkedIn canvas dimensions (px) ---------------------------------------
# WHY named constants: the exact dimensions are a contract the caller/tests
# assert against, and magic tuples in the body would be unreadable (§22 naming).
LINKEDIN_LANDSCAPE: tuple[int, int] = (1200, 627)  # link-preview / stat cards
LINKEDIN_SQUARE: tuple[int, int] = (1200, 1200)  # square charts

# matplotlib exports at figsize(inches) * dpi. 12in * 100dpi = 1200px, giving a
# deterministic 1200x1200 canvas without post-resizing.
_CHART_DPI = 100

# Fallback brand colours used only if the configured palette is unparseable, so
# a malformed CARD_BRAND_PALETTE degrades to on-brand defaults, never a crash.
_FALLBACK_NAVY = "#0B1F3A"
_FALLBACK_GOLD = "#C9A24B"
_WHITE = "#FFFFFF"

# --- Stat-card layout geometry (px) ----------------------------------------
# WHY explicit constants: the vertical budget of the 627px canvas is tight, and
# these values are the contract that keeps title + datapoints + quote + footer
# from overlapping. A card renders DETERMINISTICALLY (§13.6) so the layout must
# be computed, asserted to fit, and never allowed to run off-canvas.
_STAT_MARGIN_X = 80
_STAT_TITLE_Y = 70
_STAT_TITLE_FONT = 56
_STAT_RULE_TOP = 150
_STAT_RULE_BOTTOM = 156
_STAT_DATA_TOP = 200
_STAT_FOOTER_GAP = 20
_STAT_SOURCE_FROM_BOTTOM = 60  # source baseline = height - this
_STAT_SOURCE_FONT = 22
_STAT_QUOTE_FONT = 34
_STAT_QUOTE_MAX_LINES = 2
_STAT_LABEL_LINE_GAP = 4  # gap between a datapoint value and its label
# Hard cap on datapoints: more than this cannot stay legible in the vertical
# budget, so the renderer fails loudly instead of emitting an overlapping card.
_MAX_DATAPOINTS = 3
# Value/label font sizes shrink as the datapoint count grows so all rows fit.
_DATAPOINT_FONTS: dict[int, tuple[int, int]] = {1: (72, 30), 2: (64, 30), 3: (48, 26)}
# Descending value sizes tried when a row must shrink further to fit its slot.
_VALUE_FONT_LADDER: tuple[int, ...] = (72, 64, 56, 48, 40, 34, 28)

# --- Quote-card layout geometry (px) ---------------------------------------
# WHY separate constants from the stat card: a quote is prose, not numbers, so
# the layout is a single centred block that grows/shrinks to fill the canvas
# (no per-datapoint stepping). These govern the margins and the auto-fit search.
_QUOTE_MARGIN_X = 110  # generous side gutters so wrapped prose breathes
_QUOTE_MARGIN_TOP = 150  # room below for the gold accent + opening quote mark
_QUOTE_MARGIN_BOTTOM = 150  # room above the attribution / watermark footer
# Descending body font sizes tried by the auto-fit search: start large for a
# short quote, shrink until the wrapped block fits the vertical budget.
_QUOTE_FONT_LADDER: tuple[int, ...] = (96, 84, 72, 64, 56, 48, 42, 38, 34)
# Absolute floor on wrapped lines: at the smallest font this many lines fill the
# canvas, so beyond it the final line is ellipsised (truncate-within-bounds).
_QUOTE_MAX_LINES = 12
_QUOTE_LINE_SPACING = 1.18  # multiplier on line height for readable leading
_QUOTE_MARK_FONT = 160  # the big decorative opening quotation mark
_QUOTE_ACCENT_RULE_W = 140  # width of the short gold accent rule under the mark
_QUOTE_ACCENT_RULE_H = 6
_QUOTE_ATTRIB_FONT = 34  # attribution line ("— Someone") font size
_QUOTE_ATTRIB_FROM_BOTTOM = 90  # attribution baseline = height - this


def _parse_palette(raw: str) -> dict[str, str]:
    """Parse ``'navy=#0B1F3A;gold=#C9A24B'`` into ``{'navy': '#0B1F3A', ...}``.

    Tolerant of stray whitespace and malformed pairs: a bad pair is skipped
    (logged), and missing keys fall back to the brand defaults. WHY tolerant:
    the palette is owner-editable config (§22.6); a typo should not break the
    daily render, only lose that one custom colour.

    Each parsed VALUE is also validated as a real colour (``ImageColor.getrgb``):
    an unparseable colour (e.g. ``navy=notacolor``) is dropped with a warning so
    the missing key falls back to the on-brand default. WHY validate here: this
    is the single choke point before a colour reaches ``Image.new`` / matplotlib;
    validating downstream would let a bad value crash the render, breaking the
    "malformed palette degrades to defaults, never a crash" guarantee (§13.6).
    """
    palette: dict[str, str] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        # Only split on the FIRST '=' so a value can never be truncated.
        name, sep, value = pair.partition("=")
        if not sep or not value.strip():
            logger.warning("skipping malformed palette pair: %r", pair)
            continue
        value = value.strip()
        if not _is_valid_color(value):
            # Bad colour → drop it so the setdefault below restores the default.
            logger.warning("skipping invalid palette colour %r for %r", value, name.strip())
            continue
        palette[name.strip().lower()] = value

    palette.setdefault("navy", _FALLBACK_NAVY)
    palette.setdefault("gold", _FALLBACK_GOLD)
    return palette


def _is_valid_color(value: str) -> bool:
    """True when ``value`` is a colour Pillow/matplotlib can actually render.

    Uses ``ImageColor.getrgb`` (the same parser ``Image.new`` uses) as the source
    of truth, so any value that passes here is guaranteed drawable downstream.
    """
    try:
        ImageColor.getrgb(value)
    except ValueError:
        return False
    return True


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont:
    """Return a cached TrueType font at ``size`` px.

    Uses matplotlib's bundled DejaVu Sans (guaranteed present since matplotlib is
    a hard dependency), so text rendering is identical across machines — no
    reliance on system fonts. Cached because font loading is comparatively
    expensive and sizes repeat across a render.
    """
    font_path = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans"))
    return ImageFont.truetype(font_path, size)


def _assert_grounded(spec: CardSpec) -> None:
    """Fail loudly unless every datapoint carries a ``source_item_id``.

    Enforces the BRD §13.6 provenance rule at the single choke point before any
    number is drawn. Raising ``ValueError`` (not returning a flag) guarantees a
    caller can never accidentally render an ungrounded figure.
    """
    for index, point in enumerate(spec.datapoints):
        if not point.source_item_id:
            raise ValueError(
                f"datapoint[{index}] ({point.label!r}={point.value!r}) has no "
                "source_item_id; every rendered number must trace to a source (§13.6)"
            )


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    """Return the pixel width of ``text`` in ``font`` (for horizontal centring)."""
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    """Return the full line height (ascent + descent) of ``font`` in px."""
    ascent, descent = font.getmetrics()
    return ascent + descent


def _truncate_line(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    """Trim ``text`` with an ellipsis until it fits ``max_width`` in ``font``.

    Guards the horizontal edge: a single over-long word (or value like a giant
    number) can never run off-canvas because the tail is dropped and marked with
    an ellipsis rather than clipped silently.
    """
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    while text and _text_width(draw, text + ellipsis, font) > max_width:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


def _wrap_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Greedily wrap ``text`` to ``max_width``, capped at ``max_lines``.

    Overflow beyond ``max_lines`` is folded into an ellipsised final line, and
    each line is itself truncated so an unbreakable word still fits. This is what
    keeps long titles/labels/quotes on-canvas (§13.6) instead of overrunning.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or _text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if not lines:
        return [""]
    if len(lines) <= max_lines:
        return [_truncate_line(draw, line, font, max_width) for line in lines]

    kept = [_truncate_line(draw, line, font, max_width) for line in lines[: max_lines - 1]]
    kept.append(_truncate_line(draw, lines[max_lines - 1] + " …", font, max_width))
    return kept


@dataclass(frozen=True)
class _DatapointLayout:
    """A single positioned datapoint row (value above its muted label)."""

    value: str
    label: str
    value_y: int
    label_y: int
    value_font_size: int
    label_font_size: int


@dataclass(frozen=True)
class StatCardLayout:
    """The fully measured stat-card layout, guaranteed to fit the canvas.

    Computed once (``stat_card_layout``) and consumed by ``render_stat_card`` so
    the draw path never places text off-canvas. ``content_bottom`` is the lowest
    pixel any content occupies and is asserted ``<= canvas height`` before render.
    """

    title_line: str
    datapoints: tuple[_DatapointLayout, ...]
    quote_lines: tuple[str, ...]
    quote_top: int
    source_line: str | None
    content_bottom: int


def _fit_value_font(
    step: int, label_line_height: int, natural_size: int
) -> tuple[int, ImageFont.FreeTypeFont]:
    """Pick the largest value font whose row (value+label) fits one ``step``.

    Starts at ``natural_size`` and walks the ladder down, so rows shrink only as
    far as the vertical budget demands — keeping numbers as legible as possible
    while never overlapping the next row.
    """
    for size in _VALUE_FONT_LADDER:
        if size > natural_size:
            continue
        font = _font(size)
        if _line_height(font) + _STAT_LABEL_LINE_GAP + label_line_height <= step:
            return size, font
    smallest = _VALUE_FONT_LADDER[-1]
    return smallest, _font(smallest)


def stat_card_layout(spec: CardSpec, settings: Settings | None = None) -> StatCardLayout:
    """Measure a fit-to-canvas layout for ``spec`` (BRD §13.6 precision-first).

    Wraps/truncates the title, every datapoint value+label, and the optional
    quote to the measured available width, and spaces the datapoints with a
    dynamic step (shrinking the value font when needed) so the title, up to
    ``_MAX_DATAPOINTS`` datapoints, an optional quote, and the source/watermark
    footer all fit inside the 627px canvas. Separated from the draw path so the
    fit invariant is unit-testable via ``content_bottom``.

    Raises:
        ValueError: If ``spec`` carries more than ``_MAX_DATAPOINTS`` datapoints
            (they cannot stay legible in the vertical budget), or if the measured
            content still exceeds the canvas (a backstop that should never fire).
    """
    settings = settings or get_settings()
    if len(spec.datapoints) > _MAX_DATAPOINTS:
        raise ValueError(
            f"stat card has {len(spec.datapoints)} datapoints; at most "
            f"{_MAX_DATAPOINTS} fit legibly on a {LINKEDIN_LANDSCAPE[1]}px card "
            "(§13.6 — never render an overlapping, off-canvas card)"
        )

    width, height = LINKEDIN_LANDSCAPE
    max_width = width - 2 * _STAT_MARGIN_X
    # A throwaway 1x1 surface: text metrics do not depend on the target image.
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    title_line = _truncate_line(scratch, spec.title, _font(_STAT_TITLE_FONT), max_width)

    source_present = bool(spec.source_label)
    source_line = spec.source_label if source_present else None
    source_font = _font(_STAT_SOURCE_FONT)
    source_bottom = (
        height - _STAT_SOURCE_FROM_BOTTOM + _line_height(source_font) if source_present else 0
    )
    # Datapoints + quote must finish above the source footer (or canvas edge).
    content_limit = (height - _STAT_SOURCE_FROM_BOTTOM if source_present else height) - _STAT_FOOTER_GAP

    quote_present = bool(spec.quote)
    quote_font = _font(_STAT_QUOTE_FONT)
    quote_line_height = _line_height(quote_font)
    quote_reserve = _STAT_QUOTE_MAX_LINES * quote_line_height if quote_present else 0
    data_bottom_limit = content_limit - (quote_reserve + _STAT_FOOTER_GAP if quote_present else 0)

    natural_value_size, label_size = _DATAPOINT_FONTS[max(len(spec.datapoints), 1)]
    label_font = _font(label_size)
    label_line_height = _line_height(label_font)

    datapoints: list[_DatapointLayout] = []
    data_bottom = _STAT_DATA_TOP
    count = len(spec.datapoints)
    if count:
        step = max(1, (data_bottom_limit - _STAT_DATA_TOP) // count)
        value_size, value_font = _fit_value_font(step, label_line_height, natural_value_size)
        value_line_height = _line_height(value_font)
        cursor_y = _STAT_DATA_TOP
        for point in spec.datapoints:
            value_line = _truncate_line(scratch, point.value, value_font, max_width)
            label_line = _truncate_line(scratch, point.label, label_font, max_width)
            label_y = cursor_y + value_line_height + _STAT_LABEL_LINE_GAP
            datapoints.append(
                _DatapointLayout(
                    value=value_line,
                    label=label_line,
                    value_y=cursor_y,
                    label_y=label_y,
                    value_font_size=value_size,
                    label_font_size=label_size,
                )
            )
            data_bottom = label_y + label_line_height
            cursor_y += step

    quote_lines: list[str] = []
    quote_top = 0
    quote_bottom = 0
    if quote_present:
        quote_top = data_bottom_limit + _STAT_FOOTER_GAP
        wrapped = _wrap_lines(
            scratch, f"“{spec.quote}”", quote_font, max_width, _STAT_QUOTE_MAX_LINES
        )
        quote_lines = wrapped
        quote_bottom = quote_top + len(wrapped) * quote_line_height

    content_bottom = max(data_bottom, quote_bottom, source_bottom)
    if content_bottom > height:
        raise ValueError(
            f"stat card content bottom {content_bottom}px exceeds canvas "
            f"{height}px (§13.6 fit invariant)"
        )

    return StatCardLayout(
        title_line=title_line,
        datapoints=tuple(datapoints),
        quote_lines=tuple(quote_lines),
        quote_top=quote_top,
        source_line=source_line,
        content_bottom=content_bottom,
    )


def _draw_watermark(image: Image.Image, settings: Settings) -> None:
    """Apply a discreet BRAHMASTRA watermark in-place when config requests it.

    Honours ``POST_SIGNATURE_MODE`` (§15.6, D9): only ``card_watermark`` and
    ``both`` draw a mark. If a raster logo exists at ``BRAHMASTRA_LOGO_PATH`` it
    is composited bottom-right; otherwise a gold wordmark is drawn (the default
    logo path is an ``.svg`` Pillow cannot rasterise, so the wordmark is the
    normal path). Kept mutation-in-place because it operates on the freshly
    created canvas the caller owns — no shared state escapes.
    """
    if settings.post_signature_mode not in (SignatureMode.CARD_WATERMARK, SignatureMode.BOTH):
        return

    palette = _parse_palette(settings.card_brand_palette)
    logo_path = settings.brahmastra_logo_path

    # Prefer an actual raster logo if the owner supplied one Pillow can read.
    if logo_path.exists() and logo_path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        try:
            logo = Image.open(logo_path).convert("RGBA")
            # Scale the logo to a discreet ~120px height, preserving aspect.
            target_h = 120
            ratio = target_h / logo.height
            logo = logo.resize((int(logo.width * ratio), target_h))
            margin = 40
            image.paste(
                logo,
                (image.width - logo.width - margin, image.height - logo.height - margin),
                logo,
            )
            return
        except (OSError, ValueError) as exc:
            # Unreadable logo file → fall through to the wordmark (never crash).
            logger.warning("could not load logo %s; drawing wordmark instead: %s", logo_path, exc)

    # Wordmark fallback: a small, semi-discreet gold "BRAHMASTRA" bottom-right.
    draw = ImageDraw.Draw(image)
    mark = "BRAHMASTRA"
    font = _font(28)
    width = _text_width(draw, mark, font)
    margin = 36
    draw.text(
        (image.width - width - margin, image.height - 28 - margin),
        mark,
        font=font,
        fill=palette.get("gold", _FALLBACK_GOLD),
    )


def _png_bytes(image: Image.Image) -> bytes:
    """Serialise a Pillow image to PNG bytes via an in-memory buffer."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _assert_dimensions(data: bytes, expected: tuple[int, int]) -> None:
    """Verify serialised PNG ``data`` is exactly ``expected`` (w, h).

    A final guard so a rendering-engine change can never silently ship the wrong
    canvas size to LinkedIn — the invariant is checked on the actual bytes.
    """
    with Image.open(io.BytesIO(data)) as check:
        if check.size != expected:
            raise ValueError(
                f"rendered image is {check.size}, expected {expected} (§13.6 dims)"
            )


def render_stat_card(spec: CardSpec, settings: Settings | None = None) -> bytes:
    """Render a deterministic stat/quote card as PNG bytes at 1200x627.

    Layout: navy background, gold rule under a white title, then each grounded
    datapoint as a large gold value with a muted white label. An optional
    pull-quote and source attribution render beneath. Every value is drawn
    straight from ``spec.datapoints`` — nothing is synthesised — and each must be
    grounded (see ``_assert_grounded``).

    Args:
        spec: The validated card content (title + grounded datapoints).
        settings: Config source (palette + signature mode); defaults to the
            process singleton.

    Returns:
        PNG bytes, exactly ``1200x627``.

    Raises:
        ValueError: If any datapoint lacks a ``source_item_id``.
    """
    settings = settings or get_settings()
    _assert_grounded(spec)  # fail loudly BEFORE drawing any number
    # Measure a fit-to-canvas layout (wraps/truncates text, caps + spaces the
    # datapoints); raises if the content cannot fit rather than overflow (§13.6).
    layout = stat_card_layout(spec, settings)
    palette = _parse_palette(settings.card_brand_palette)
    navy = palette.get("navy", _FALLBACK_NAVY)
    gold = palette.get("gold", _FALLBACK_GOLD)

    width, height = LINKEDIN_LANDSCAPE
    image = Image.new("RGB", (width, height), navy)
    draw = ImageDraw.Draw(image)

    margin_x = _STAT_MARGIN_X
    # --- Title + gold underline -------------------------------------------
    draw.text((margin_x, _STAT_TITLE_Y), layout.title_line, font=_font(_STAT_TITLE_FONT), fill=_WHITE)
    draw.rectangle(
        [(margin_x, _STAT_RULE_TOP), (width - margin_x, _STAT_RULE_BOTTOM)],
        fill=gold,
    )

    # --- Datapoints (value big/gold, label muted) --------------------------
    for point in layout.datapoints:
        draw.text(
            (margin_x, point.value_y), point.value, font=_font(point.value_font_size), fill=gold
        )
        draw.text(
            (margin_x, point.label_y), point.label, font=_font(point.label_font_size), fill=_WHITE
        )

    # --- Optional pull-quote ----------------------------------------------
    if layout.quote_lines:
        quote_font = _font(_STAT_QUOTE_FONT)
        line_height = _line_height(quote_font)
        for offset, line in enumerate(layout.quote_lines):
            draw.text(
                (margin_x, layout.quote_top + offset * line_height),
                line,
                font=quote_font,
                fill=_WHITE,
            )

    # --- Optional discreet source attribution ------------------------------
    if layout.source_line:
        source_font = _font(_STAT_SOURCE_FONT)
        draw.text(
            (margin_x, height - _STAT_SOURCE_FROM_BOTTOM), layout.source_line, font=source_font, fill=gold
        )

    _draw_watermark(image, settings)

    data = _png_bytes(image)
    _assert_dimensions(data, LINKEDIN_LANDSCAPE)
    return data


def _fit_quote_block(
    draw: ImageDraw.ImageDraw,
    quote: str,
    max_width: int,
    max_height: int,
) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
    """Auto-fit a quote into ``max_width`` x ``max_height`` and return the block.

    Walks ``_QUOTE_FONT_LADDER`` from large to small, wrapping the quote at each
    size, and returns the FIRST size whose wrapped block fits the vertical budget
    — so a short quote renders large and a long quote shrinks gracefully. If even
    the smallest font cannot fit, the block is wrapped at ``_QUOTE_MAX_LINES`` and
    the overflow is folded into an ellipsised final line by ``_wrap_lines`` (the
    truncate-within-bounds guarantee, §13.6). WHY a search rather than a formula:
    line count after greedy wrapping is font-dependent and not closed-form, so an
    empirical descent is the reliable, deterministic way to pick the largest fit.

    Returns:
        A ``(lines, font, line_step)`` tuple where ``line_step`` is the per-line
        vertical advance (line height * spacing), used by the caller to place and
        vertically centre the block.
    """
    for size in _QUOTE_FONT_LADDER:
        font = _font(size)
        line_step = int(_line_height(font) * _QUOTE_LINE_SPACING)
        # Cap lines generously here; only the smallest font should ever truncate.
        lines = _wrap_lines(draw, quote, font, max_width, _QUOTE_MAX_LINES)
        if len(lines) * line_step <= max_height:
            return lines, font, line_step

    # Smallest font still overflowed → force a hard cap so it truncates in bounds.
    font = _font(_QUOTE_FONT_LADDER[-1])
    line_step = int(_line_height(font) * _QUOTE_LINE_SPACING)
    max_lines = max(1, max_height // line_step)
    lines = _wrap_lines(draw, quote, font, max_width, min(max_lines, _QUOTE_MAX_LINES))
    return lines, font, line_step


def render_quote_card(
    quote: str,
    *,
    attribution: str | None = None,
    settings: Settings | None = None,
    dimensions: tuple[int, int] = LINKEDIN_SQUARE,
) -> bytes:
    """Render a deterministic, on-brand quote card as PNG bytes.

    WHY deterministic (not agy/diffusion): a quote is WORDS, and diffusion models
    mangle text (BRD §13.6/D10) — so the quote is laid out pixel-precisely by
    Pillow. Unlike the stat card there is NO grounding requirement: a quote is
    prose, not numbers, so nothing needs a ``source_item_id``.

    Layout: a navy field, a large decorative gold opening quotation mark with a
    short gold accent rule beneath it, then the quote auto-fit and centred (word-
    wrapped, font-size chosen to fill the canvas without overflowing), and an
    optional discreet attribution line near the bottom. A watermark is applied per
    ``POST_SIGNATURE_MODE`` (reusing the shared ``_draw_watermark``).

    Args:
        quote: The quote prose to render. Must be non-blank (fail-closed).
        attribution: Optional source/author, rendered as ``"— attribution"``.
        settings: Config source (palette + signature mode); defaults to the
            process singleton.
        dimensions: Target canvas — square ``1200x1200`` (default) or the
            landscape ``1200x627`` link-preview shape.

    Returns:
        PNG bytes, exactly ``dimensions``.

    Raises:
        ValueError: If ``quote`` is blank/whitespace-only.
    """
    settings = settings or get_settings()

    # Fail closed: a blank quote has nothing to render and must not emit an empty
    # card silently (§22 — fail-closed, never a misleading empty visual).
    quote_text = quote.strip()
    if not quote_text:
        raise ValueError("render_quote_card requires a non-empty quote")

    palette = _parse_palette(settings.card_brand_palette)
    navy = palette.get("navy", _FALLBACK_NAVY)
    gold = palette.get("gold", _FALLBACK_GOLD)

    width, height = dimensions
    image = Image.new("RGB", (width, height), navy)
    draw = ImageDraw.Draw(image)

    margin_x = _QUOTE_MARGIN_X
    max_width = width - 2 * margin_x

    # --- Decorative gold opening quotation mark + accent rule --------------
    # WHY an accent instead of a datapoint: the quote card's "tasteful gold
    # accent" is a mark + rule (the stat card uses a full underline). Sizes are
    # scaled down on the shorter landscape canvas so the mark never dominates.
    mark_font_size = _QUOTE_MARK_FONT if height >= 900 else int(_QUOTE_MARK_FONT * 0.6)
    mark_font = _font(mark_font_size)
    draw.text((margin_x, int(_QUOTE_MARGIN_TOP * 0.25)), "“", font=mark_font, fill=gold)
    rule_y = _QUOTE_MARGIN_TOP
    draw.rectangle(
        [(margin_x, rule_y), (margin_x + _QUOTE_ACCENT_RULE_W, rule_y + _QUOTE_ACCENT_RULE_H)],
        fill=gold,
    )

    # --- Auto-fit + centre the quote block ---------------------------------
    # Vertical budget sits between the accent rule and the attribution footer.
    block_top_limit = rule_y + _QUOTE_ACCENT_RULE_H + 40
    block_bottom_limit = height - _QUOTE_MARGIN_BOTTOM
    available_height = block_bottom_limit - block_top_limit
    lines, body_font, line_step = _fit_quote_block(draw, quote_text, max_width, available_height)

    block_height = len(lines) * line_step
    # Centre the block vertically within the available band.
    start_y = block_top_limit + max(0, (available_height - block_height) // 2)
    for offset, line in enumerate(lines):
        line_width = _text_width(draw, line, body_font)
        line_x = margin_x + max(0, (max_width - line_width) // 2)  # horizontal centre
        draw.text((line_x, start_y + offset * line_step), line, font=body_font, fill=_WHITE)

    # --- Optional discreet attribution -------------------------------------
    if attribution and attribution.strip():
        attrib_font = _font(_QUOTE_ATTRIB_FONT)
        # Truncate so a long name can never run off the right edge (§13.6).
        attrib_line = _truncate_line(draw, f"— {attribution.strip()}", attrib_font, max_width)
        attrib_width = _text_width(draw, attrib_line, attrib_font)
        attrib_x = margin_x + max(0, (max_width - attrib_width) // 2)
        draw.text(
            (attrib_x, height - _QUOTE_ATTRIB_FROM_BOTTOM), attrib_line, font=attrib_font, fill=gold
        )

    _draw_watermark(image, settings)

    data = _png_bytes(image)
    _assert_dimensions(data, dimensions)
    return data


# --- Contrast card (two-panel anime comparison, BRD §13.6 + owner request) ----
# Layout from agy's design review: 1080x1080, two 540-wide panels, a 4px black
# center divider, and a semi-transparent black band (Y 860-1040) behind each
# crisp label. The anime PANELS come TEXT-FREE from agy; the labels are composited
# deterministically here so text is always legible (diffusion mangles words).
_CONTRAST_SIZE = 1080
_CONTRAST_HALF = 540
_CONTRAST_BAND_TOP = 860
_CONTRAST_BAND_BOTTOM = 1040
_CONTRAST_LABEL_FONTS = (104, 92, 80, 68, 58, 48, 40)  # descending auto-fit search
_CONTRAST_PAD = 34  # horizontal padding inside a panel for the label text


def _cover_crop(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale ``img`` to COVER a ``width``x``height`` box, then center-crop to fit."""
    img = img.convert("RGB")
    scale = max(width / img.width, height / img.height)
    resized = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _wrap_words(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """Greedy word-wrap ``text`` to lines no wider than ``max_w`` (1-3 word labels)."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if not current or _text_width(draw, trial, font) <= max_w:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _draw_band_label(
    draw: ImageDraw.ImageDraw, text: str, panel_x0: int, band_top: int, band_bottom: int
) -> None:
    """Auto-fit + center a bold ALL-CAPS label inside one panel's overlay band."""
    max_w = _CONTRAST_HALF - 2 * _CONTRAST_PAD
    max_h = (band_bottom - band_top) - 24
    font = _font(_CONTRAST_LABEL_FONTS[-1])
    lines = [text]
    for size in _CONTRAST_LABEL_FONTS:
        font = _font(size)
        lines = _wrap_words(draw, text, font, max_w)
        line_h = size + 8
        widest = max((_text_width(draw, ln, font) for ln in lines), default=0)
        if line_h * len(lines) <= max_h and widest <= max_w:
            break
    line_h = font.size + 8
    total_h = line_h * len(lines)
    start_y = band_top + ((band_bottom - band_top) - total_h) // 2
    for i, line in enumerate(lines):
        line_w = _text_width(draw, line, font)
        line_x = panel_x0 + (_CONTRAST_HALF - line_w) // 2
        draw.text((line_x, start_y + i * line_h), line, font=font, fill=_WHITE)


def render_contrast_card(
    left_panel: bytes,
    right_panel: bytes,
    left_label: str,
    right_label: str,
    *,
    settings: Settings | None = None,
) -> bytes:
    """Composite two anime panels + crisp labels into a 1080x1080 contrast card.

    ``left_panel``/``right_panel`` are text-free PNG bytes (from agy); the labels
    are drawn deterministically over a readable band so the words are always crisp.
    """
    settings = settings or get_settings()
    canvas = Image.new("RGB", (_CONTRAST_SIZE, _CONTRAST_SIZE), _FALLBACK_NAVY)
    canvas.paste(_cover_crop(Image.open(io.BytesIO(left_panel)), _CONTRAST_HALF, _CONTRAST_SIZE), (0, 0))
    canvas.paste(
        _cover_crop(Image.open(io.BytesIO(right_panel)), _CONTRAST_HALF, _CONTRAST_SIZE),
        (_CONTRAST_HALF, 0),
    )

    # Divider + the two readability bands, composited as one RGBA layer.
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([_CONTRAST_HALF - 2, 0, _CONTRAST_HALF + 2, _CONTRAST_SIZE], fill=(0, 0, 0, 255))
    for x0 in (0, _CONTRAST_HALF):
        od.rectangle(
            [x0, _CONTRAST_BAND_TOP, x0 + _CONTRAST_HALF, _CONTRAST_BAND_BOTTOM], fill=(0, 0, 0, 180)
        )
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(canvas)
    _draw_band_label(draw, left_label.upper().strip(), 0, _CONTRAST_BAND_TOP, _CONTRAST_BAND_BOTTOM)
    _draw_band_label(
        draw, right_label.upper().strip(), _CONTRAST_HALF, _CONTRAST_BAND_TOP, _CONTRAST_BAND_BOTTOM
    )
    # Respect the owner's signature mode (off => no stamp; no forced AI attribution).
    _draw_watermark(canvas, settings)
    return _png_bytes(canvas)


def render_chart(spec: CardSpec, settings: Settings | None = None) -> bytes:
    """Render a simple bar/line chart as PNG bytes at 1200x1200.

    Chart type comes from ``spec.chart_type`` (``'line'`` → line, anything else →
    bar). Datapoint values are parsed to floats via ``Datapoint.numeric`` (which
    raises on non-numeric data), keeping the chart strictly grounded in the
    supplied figures. matplotlib renders the plot; Pillow re-opens the bytes to
    apply the shared brand watermark, so charts and cards carry an identical mark.

    Args:
        spec: Card content whose datapoints supply the series (label → value).
        settings: Config source (palette + signature mode).

    Returns:
        PNG bytes, exactly ``1200x1200``.

    Raises:
        ValueError: If there are no datapoints, any lacks a ``source_item_id``,
            or any value is non-numeric.
    """
    settings = settings or get_settings()
    _assert_grounded(spec)  # provenance before plotting

    if not spec.datapoints:
        raise ValueError("render_chart requires at least one datapoint")

    palette = _parse_palette(settings.card_brand_palette)
    navy = palette.get("navy", _FALLBACK_NAVY)
    gold = palette.get("gold", _FALLBACK_GOLD)

    labels = [point.label for point in spec.datapoints]
    # ``numeric`` raises on non-numeric values → a chart over bad data fails loud.
    values = [point.numeric() for point in spec.datapoints]

    width, height = LINKEDIN_SQUARE
    # figsize in inches * dpi = pixels → deterministic exact-size export.
    figure, axes = plt.subplots(
        figsize=(width / _CHART_DPI, height / _CHART_DPI), dpi=_CHART_DPI
    )
    try:
        figure.patch.set_facecolor(navy)
        axes.set_facecolor(navy)

        if (spec.chart_type or "bar").lower() == "line":
            axes.plot(labels, values, color=gold, marker="o", linewidth=3)
        else:
            axes.bar(labels, values, color=gold)

        # On-brand styling: white title/ticks against the navy field.
        axes.set_title(spec.title, color=_WHITE, fontsize=22, pad=20)
        axes.tick_params(colors=_WHITE, labelsize=14)
        for spine in axes.spines.values():
            spine.set_color(_WHITE)

        buffer = io.BytesIO()
        # facecolor on savefig ensures the exported margin matches the field.
        figure.savefig(buffer, format="PNG", dpi=_CHART_DPI, facecolor=navy)
    finally:
        # Always release the figure — a leaked figure accumulates memory across
        # the daily run (matplotlib keeps global references otherwise).
        plt.close(figure)

    # Post-process through Pillow for the shared watermark, then re-serialise.
    with Image.open(io.BytesIO(buffer.getvalue())) as chart_image:
        chart_rgb = chart_image.convert("RGB")
    _draw_watermark(chart_rgb, settings)

    data = _png_bytes(chart_rgb)
    _assert_dimensions(data, LINKEDIN_SQUARE)
    return data
