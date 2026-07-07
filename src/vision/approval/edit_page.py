"""HTML page rendering + edit re-validation for the approval service (BRD §14.3).

WHY this module exists: the FastAPI approval endpoints (``web.py``) must render a
handful of tiny, self-contained HTML pages — a confirmation page (so a GET can
*show* an action without performing it, per the threat model), an edit page, a
success page and a single generic "link no longer valid" error page. Keeping ALL
of that markup here (rather than inline in the route handlers) means:

  * ``web.py`` stays about *routing + security*, this module about *presentation*;
  * every page shares one navy/gold shell (re-paletted from finalert's shell,
    BRD §13.6) so branding is defined once;
  * the edit page's client-side character counter and the server-side
    length/format/compliance re-validation live next to each other.

Security notes baked into every page here:
  * No third-party assets are referenced (threat model §1 "no third-party
    assets") — all CSS is inline, so an email scanner / proxy can never pull a
    remote resource and the pages render offline.
  * Every dynamic value is HTML-escaped via :func:`_esc` before interpolation to
    stop reflected-XSS through a draft's own text.
  * The error page is deliberately GENERIC (no reason, no token, no draft id) so
    an attacker probing the endpoint learns nothing (threat model §1/§2).
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from vision.config import Settings
from vision.synthesise.quality import find_banned_phrases

# --- Content limits (config-shaped constants, single source of truth) -------
# LinkedIn's hard commentary limit is 3000 characters; a post longer than this
# is rejected by the API, so the edit page must never let one through. Kept as a
# module constant (not a magic number) so both the client counter and the
# server validator agree on the same ceiling.
LINKEDIN_MAX_CHARS: int = 3000

# A post must carry SOME text — an empty commentary is never publishable. One is
# the smallest meaningful floor; the real voice-profile minimum is enforced
# upstream at synthesis time, this is just the fail-closed guard for a hand-edit.
MIN_CHARS: int = 1

# Hashtag count window mirrors the synthesis quality rule (quality.py: 3-5). An
# edited post that drifts outside this range is flagged so the owner fixes it
# before the edited version can be approved.
MIN_HASHTAGS: int = 3
MAX_HASHTAGS: int = 5

# A small default "hype / clickbait" blocklist for the compliance re-check on a
# hand-edit. This is intentionally lightweight (NOT a full LLM critique, §14.3):
# it only catches obvious banned wording the owner might reintroduce while
# editing. The authoritative banned list is the voice profile at synthesis time.
DEFAULT_BANNED_PHRASES: tuple[str, ...] = (
    "game changer",
    "game-changer",
    "revolutionary",
    "groundbreaking",
    "guaranteed",
    "disrupt",
    "synergy",
    "10x",
)


def _esc(value: object) -> str:
    """HTML-escape any value for safe interpolation into a page.

    Centralised so no page-building f-string can forget to escape a draft field —
    a draft's ``post_text`` is attacker-influenceable content, so it must never
    reach the browser un-escaped (reflected-XSS defence).
    """
    return html.escape("" if value is None else str(value), quote=True)


@dataclass(frozen=True)
class Palette:
    """The navy/gold brand palette (BRD §13.6), parsed from settings.

    Frozen so a rendered page can never mutate the shared brand colours. Defaults
    match the BRD's ``navy=#0B1F3A;gold=#C9A24B`` so an un-configured checkout
    still renders on-brand.
    """

    navy: str = "#0B1F3A"
    gold: str = "#C9A24B"
    # Derived neutral surfaces so the shell reads as a crisp, paper-like card
    # (mirrors finalert's neutral surfaces, re-paletted to the VISION brand).
    page_bg: str = "#F4F5F7"
    card_bg: str = "#FFFFFF"
    border: str = "#E3E6EB"
    text: str = "#1A2433"
    muted: str = "#6B7280"
    danger: str = "#C62828"


def build_palette(settings: Settings) -> Palette:
    """Parse ``settings.card_brand_palette`` (``navy=..;gold=..``) into a Palette.

    Config-over-code (§22): the owner can re-brand via the env var without a code
    change. A malformed/partial value degrades gracefully to the on-brand
    defaults rather than raising — a page must always render.
    """
    parsed: dict[str, str] = {}
    for pair in settings.card_brand_palette.split(";"):
        if "=" in pair:
            key, _, val = pair.partition("=")
            parsed[key.strip().lower()] = val.strip()
    # Fall back to the frozen defaults for any missing key.
    default = Palette()
    return Palette(
        navy=parsed.get("navy", default.navy),
        gold=parsed.get("gold", default.gold),
    )


def render_shell(*, title: str, subtitle: str, body_html: str, palette: Palette) -> str:
    """Wrap page ``body_html`` in the shared navy/gold VISION shell.

    One gold top strip, a navy header band, a white body card and a muted footer
    — the VISION re-palette of finalert's ``wrap_shell``. ``title``/``subtitle``
    are escaped; ``body_html`` is trusted markup assembled by this module's own
    render functions (never raw user input).
    """
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{_esc(title)}</title>
</head>
<body style="margin:0;padding:0;background:{palette.page_bg};
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
color:{palette.text};">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{palette.page_bg};padding:28px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
 style="max-width:600px;width:100%;background:{palette.card_bg};
 border:1px solid {palette.border};border-radius:14px;overflow:hidden;">
  <tr><td style="height:6px;background:{palette.gold};"></td></tr>
  <tr><td style="padding:22px 30px 16px;background:{palette.navy};">
    <div style="color:{palette.gold};font-size:11px;letter-spacing:3px;
     text-transform:uppercase;font-weight:700;">VISION · Brahmastra</div>
    <div style="color:#FFFFFF;font-size:21px;font-weight:700;margin-top:6px;">{_esc(title)}</div>
    <div style="color:#C9D2E0;font-size:13px;margin-top:4px;">{_esc(subtitle)}</div>
  </td></tr>
  <tr><td style="padding:24px 30px 28px;">{body_html}</td></tr>
  <tr><td style="background:{palette.navy};padding:12px 30px;">
    <div style="color:#9FB0C6;font-size:11px;line-height:1.6;">
      This link acts on your LinkedIn profile. It is single-use and expires.
    </div>
  </td></tr>
  <tr><td style="height:6px;background:{palette.gold};"></td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _button(label: str, palette: Palette) -> str:
    """Return the shared gold submit-button markup used by every POST form."""
    return (
        f'<button type="submit" style="display:inline-block;background:{palette.gold};'
        f'color:{palette.navy};border:none;border-radius:8px;padding:12px 22px;'
        f'font-size:15px;font-weight:700;cursor:pointer;">{_esc(label)}</button>'
    )


def _draft_preview(*, post_text: str, hashtags: list[str], palette: Palette) -> str:
    """Render a read-only preview block of the draft's text + hashtags."""
    tags = " ".join(hashtags) if hashtags else ""
    return (
        f'<div style="background:{palette.page_bg};border:1px solid {palette.border};'
        f'border-radius:10px;padding:16px 18px;white-space:pre-wrap;font-size:14px;'
        f'line-height:1.55;color:{palette.text};">{_esc(post_text)}</div>'
        f'<div style="margin-top:8px;color:{palette.navy};font-size:13px;'
        f'font-weight:600;">{_esc(tags)}</div>'
    )


def render_confirmation_page(
    *,
    action: str,
    action_label: str,
    post_text: str,
    hashtags: list[str],
    token: str,
    action_url: str,
    extra_note: str,
    palette: Palette,
) -> str:
    """Render the GET confirmation page: a preview + a POST form (no mutation).

    This is the crux of the threat-model rule "GET displays confirmation only;
    state change requires POST" (§1/§22). The page performs NO action — it only
    shows what *will* happen and offers a POST button that carries the token in a
    hidden field (so the token stays out of the browser address bar / Referer on
    the subsequent request).
    """
    note_html = (
        f'<div style="margin-top:12px;color:{palette.muted};font-size:13px;">'
        f"{_esc(extra_note)}</div>"
        if extra_note
        else ""
    )
    body = f"""
<p style="margin:0 0 14px;font-size:15px;">You are about to
 <b>{_esc(action_label)}</b> this post. Nothing has changed yet — press the button
 below to confirm.</p>
{_draft_preview(post_text=post_text, hashtags=hashtags, palette=palette)}
{note_html}
<form method="post" action="{_esc(action_url)}" style="margin-top:22px;">
  <input type="hidden" name="token" value="{_esc(token)}">
  <input type="hidden" name="action" value="{_esc(action)}">
  {_button(action_label, palette)}
</form>
"""
    return render_shell(
        title=f"Confirm: {action_label}",
        subtitle="One-click confirmation",
        body_html=body,
        palette=palette,
    )


def render_edit_page(
    *,
    post_text: str,
    hashtags: list[str],
    token: str,
    action_url: str,
    errors: list[str] | None = None,
    prompt: str | None = None,
    palette: Palette,
) -> str:
    """Render the editable draft page with a live character counter (§14.3).

    Pre-fills the post text and hashtags, shows an inline JS character count
    against :data:`LINKEDIN_MAX_CHARS`, and posts back to ``action_url`` with an
    "Approve edited" button. Any server-side validation ``errors`` from a prior
    failed submit are shown at the top so the owner can fix and resubmit — the
    token is NOT consumed on a validation failure, so the same link still works.

    ``prompt`` is an OPTIONAL neutral instruction banner shown above the form. It
    is reused by the council OVERRULE flow to label the page ("Add your override:")
    so the owner understands this edit is an override of the council's synthesis —
    the machinery is otherwise identical to a normal edit (no separate page/endpoint).
    """
    hashtags_text = " ".join(hashtags) if hashtags else ""
    # An optional neutral (non-error) instruction banner — e.g. the overrule prompt.
    prompt_html = ""
    if prompt:
        prompt_html = (
            f'<div style="background:{palette.card_bg};border:1px solid {palette.border};'
            f'border-radius:8px;padding:12px 16px;margin-bottom:16px;color:{palette.navy};'
            f'font-size:14px;font-weight:600;">{_esc(prompt)}</div>'
        )
    errors_html = ""
    if errors:
        items = "".join(f"<li>{_esc(e)}</li>" for e in errors)
        errors_html = (
            f'<div style="background:#FDECEC;border:1px solid {palette.danger};'
            f'border-radius:8px;padding:12px 16px;margin-bottom:16px;color:{palette.danger};'
            f'font-size:14px;"><b>Please fix before approving:</b>'
            f'<ul style="margin:8px 0 0 18px;padding:0;">{items}</ul></div>'
        )
    # NOTE on the inline <script>: braces are doubled to survive the f-string.
    # The counter is purely advisory UX; the SERVER re-validates on POST, so a
    # client with JS disabled is still safe (fail-closed on the server).
    body = f"""
{prompt_html}
{errors_html}
<form method="post" action="{_esc(action_url)}">
  <input type="hidden" name="token" value="{_esc(token)}">
  <input type="hidden" name="action" value="edit">
  <label for="post_text" style="display:block;font-size:13px;font-weight:600;
   color:{palette.navy};margin-bottom:6px;">Post text</label>
  <textarea id="post_text" name="post_text" rows="12"
   style="width:100%;box-sizing:border-box;border:1px solid {palette.border};
   border-radius:10px;padding:12px;font-size:14px;line-height:1.55;
   font-family:inherit;color:{palette.text};">{_esc(post_text)}</textarea>
  <div style="margin:6px 2px 16px;font-size:12px;text-align:right;">
    <span id="cc" style="font-family:ui-monospace,Menlo,monospace;
     color:{palette.navy};"></span></div>
  <label for="hashtags" style="display:block;font-size:13px;font-weight:600;
   color:{palette.navy};margin-bottom:6px;">Hashtags (space-separated)</label>
  <input id="hashtags" name="hashtags" value="{_esc(hashtags_text)}"
   style="width:100%;box-sizing:border-box;border:1px solid {palette.border};
   border-radius:10px;padding:12px;font-size:14px;font-family:inherit;
   color:{palette.text};">
  <div style="margin-top:22px;">{_button('Approve edited', palette)}</div>
</form>
<script>
  var ta = document.getElementById('post_text');
  var cc = document.getElementById('cc');
  var max = {LINKEDIN_MAX_CHARS};
  function updateCount() {{
    var n = ta.value.length;
    cc.textContent = n + ' / ' + max + ' characters';
    cc.style.color = (n > max) ? '{palette.danger}' : '{palette.navy}';
  }}
  ta.addEventListener('input', updateCount);
  updateCount();
</script>
"""
    return render_shell(
        title="Edit post",
        subtitle="Revise, then approve the edited version",
        body_html=body,
        palette=palette,
    )


def render_result_page(*, heading: str, message: str, palette: Palette) -> str:
    """Render a simple success page after a POST action completes."""
    body = (
        f'<div style="text-align:center;padding:16px 0;">'
        f'<div style="font-size:40px;line-height:1;color:{palette.gold};">&#10003;</div>'
        f'<h2 style="margin:14px 0 8px;font-size:20px;color:{palette.navy};">{_esc(heading)}</h2>'
        f'<p style="margin:0;font-size:15px;color:{palette.muted};">{_esc(message)}</p>'
        f"</div>"
    )
    return render_shell(
        title=heading, subtitle="Done", body_html=body, palette=palette
    )


def render_error_page(*, palette: Palette) -> str:
    """Render the single GENERIC error page (threat model §1/§2).

    Deliberately says nothing about *why* the link failed (expired vs. replayed
    vs. tampered) and echoes no token/draft id — an attacker probing the endpoint
    gets a uniform response, and a real owner gets a clear next step.
    """
    body = (
        f'<div style="text-align:center;padding:16px 0;">'
        f'<h2 style="margin:0 0 8px;font-size:20px;color:{palette.navy};">'
        f"This link is no longer valid</h2>"
        f'<p style="margin:0;font-size:15px;color:{palette.muted};">'
        f"It may have expired, already been used, or be incorrect. "
        f"Please use the most recent approval email, or wait for the next one.</p>"
        f"</div>"
    )
    return render_shell(
        title="Link no longer valid",
        subtitle="",
        body_html=body,
        palette=palette,
    )


def validate_edited_post(
    post_text: str, hashtags: list[str], settings: Settings
) -> list[str]:
    """Re-run length / format / compliance checks on a hand-edited post (§14.3).

    Returns a list of human-readable problems (empty ⇒ the edit is acceptable).
    This is the NON-LLM re-validation the BRD requires before an edited post may
    be approved: it is pure and deterministic (no model call, no I/O) so the same
    edit always yields the same verdict.

    Checks:
      * length — non-empty and within LinkedIn's hard 3000-char ceiling;
      * format — hashtag count in the 3-5 window and each tag well-formed
        (``#`` prefix, no embedded whitespace);
      * compliance — no obvious banned "hype" phrase reintroduced by the edit.

    ``settings`` is accepted for forward-compat (e.g. a future configurable
    ceiling) and to keep the signature uniform with the rest of the layer.
    """
    problems: list[str] = []

    # --- Length -----------------------------------------------------------
    text = post_text.strip()
    if len(text) < MIN_CHARS:
        problems.append("Post text cannot be empty.")
    if len(post_text) > LINKEDIN_MAX_CHARS:
        problems.append(
            f"Post is {len(post_text)} characters — the maximum is {LINKEDIN_MAX_CHARS}."
        )

    # --- Format (hashtags) ------------------------------------------------
    count = len(hashtags)
    if count < MIN_HASHTAGS or count > MAX_HASHTAGS:
        problems.append(
            f"Use between {MIN_HASHTAGS} and {MAX_HASHTAGS} hashtags "
            f"(found {count})."
        )
    for tag in hashtags:
        # A well-formed tag starts with '#', has body after it, and contains no
        # whitespace (a space means two tags were merged / mistyped).
        if not tag.startswith("#") or len(tag) < 2 or any(ch.isspace() for ch in tag):
            problems.append(f"Malformed hashtag: {tag!r}")

    # --- Compliance (banned hype phrases) ---------------------------------
    banned_hit = find_banned_phrases(post_text, list(DEFAULT_BANNED_PHRASES))
    if banned_hit:
        problems.append(
            "Remove non-compliant phrasing: " + ", ".join(banned_hit) + "."
        )

    return problems
