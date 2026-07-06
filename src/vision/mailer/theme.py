"""HTML email theming for VISION approval mail (BRD §13.6, §14.1).

WHY this module exists: the daily approval email must render cleanly in every
mail client (Gmail, Outlook, Apple Mail) where modern CSS, external stylesheets
and web fonts are unreliable. So every visual style here is *inline* and
table-based — the email-safe subset — adapted from finalert's
``alerts/email_theme.py`` but **re-paletted to VISION's navy/gold brand**.

The palette is NOT hard-coded: it is parsed from ``settings.CARD_BRAND_PALETTE``
(config over code, §22) which ships as ``"navy=#0B1F3A;gold=#C9A24B"``. Changing
the brand is therefore a config edit, never a code change.

Public surface (kept deliberately small so composer.py stays declarative):
  * :func:`wrap_shell` — full document shell: brand strip + header + body + footer
  * :func:`chip`       — a small coloured pill (a flag / status label)
  * :func:`bar`        — a 0-1 progress bar (confidence / grounding)
  * :func:`button`     — an email-safe call-to-action anchor (Approve / Reject …)
"""

from __future__ import annotations

import html as _html
import urllib.parse as _urlparse
from dataclasses import dataclass

from vision.config import Settings, get_settings

# --- Neutral surfaces -------------------------------------------------------
# WHY constants (not config): the brand colours (navy/gold) are the only thing
# the owner may want to re-skin; the paper-like neutrals are a fixed, tested
# backdrop that both brand colours were chosen to sit against. Keeping them here
# means the palette config stays a two-value knob, not a full theme sheet.
BG_PAGE = "#F4F5F7"  # page background behind the card
BG_CARD = "#FFFFFF"  # the email card surface
BG_CARD_ALT = "#F8F9FB"  # inset panels (the post text block, quality report)
BORDER = "#E3E6EC"  # hairline borders
BORDER_SOFT = "#EDEFF3"
TEXT_PRIMARY = "#14213A"  # near-navy body text for contrast on white
TEXT_SECOND = "#4A5468"
TEXT_MUTED = "#8A93A6"

# Fallbacks used only if the palette config is malformed — the brand still
# renders rather than the email failing to build (fail-safe, §14.5).
_DEFAULT_NAVY = "#0B1F3A"
_DEFAULT_GOLD = "#C9A24B"

# Monospace stack reused for figures/char-counts so numbers align like a report.
_MONO = "ui-monospace,Menlo,Consolas,'SF Mono',monospace"
_SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


@dataclass(frozen=True)
class Palette:
    """Immutable brand colours resolved from ``settings.CARD_BRAND_PALETTE``.

    Frozen because a palette is a value object: once resolved for a render it
    must not mutate midway (immutability principle). ``gold_soft`` is a derived
    tint used for chip backgrounds so a single gold value drives both.
    """

    navy: str
    gold: str

    @property
    def navy_soft(self) -> str:
        """A pale navy wash for chip backgrounds (kept as a literal, not computed,
        because email clients cannot do colour math and we want a tested value)."""
        return "#EAEEF5"

    @property
    def gold_soft(self) -> str:
        """A pale gold wash for positive/brand chip backgrounds."""
        return "#FBF5E6"


def parse_palette(spec: str) -> Palette:
    """Parse the ``"navy=#0B1F3A;gold=#C9A24B"`` config string into a :class:`Palette`.

    WHY tolerant parsing: a malformed or partial palette must never crash the
    daily email (fail-safe). Unknown keys are ignored and any missing colour
    falls back to the brand default, so the worst case is "default navy/gold",
    not "no email". The format is ``key=value`` pairs separated by ``;``.
    """
    colors: dict[str, str] = {}
    for pair in spec.split(";"):
        # Skip empty fragments from a trailing/duplicate ";" without raising.
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            colors[key] = value
    return Palette(
        navy=colors.get("navy", _DEFAULT_NAVY),
        gold=colors.get("gold", _DEFAULT_GOLD),
    )


def _palette(settings: Settings | None = None) -> Palette:
    """Resolve the active palette from settings (single source of truth).

    Injectable ``settings`` keeps the theme functions pure and unit-testable; in
    production the cached ``get_settings()`` singleton supplies the config.
    """
    resolved = settings if settings is not None else get_settings()
    return parse_palette(resolved.card_brand_palette)


def chip(label: str, tone: str = "neutral", *, settings: Settings | None = None) -> str:
    """Render a small coloured pill for a status/flag label (e.g. a compliance flag).

    ``tone`` selects the colour scheme without exposing raw hex to callers:
      * ``"ok"``      — brand gold on a soft gold wash (a passed check)
      * ``"warn"``    — amber (a soft breach worth the owner's eye)
      * ``"bad"``     — red (a hard flag)
      * ``"neutral"`` — muted grey (informational)

    The label is HTML-escaped so a flag string sourced from model output can
    never inject markup into the email (defence in depth, §22 security).
    """
    pal = _palette(settings)
    schemes: dict[str, tuple[str, str, str]] = {
        "ok": (pal.gold_soft, "#7A5B12", pal.gold),
        "warn": ("#FFF6E5", "#8A5A00", "#E6A100"),
        "bad": ("#FDECEC", "#B3261E", "#E5484D"),
        "neutral": ("#EEF0F4", TEXT_SECOND, "#CFD4DE"),
    }
    bg, fg, border = schemes.get(tone, schemes["neutral"])
    safe = _html.escape(label)
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f"padding:3px 10px;border-radius:11px;font-size:11px;font-weight:700;"
        f"letter-spacing:.4px;border:1px solid {border};font-family:{_SANS};"
        f'margin:2px 4px 2px 0;">{safe}</span>'
    )


def bar(value: float, label: str = "", *, settings: Settings | None = None) -> str:
    """Render an email-safe 0-1 progress bar (used for confidence / grounding).

    Renders as a nested table (the only layout primitive every mail client
    honours). ``value`` is clamped into ``[0, 1]`` so a malformed score can never
    produce a negative width or overflow the track (fail-safe).
    """
    pal = _palette(settings)
    try:
        # A non-numeric value degrades to 0 rather than raising mid-render.
        clamped = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        clamped = 0.0
    pct = int(round(clamped * 100))
    # Gold once the score is strong, muted while it is weak — a glanceable cue.
    fill = pal.gold if clamped >= 0.75 else TEXT_MUTED
    caption = f"{_html.escape(label)} " if label else ""
    return (
        '<table cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
        '<td style="width:120px;padding-right:8px;vertical-align:middle;">'
        f'<div style="background:{BORDER};border-radius:6px;height:7px;overflow:hidden;">'
        f'<div style="background:{fill};height:7px;width:{pct}%;"></div></div></td>'
        f'<td style="color:{TEXT_SECOND};font-size:12px;font-family:{_MONO};'
        f'white-space:nowrap;">{caption}{pct}%</td>'
        "</tr></table>"
    )


_ALLOWED_URL_SCHEMES = ("http", "https")


def safe_url(url: str) -> str:
    """Return an href-safe URL: only http/https allowed, fully attribute-escaped.

    WHY: source URLs and the published post URL come from scraped or model-derived
    items (untrusted), not from our own signed minting. A raw double-quote would
    break out of the ``href="..."`` attribute and inject markup / a tracking pixel
    (defeating the threat model's "no third-party assets" rule), and a
    ``javascript:``/``data:`` scheme would execute in some mail clients. We
    allowlist the scheme first, then ``html.escape(quote=True)`` so the value can
    never escape the attribute context. Anything not http/https collapses to a
    harmless dead ``#`` link. Server-minted https links pass through unchanged
    (base64url tokens contain no characters that require escaping).
    """
    try:
        scheme = _urlparse.urlparse(url).scheme.lower()
    except (ValueError, TypeError):
        # Malformed input is treated as unsafe, never emitted as a live link.
        return "#"
    if scheme not in _ALLOWED_URL_SCHEMES:
        return "#"
    return _html.escape(url, quote=True)


def button(text: str, url: str, *, primary: bool = False, settings: Settings | None = None) -> str:
    """Render one email-safe call-to-action anchor (Approve / Post now / Edit / Reject).

    WHY an ``<a>`` styled as a button (not a ``<button>``): form controls do not
    work in email; a padded, coloured anchor is the portable pattern. The primary
    action (Approve) fills with navy; secondary actions are gold-outlined so the
    owner's eye lands on Approve first.

    ``url`` is passed through :func:`safe_url` (scheme allowlist + attribute
    escape) and the visible ``text`` is HTML-escaped. Signed server-minted links
    pass through unchanged; this defends the confirmation "View post" case where
    the URL is the untrusted, model-derived published post URL. Per the threat
    model the action links are single-use and short-TTL; the GET only shows a
    confirmation page — the state change happens on the POST from that page.
    """
    pal = _palette(settings)
    if primary:
        bg, fg, border = pal.navy, "#FFFFFF", pal.navy
    else:
        bg, fg, border = "#FFFFFF", pal.navy, pal.gold
    safe = _html.escape(text)
    return (
        f'<a href="{safe_url(url)}" '
        f"style=\"display:inline-block;background:{bg};color:{fg};"
        f"border:1.5px solid {border};text-decoration:none;font-weight:700;"
        f"font-size:14px;font-family:{_SANS};padding:11px 20px;border-radius:8px;"
        f'margin:4px 6px 4px 0;letter-spacing:.2px;">{safe}</a>'
    )


def _brand_strip(pal: Palette) -> str:
    """The navy→gold top/bottom accent strip that brands the shell."""
    return (
        f'<div style="height:5px;background:{pal.navy};"></div>'
        f'<div style="height:3px;background:{pal.gold};"></div>'
    )


def wrap_shell(title: str, subtitle: str, body: str, kpi: str, *, settings: Settings | None = None) -> str:
    """Wrap a body fragment in the full VISION email document (BRD §13.6 branding).

    Layout mirrors finalert's shell — brand strip, header (title + subtitle on the
    left, a single KPI on the right), the caller's ``body`` HTML, footer — but is
    re-paletted navy/gold and driven entirely by inline styles.

    Args:
        title:    the email headline (e.g. the post focus). HTML-escaped.
        subtitle: a mono sub-line (e.g. the date). HTML-escaped.
        body:     already-built inner HTML rows (``<tr>…`` fragments). NOT escaped
                  — the composer is trusted to build safe markup here.
        kpi:      the single right-aligned figure (e.g. char count / confidence).
                  HTML-escaped.
    """
    pal = _palette(settings)
    t, s, k = _html.escape(title), _html.escape(subtitle), _html.escape(kpi)
    header = (
        f'<tr><td style="padding:22px 30px 16px;background:{BG_CARD};">'
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>'
        "<td>"
        f'<div style="color:{pal.gold};font-size:11px;letter-spacing:3px;'
        f'text-transform:uppercase;font-weight:800;">VISION · DAILY DRAFT</div>'
        f'<div style="color:{TEXT_PRIMARY};font-size:21px;font-weight:700;'
        f'margin-top:6px;letter-spacing:-0.2px;">{t}</div>'
        f'<div style="color:{TEXT_MUTED};font-size:13px;margin-top:4px;'
        f'font-family:{_MONO};">{s}</div>'
        "</td>"
        '<td align="right" style="vertical-align:top;">'
        f'<div style="color:{TEXT_MUTED};font-size:10px;text-transform:uppercase;'
        f'letter-spacing:1.5px;font-weight:700;">STATUS</div>'
        f'<div style="color:{pal.navy};font-size:20px;font-weight:800;'
        f'font-family:{_MONO};margin-top:2px;">{k}</div>'
        "</td>"
        "</tr></table></td></tr>"
    )
    footer_strip = _brand_strip(pal)
    return (
        "<!DOCTYPE html>"
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:{BG_PAGE};'
        f'font-family:{_SANS};color:{TEXT_PRIMARY};">'
        f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="background:{BG_PAGE};padding:20px 0;"><tr><td align="center">'
        f'<table width="680" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="max-width:680px;width:100%;background:{BG_CARD};'
        f'border:1px solid {BORDER};border-radius:14px;overflow:hidden;">'
        f'<tr><td style="padding:0;">{_brand_strip(pal)}</td></tr>'
        f"{header}"
        f"{body}"
        f'<tr><td style="padding:0;">{footer_strip}</td></tr>'
        "</table></td></tr></table></body></html>"
    )
