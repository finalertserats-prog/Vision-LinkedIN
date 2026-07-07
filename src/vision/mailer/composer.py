"""Compose the daily approval + confirmation emails (BRD §14.1, Appendix B).

WHY this module exists: it is the single place that turns a verified ``Draft`` (+
its sources + freshly-minted signed action links) into the exact three artefacts
a provider needs — ``(subject, text, html)``. Keeping composition pure (no DB, no
network, no token minting) makes every section deterministically testable and
keeps the security-sensitive link *creation* in ``approval/tokens.py`` where it
belongs; this module only *places* links it is handed.

The rendered email follows Appendix B section-for-section:
    Subject → PROPOSED POST (+ char count) → IMAGE preview → QUALITY REPORT →
    SOURCES → action buttons → footer (run id + expiry).

Security notes (§22, threat model §1): all draft/model-derived text is
HTML-escaped before it reaches the HTML body (the theme helpers escape their
inputs; free-form blocks are escaped here). The signed links are emitted
verbatim (server-minted, not user input) and only ever point at a GET that shows
a confirmation page — the state change happens on that page's POST.
"""

from __future__ import annotations

import base64
import html as _html
import logging
import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from vision.config import Settings, get_settings
from vision.mailer import theme

log = logging.getLogger(__name__)

# The four actions the news approval email offers, in the BRD's display order,
# paired with their button label and whether they are the primary (navy) CTA.
# Sourced from Appendix B: "Approve & schedule (09:00) · Post now · Edit · Reject".
_ACTION_ORDER: tuple[tuple[str, str, bool], ...] = (
    ("approve", "Approve & schedule 09:00", True),
    ("post_now", "Post now", False),
    ("edit", "Edit", False),
    ("reject", "Reject", False),
)

# A COUNCIL draft offers one EXTRA action — "Overrule" — so the owner can supply a
# one-line counter-take that overrides the council's synthesised post. WHY it sits
# between Edit and Reject: it is an edit-flow *variant* (it reuses the edit
# machinery, not a new endpoint — see ``approval/tokens.py`` VALID_ACTIONS and the
# edit page), so it reads naturally next to Edit. It is never primary — the owner
# should approve the council's own synthesis by default, not reflexively overrule.
_COUNCIL_ACTION_ORDER: tuple[tuple[str, str, bool], ...] = (
    ("approve", "Approve & schedule 09:00", True),
    ("post_now", "Post now", False),
    ("edit", "Edit", False),
    ("overrule", "Overrule", False),
    ("reject", "Reject", False),
)

# The sentinel ``content_mode`` value that marks a draft as council-generated
# (BRD §5 evolution / council-content-vision). Anything else renders the standard
# news email. A constant, not a scattered literal, so the branch is auditable.
_COUNCIL_MODE = "council"


@dataclass(frozen=True)
class SourceRef:
    """A source item shown in the SOURCES section — just a title and a link.

    A tiny value object (not the full ORM ``Item``) so the composer stays
    decoupled from the DB layer and tests can construct sources inline. Frozen
    per the immutability principle.
    """

    title: str
    url: str


class _DraftLike(Protocol):
    """The minimal draft surface the composer reads (structural typing).

    Declared as a ``Protocol`` so the composer depends on the *shape* it needs —
    not on the SQLAlchemy ``Draft`` class — which keeps it unit-testable with a
    lightweight stub while still matching the real ORM row at runtime.
    """

    id: Any
    run_id: Any
    lane_focus: str | None
    post_text: str | None
    quality_report: dict[str, Any] | None
    confidence: float | None
    token_expires_at: datetime | None
    image_type: str
    image_path: str | None
    # --- Council fields (§5 evolution) -------------------------------------
    # These live on the ORM ``Draft`` for council-generated drafts. They are
    # declared ``| None`` / optional here because a NEWS draft has ``content_mode``
    # != 'council' and no ``council_meta``; the composer reads them defensively
    # (via getattr, tolerating an older ORM row that predates the columns) so this
    # module never hard-couples to the DB migration that adds them.
    content_mode: str | None
    council_meta: dict[str, Any] | None


def _fmt_date(when: datetime, settings: Settings) -> str:
    """Format a date as the BRD's ``6 Jul 2026`` form in the owner's timezone.

    Uses ``%-d``-free formatting (``%d`` is zero-padded and not portable to strip)
    by building the day manually, so "06" renders as "6" on every platform
    including Windows, which lacks ``%-d``.
    """
    # Render in the configured wall-clock zone where the datetime is tz-aware.
    localised = when
    try:
        from zoneinfo import ZoneInfo

        if when.tzinfo is not None:
            localised = when.astimezone(ZoneInfo(settings.tz))
    except (KeyError, ValueError, ImportError):
        # An unknown TZ name must not break the subject line; fall back to the
        # value as given rather than raising (fail-safe display path).
        localised = when
    return f"{localised.day} {localised.strftime('%b %Y')}"


def _short_id(value: Any) -> str:
    """Return a short, human-glanceable id prefix for the footer (e.g. ``7f3a…``).

    The full run id is long; Appendix B shows a truncated form. Never security-
    sensitive (it is just a run identifier), so truncation is purely cosmetic.
    """
    text = str(value)
    return f"{text[:8]}…" if len(text) > 8 else text


def _fmt_expiry(when: datetime | None, settings: Settings) -> str:
    """Format the link expiry as ``HH:MM TZ today`` for the footer, or a fallback."""
    if when is None:
        return "end of day"
    localised = when
    try:
        from zoneinfo import ZoneInfo

        if when.tzinfo is not None:
            localised = when.astimezone(ZoneInfo(settings.tz))
    except (KeyError, ValueError, ImportError):
        localised = when
    # Short TZ abbreviation keeps the footer compact (e.g. "20:00 IST").
    tz_label = localised.tzname() or settings.tz
    return f"{localised.strftime('%H:%M')} {tz_label}"


# --- Quality-report rendering ----------------------------------------------


def _quality_lines(report: Mapping[str, Any] | None) -> list[str]:
    """Turn the §14.4 ``quality_report`` dict into human-readable plain-text lines.

    Tolerant of missing keys (a partial report must still render): each metric is
    emitted only when present, so an older or trimmed report degrades gracefully
    rather than raising a ``KeyError`` mid-compose (fail-safe).
    """
    if not report:
        return ["Quality report unavailable."]

    lines: list[str] = []

    grounding = report.get("grounding_pct")
    if grounding is not None:
        # Show the grounded/total fraction when the claim lists are present.
        unsupported = report.get("unsupported_claims") or []
        lines.append(f"Grounding: {grounding}% · unsupported claims: {len(unsupported)}")

    dedup = report.get("dedup_vs_own_90d")
    if isinstance(dedup, Mapping):
        verdict = "PASS" if dedup.get("pass") else "REVIEW"
        sim = dedup.get("max_similarity")
        sim_txt = f" (max sim {sim})" if sim is not None else ""
        lines.append(f"Dedup vs your last 90d: {verdict}{sim_txt}")

    tone = report.get("tone_flags") or []
    compliance = report.get("compliance_flags") or []
    lines.append(f"Tone flags: {', '.join(tone) if tone else 'none'}")
    lines.append(f"Compliance flags: {', '.join(compliance) if compliance else 'none'}")

    confidence = report.get("confidence")
    if confidence is not None:
        lines.append(f"Confidence: {confidence}")

    return lines


def _quality_html(report: Mapping[str, Any] | None, settings: Settings) -> str:
    """Render the quality report as themed HTML (chips for flags, a bar for confidence)."""
    if not report:
        return f'<div style="color:{theme.TEXT_MUTED};font-size:13px;">Quality report unavailable.</div>'

    rows: list[str] = []

    grounding = report.get("grounding_pct")
    if grounding is not None:
        unsupported = report.get("unsupported_claims") or []
        tone = "ok" if not unsupported else "warn"
        rows.append(
            f'<div style="margin:2px 0;">Grounding '
            f"{theme.chip(f'{grounding}%', tone, settings=settings)}"
            f'<span style="color:{theme.TEXT_MUTED};font-size:12px;">'
            f"{len(unsupported)} unsupported</span></div>"
        )

    dedup = report.get("dedup_vs_own_90d")
    if isinstance(dedup, Mapping):
        passed = bool(dedup.get("pass"))
        sim = dedup.get("max_similarity")
        sim_txt = f" · max sim {_html.escape(str(sim))}" if sim is not None else ""
        rows.append(
            f'<div style="margin:6px 0;">Dedup 90d '
            f"{theme.chip('PASS' if passed else 'REVIEW', 'ok' if passed else 'warn', settings=settings)}"
            f'<span style="color:{theme.TEXT_MUTED};font-size:12px;">{sim_txt}</span></div>'
        )

    # Flags: one chip per flag; an empty list renders a single calm "none" chip.
    for kind, flags in (("Tone", report.get("tone_flags") or []), ("Compliance", report.get("compliance_flags") or [])):
        if flags:
            chips = "".join(theme.chip(str(f), "bad", settings=settings) for f in flags)
        else:
            chips = theme.chip("none", "ok", settings=settings)
        rows.append(f'<div style="margin:6px 0;">{kind} flags {chips}</div>')

    confidence = report.get("confidence")
    if confidence is not None:
        rows.append(
            f'<div style="margin:8px 0 2px;">Confidence</div>'
            f"{theme.bar(confidence, settings=settings)}"
        )

    return "".join(rows)


# --- Section builders (HTML) ------------------------------------------------


def _panel_row(inner_html: str) -> str:
    """Wrap section HTML in a padded table row so sections stack in the shell."""
    return f'<tr><td style="padding:8px 30px;">{inner_html}</td></tr>'


def _inset(inner_html: str) -> str:
    """Render an inset panel (the post block / quality block sit on a soft card)."""
    return (
        f'<div style="background:{theme.BG_CARD_ALT};border:1px solid {theme.BORDER_SOFT};'
        f'border-radius:10px;padding:16px 18px;">{inner_html}</div>'
    )


def _image_data_uri(image_path: str) -> str | None:
    """Read a local image file and return an inline ``data:`` URI, or ``None``.

    WHY inline (not a hosted URL): the threat model forbids third-party assets in
    the email (no external fetch that could leak a referrer / confirm an open).
    A base64 data URI keeps the preview self-contained. File-read failures return
    ``None`` (the email still sends, just without the preview) — specific
    exceptions only, never a bare ``except`` (§22).
    """
    path = Path(image_path)
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        log.warning("image preview skipped (%s); sending without it.", exc.__class__.__name__)
        return None
    mime, _ = mimetypes.guess_type(path.name)
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime or 'image/png'};base64,{encoded}"


def _sources_html(sources: Sequence[SourceRef]) -> str:
    """Render the numbered SOURCES list; titles escaped, links scheme-checked + escaped.

    Source URLs are untrusted (scraped / model-derived), so each is passed through
    theme.safe_url (https/http allowlist + attribute escape) — an injected quote or
    javascript: scheme can never break out of the href. See theme.safe_url.
    """
    if not sources:
        return f'<div style="color:{theme.TEXT_MUTED};font-size:13px;">No sources listed.</div>'
    items: list[str] = []
    for idx, src in enumerate(sources, start=1):
        safe_title = _html.escape(src.title)
        items.append(
            f'<li style="margin:4px 0;font-size:13px;">'
            f'<a href="{theme.safe_url(src.url)}" style="color:{theme.TEXT_PRIMARY};">{safe_title}</a></li>'
        )
    return f'<ol style="margin:0;padding-left:20px;color:{theme.TEXT_SECOND};">{"".join(items)}</ol>'


# --- Council rendering (§5 evolution) ---------------------------------------


def _is_council(draft: _DraftLike) -> bool:
    """Return whether ``draft`` is a council draft that should render the council email.

    Read defensively (``getattr``) so a draft object — or an older ORM row — that
    predates the ``content_mode`` / ``council_meta`` columns is simply treated as a
    normal news draft rather than raising. Both a council content_mode AND a
    non-empty ``council_meta`` are required: without the meta there is nothing
    council-specific to render, so we fall back to the standard email (fail-safe).
    """
    mode = getattr(draft, "content_mode", None)
    meta = getattr(draft, "council_meta", None)
    return mode == _COUNCIL_MODE and isinstance(meta, Mapping) and bool(meta)


def _council_block_html(council_block: str) -> str:
    """Render the 3 unnamed viewpoints (+ 'Powered by Brahmastra') as escaped HTML.

    The block is model-derived text, so it is HTML-escaped in full before it reaches
    the body (§22 / threat model §4 — no draft/model text is ever trusted as markup).
    ``white-space:pre-wrap`` preserves the bullet lines exactly as the council editor
    wrote them without us re-parsing (and possibly mis-splitting) them. The block is
    de-named upstream (only 'Powered by Brahmastra' attributes it), so no AI model
    name can appear here.
    """
    return (
        f'<div style="color:{theme.TEXT_MUTED};font-size:11px;text-transform:uppercase;'
        f'letter-spacing:1.5px;font-weight:700;margin-bottom:8px;">Council</div>'
        f'<pre style="margin:0;white-space:pre-wrap;word-wrap:break-word;'
        f"font-family:{theme._SANS};font-size:14px;line-height:1.55;"
        f'color:{theme.TEXT_PRIMARY};">{_html.escape(council_block)}</pre>'
    )


def _raw_debate_html(transcript: Any) -> str:
    """Render the raw debate as a COLLAPSED, fully-escaped ``<details>`` peek.

    WHY collapsed: the un-edited transcript is provenance the owner can open on
    demand, never the headline — it stays folded so the email leads with the post,
    not the debate. The transcript may be a nested ``{voice: {round1, round2}}`` dict
    or a plain string; either way EVERY value is HTML-escaped before rendering, so a
    voice that emitted markup (or an injected string) can never break out of the
    body. If the transcript is missing/empty we render nothing (no empty widget).

    NOTE: the transcript is internal provenance and MAY reference the underlying
    voices; it is shown only in this owner-facing review peek and is NEVER part of
    the published post (the post + Council block are de-named upstream).
    """
    if not transcript:
        return ""

    lines: list[str] = []
    if isinstance(transcript, Mapping):
        # Structured {voice: {"round1": ..., "round2": ...}} — render each round on
        # its own escaped line so the debate reads in order.
        for voice, rounds in transcript.items():
            safe_voice = _html.escape(str(voice))
            if isinstance(rounds, Mapping):
                for label, text in rounds.items():
                    lines.append(
                        f"<div style=\"margin:6px 0;\"><strong>{safe_voice}"
                        f" · {_html.escape(str(label))}</strong>: "
                        f"{_html.escape(str(text))}</div>"
                    )
            else:
                lines.append(
                    f'<div style="margin:6px 0;"><strong>{safe_voice}</strong>: '
                    f"{_html.escape(str(rounds))}</div>"
                )
    else:
        # A plain-string transcript: escape it whole and preserve its line breaks.
        lines.append(
            f'<pre style="margin:0;white-space:pre-wrap;word-wrap:break-word;'
            f'font-size:12px;color:{theme.TEXT_SECOND};">{_html.escape(str(transcript))}</pre>'
        )

    body = "".join(lines)
    # A native <details>/<summary> is the most portable "collapsible" primitive;
    # clients that ignore it simply show the content inline (still safe, still
    # escaped) — the peek is never load-bearing.
    return (
        '<details style="margin:0;">'
        f'<summary style="cursor:pointer;color:{theme.TEXT_MUTED};font-size:11px;'
        'text-transform:uppercase;letter-spacing:1.5px;font-weight:700;">'
        "Raw debate (peek)</summary>"
        f'<div style="margin-top:10px;font-size:13px;color:{theme.TEXT_SECOND};">{body}</div>'
        "</details>"
    )


def _council_text_sections(meta: Mapping[str, Any]) -> list[str]:
    """Plain-text council sections: the Council block then a raw-debate note.

    The plain-text fallback shows the same Council block (already de-named, carrying
    only 'Powered by Brahmastra') and points to the HTML peek for the raw debate,
    rather than dumping the whole transcript into every mail client. Tolerant of a
    missing block (renders a placeholder, never raises).
    """
    council_block = str(meta.get("council_block") or "").strip()
    sections: list[str] = ["", "[ COUNCIL ]"]
    sections.append(council_block or "(council block unavailable)")
    sections += ["", "[ RAW DEBATE ]", "(collapsible in the HTML email)"]
    return sections


def compose_approval_email(
    draft: _DraftLike,
    sources: Sequence[SourceRef],
    signed_links: Mapping[str, str],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    """Compose the daily approval email → ``(subject, text, html)`` (BRD §14.1).

    Args:
        draft:        the verified draft (post text, quality report, image, ids).
        sources:      the source items to list for spot-checking.
        signed_links: mapping of action → freshly-minted signed URL. Must contain
                      ``approve``/``post_now``/``edit``/``reject``; a missing key
                      raises ``KeyError`` (fail-closed — never render a dead link).
        settings:     injectable config (defaults to the process singleton).
        now:          injectable "today" for deterministic subjects in tests.

    The subject is exactly ``VISION daily draft — {focus} — {date}``; the body
    reproduces the post verbatim with its character count, the quality report,
    the sources, the four action buttons, and a footer carrying the run id and
    the link expiry.
    """
    cfg = settings if settings is not None else get_settings()
    today = now if now is not None else datetime.now()

    focus = (draft.lane_focus or "daily update").strip()
    date_str = _fmt_date(today, cfg)
    subject = f"VISION daily draft — {focus} — {date_str}"

    post_text = draft.post_text or ""
    char_count = len(post_text)
    quality_lines = _quality_lines(draft.quality_report)

    # A council draft renders an extra Council block + raw-debate peek + an
    # Overrule action; everything else renders the standard news email. Resolved
    # ONCE so the text body, HTML body, and button set all agree.
    is_council = _is_council(draft)
    council_meta: Mapping[str, Any] = (
        getattr(draft, "council_meta", None) or {}
    ) if is_council else {}
    action_order = _COUNCIL_ACTION_ORDER if is_council else _ACTION_ORDER

    # --- Plain-text body (the fallback every client can read) ---------------
    text_sections: list[str] = [
        f"[ PROPOSED POST — {char_count:,} chars ]",
        post_text,
        "",
    ]
    if is_council:
        # POST first, then the Council block + a pointer to the collapsible debate.
        text_sections += _council_text_sections(council_meta)
    has_image = bool(draft.image_path) and draft.image_type != "none"
    if has_image:
        text_sections += [f"[ IMAGE — type: {draft.image_type} ]", "(inline preview in the HTML email)", ""]
    text_sections += ["[ QUALITY REPORT ]", *quality_lines, ""]
    text_sections += ["[ SOURCES ]"]
    text_sections += [f"{i}. {s.title} — {s.url}" for i, s in enumerate(sources, start=1)] or ["(none)"]
    text_sections += [
        "",
        "[ ACTIONS ]",
        *[f"{label}: {signed_links[action]}" for action, label, _ in action_order],
        "",
        f"Run {_short_id(draft.run_id)} · Links expire {_fmt_expiry(draft.token_expires_at, cfg)} today.",
    ]
    text = "\n".join(text_sections)

    # --- HTML body ----------------------------------------------------------
    body_rows: list[str] = []

    # PROPOSED POST — verbatim in a monospace-safe pre so line breaks survive.
    post_block = (
        f'<div style="color:{theme.TEXT_MUTED};font-size:11px;text-transform:uppercase;'
        f'letter-spacing:1.5px;font-weight:700;margin-bottom:8px;">Proposed post</div>'
        f'<pre style="margin:0;white-space:pre-wrap;word-wrap:break-word;'
        f"font-family:{theme._SANS};font-size:14px;line-height:1.55;"
        f'color:{theme.TEXT_PRIMARY};">{_html.escape(post_text)}</pre>'
    )
    body_rows.append(_panel_row(_inset(post_block)))

    # COUNCIL — for a council draft, the 3 unnamed viewpoints then the collapsible
    # raw-debate peek sit directly under the POST (Appendix: post → council → debate).
    if is_council:
        council_block = str(council_meta.get("council_block") or "")
        if council_block.strip():
            body_rows.append(_panel_row(_inset(_council_block_html(council_block))))
        raw_debate = _raw_debate_html(council_meta.get("transcript"))
        if raw_debate:
            body_rows.append(_panel_row(_inset(raw_debate)))

    # IMAGE preview — embedded inline as a data URI when the draft has an image.
    if has_image and draft.image_path is not None:
        data_uri = _image_data_uri(draft.image_path)
        if data_uri is not None:
            body_rows.append(
                _panel_row(
                    f'<div style="color:{theme.TEXT_MUTED};font-size:11px;text-transform:uppercase;'
                    f'letter-spacing:1.5px;font-weight:700;margin-bottom:8px;">'
                    f"Image · {_html.escape(draft.image_type)}</div>"
                    f'<img src="{data_uri}" alt="post image preview" '
                    f'style="max-width:100%;border-radius:10px;border:1px solid {theme.BORDER};" />'
                )
            )

    # QUALITY REPORT.
    body_rows.append(
        _panel_row(
            f'<div style="color:{theme.TEXT_MUTED};font-size:11px;text-transform:uppercase;'
            f'letter-spacing:1.5px;font-weight:700;margin-bottom:8px;">Quality report</div>'
            f"{_inset(_quality_html(draft.quality_report, cfg))}"
        )
    )

    # SOURCES.
    body_rows.append(
        _panel_row(
            f'<div style="color:{theme.TEXT_MUTED};font-size:11px;text-transform:uppercase;'
            f'letter-spacing:1.5px;font-weight:700;margin-bottom:8px;">Sources</div>'
            f"{_sources_html(sources)}"
        )
    )

    # ACTION BUTTONS — Approve is primary (navy); the rest gold-outlined. A council
    # draft additionally offers 'Overrule' (an edit-flow variant); the action set
    # was chosen above so the missing-link fail-closed guard covers it too.
    buttons = "".join(
        theme.button(label, signed_links[action], primary=primary, settings=cfg)
        for action, label, primary in action_order
    )
    body_rows.append(_panel_row(f'<div style="margin:6px 0;">{buttons}</div>'))

    # FOOTER — run id + expiry.
    footer = (
        f'<div style="color:{theme.TEXT_MUTED};font-size:12px;font-family:{theme._MONO};">'
        f"Run {_html.escape(_short_id(draft.run_id))} · "
        f"Links expire {_html.escape(_fmt_expiry(draft.token_expires_at, cfg))} today.</div>"
    )
    body_rows.append(_panel_row(footer))
    # A little breathing room at the bottom of the card.
    body_rows.append('<tr><td style="padding:0 0 14px;"></td></tr>')

    html = theme.wrap_shell(
        title=focus,
        subtitle=date_str,
        body="".join(body_rows),
        kpi=f"{char_count:,}c",
        settings=cfg,
    )

    return subject, text, html


def compose_confirmation_email(
    draft: _DraftLike,
    post_url: str,
    *,
    settings: Settings | None = None,
) -> tuple[str, str, str]:
    """Compose the post-publish confirmation email → ``(subject, text, html)``.

    Sent after a draft actually publishes: it confirms the live post URL so the
    owner has a receipt. No action links — nothing here mutates state.
    """
    cfg = settings if settings is not None else get_settings()
    focus = (draft.lane_focus or "daily update").strip()
    subject = f"VISION posted — {focus}"

    text = "\n".join(
        [
            "Your VISION draft is now live on LinkedIn.",
            "",
            f"View it: {post_url}",
            "",
            f"Run {_short_id(draft.run_id)}.",
        ]
    )

    body = _panel_row(
        _inset(
            f'<div style="font-size:15px;color:{theme.TEXT_PRIMARY};margin-bottom:12px;">'
            "Your draft is now live on LinkedIn.</div>"
            f"{theme.button('View post', post_url, primary=True, settings=cfg)}"
        )
    ) + _panel_row(
        f'<div style="color:{theme.TEXT_MUTED};font-size:12px;font-family:{theme._MONO};">'
        f"Run {_html.escape(_short_id(draft.run_id))}.</div>"
    )

    html = theme.wrap_shell(title=focus, subtitle="Published", body=body, kpi="LIVE", settings=cfg)
    return subject, text, html
