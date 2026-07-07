"""Tests for the VISION mailer (BRD §14.1, §14.5; threat model §1, §4).

All tests follow AAA (Arrange → Act → Assert) and mock every external boundary —
SMTP is patched, the Resend HTTP call is intercepted with ``respx``, and no real
email or network I/O ever happens (BRD §18: mock external deps, tests are done).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from vision.config import Settings
from vision.mailer import theme
from vision.mailer.composer import (
    SourceRef,
    compose_approval_email,
    compose_confirmation_email,
)
from vision.mailer.dedup import SendDeduper
from vision.mailer.sender import (
    ResendSender,
    SMTPSender,
    _RESEND_ENDPOINT,
    get_sender,
)

# A fixed reference time so subject/date rendering is deterministic (IST).
_IST = timezone(timedelta(hours=5, minutes=30))
_NOW = datetime(2026, 7, 6, 8, 0, 0, tzinfo=_IST)


def _settings(**overrides: object) -> Settings:
    """Build a Settings object for tests without reading a real .env.

    ``_env_file=None`` isolates the test from any repo/dev ``.env`` so the
    provider/credentials under test are exactly the overrides given.
    """
    # Settings fields carry aliases (EMAIL_PROVIDER, …) and the model ignores
    # unknown keys, so init MUST use the aliases or the value is silently dropped.
    base: dict[str, object] = {
        "EMAIL_PROVIDER": "smtp",
        "EMAIL_FROM": "vision@example.com",
        "EMAIL_TO": "owner@example.com",
        "EMAIL_API_KEY": "app-password-value",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _draft(**overrides: object) -> SimpleNamespace:
    """A lightweight structural stub matching the composer's ``_DraftLike`` shape."""
    fields: dict[str, object] = {
        "id": "7f3a1b2c-0000-4000-8000-000000000001",
        "run_id": "7f3a9999-0000-4000-8000-0000000000ff",
        "lane_focus": "AI in clinical ops",
        "post_text": "Most AI wins are workflows, not models.\n\nHere is why.",
        "quality_report": {
            "char_count": 52,
            "has_hook": True,
            "grounding_pct": 100,
            "unsupported_claims": [],
            "dedup_vs_own_90d": {"max_similarity": 0.31, "pass": True},
            "tone_flags": [],
            "compliance_flags": [],
            "hashtags": ["#HealthTech", "#AIinHealthcare"],
            "confidence": 0.86,
        },
        "confidence": 0.86,
        "token_expires_at": datetime(2026, 7, 6, 20, 0, 0, tzinfo=_IST),
        "image_type": "none",
        "image_path": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


_LINKS = {
    "approve": "https://vision.example/a/approve/TOKEN",
    "post_now": "https://vision.example/a/post_now/TOKEN",
    "edit": "https://vision.example/a/edit/TOKEN",
    "reject": "https://vision.example/a/reject/TOKEN",
}

# A council draft additionally offers an 'overrule' link (the edit-flow variant).
_COUNCIL_LINKS = {**_LINKS, "overrule": "https://vision.example/a/overrule/TOKEN"}


def _council_draft(**overrides: object) -> SimpleNamespace:
    """A council draft stub: content_mode='council' + a populated council_meta.

    The ``council_block`` mirrors the de-named composer output (3 unnamed viewpoints
    + the single 'Powered by Brahmastra' line), and the ``transcript`` is the raw
    per-voice debate carried only for the owner's review peek — never published.
    """
    fields: dict[str, object] = {
        "content_mode": "council",
        "council_meta": {
            "topic": "Should hospitals trust unexplainable AI?",
            "format": "show_the_split",
            "situation": "disagreed — one prized speed, another safety",
            "council_block": (
                "• Move fast, the upside is huge\n"
                "• Slow down, the downside is irreversible\n"
                "• The real risk is pretending it's binary\n"
                "Powered by Brahmastra"
            ),
            "transcript": {
                "Gemini": {"round1": "Gemini's take one", "round2": "Gemini holds"},
                "Codex": {"round1": "Codex's take one", "round2": "Codex sharpens"},
                "Claude": {"round1": "Claude's take one", "round2": "Claude shifts"},
            },
        },
    }
    fields.update(overrides)
    return _draft(**fields)


# ---------------------------------------------------------------------------
# SMTPSender — builds correct MIME and calls smtplib (mocked, no real send).
# ---------------------------------------------------------------------------


def test_smtp_sender_builds_multipart_mime_and_calls_smtplib() -> None:
    # Arrange: an SMTP sender on the STARTTLS path with a patched smtplib.SMTP.
    sender = SMTPSender(
        host="smtp.example.com",
        port=587,
        username="vision@example.com",
        password="app-password-value",
        from_addr="vision@example.com",
        default_to="owner@example.com",
    )
    server = MagicMock()
    with patch("vision.mailer.sender.smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value.__enter__.return_value = server

        # Act
        ok = sender.send("Subject line", "plain body", "<b>html body</b>")

    # Assert: STARTTLS handshake happened and one message was sent.
    assert ok is True
    server.starttls.assert_called_once()
    server.login.assert_called_once()
    server.sendmail.assert_called_once()
    from_arg, to_arg, raw = server.sendmail.call_args.args
    assert from_arg == "vision@example.com"
    assert to_arg == ["owner@example.com"]
    # The wire message is multipart/alternative with BOTH parts present.
    assert "Subject: Subject line" in raw
    assert 'Content-Type: multipart/alternative' in raw
    assert "text/plain" in raw and "text/html" in raw


def test_smtp_sender_uses_ssl_on_port_465() -> None:
    # Arrange: port 465 must take the implicit-SSL branch (SMTP_SSL, no STARTTLS).
    sender = SMTPSender(
        host="smtp.example.com",
        port=465,
        username="vision@example.com",
        password="app-password-value",
        from_addr="vision@example.com",
        default_to="owner@example.com",
    )
    server = MagicMock()
    with patch("vision.mailer.sender.smtplib.SMTP_SSL") as ssl_cls:
        ssl_cls.return_value.__enter__.return_value = server

        # Act
        ok = sender.send("S", "t", "<i>h</i>")

    # Assert
    assert ok is True
    ssl_cls.assert_called_once()
    server.sendmail.assert_called_once()


def test_smtp_sender_disabled_without_password_returns_false() -> None:
    # Arrange: no credential ⇒ inert sender (dev checkout degrades safely).
    sender = SMTPSender(
        host="h", port=587, username="u", password="",
        from_addr="vision@example.com", default_to="owner@example.com",
    )
    # Act
    ok = sender.send("s", "t", "h")
    # Assert
    assert ok is False


def test_smtp_auth_error_returns_false_without_leaking_password(caplog: pytest.LogCaptureFixture) -> None:
    # Arrange: login raises the auth error; the secret must not reach the logs.
    import smtplib

    sender = SMTPSender(
        host="h", port=587, username="u", password="super-secret-app-pw",
        from_addr="vision@example.com", default_to="owner@example.com",
    )
    server = MagicMock()
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
    with patch("vision.mailer.sender.smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value.__enter__.return_value = server
        # Act
        with caplog.at_level("ERROR"):
            ok = sender.send("s", "t", "h")

    # Assert: handled (False), and the password never appears in any log line.
    assert ok is False
    assert "super-secret-app-pw" not in caplog.text


# ---------------------------------------------------------------------------
# ResendSender — posts the correct payload (respx-mocked HTTP).
# ---------------------------------------------------------------------------


@respx.mock
def test_resend_sender_posts_expected_payload() -> None:
    # Arrange: intercept the Resend endpoint and capture the request.
    route = respx.post(_RESEND_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"id": "email_123"})
    )
    sender = ResendSender(
        api_key="re_test_key",
        from_addr="vision@example.com",
        default_to="owner@example.com",
    )

    # Act
    ok = sender.send("Daily draft", "plain", "<h1>rich</h1>")

    # Assert: accepted, and the JSON payload carries all fields + both bodies.
    assert ok is True
    assert route.called
    request = route.calls.last.request
    import json as _json

    sent = _json.loads(request.content)
    assert sent["from"] == "vision@example.com"
    assert sent["to"] == ["owner@example.com"]
    assert sent["subject"] == "Daily draft"
    assert sent["html"] == "<h1>rich</h1>"
    assert sent["text"] == "plain"
    # The API key travels only in the Authorization header (never in the body).
    assert request.headers["authorization"] == "Bearer re_test_key"
    assert "re_test_key" not in sent.values()


@respx.mock
def test_resend_sender_returns_false_on_non_2xx() -> None:
    # Arrange: provider rejects the send.
    respx.post(_RESEND_ENDPOINT).mock(return_value=httpx.Response(422, json={"error": "bad"}))
    sender = ResendSender(api_key="re_test_key", from_addr="v@x.com", default_to="o@x.com")
    # Act
    ok = sender.send("s", "t", "h")
    # Assert
    assert ok is False


# ---------------------------------------------------------------------------
# Factory — selects the provider by settings.EMAIL_PROVIDER.
# ---------------------------------------------------------------------------


def test_get_sender_selects_smtp() -> None:
    # Arrange / Act
    sender = get_sender(_settings(EMAIL_PROVIDER="smtp"))
    # Assert
    assert isinstance(sender, SMTPSender)


def test_get_sender_selects_resend() -> None:
    # Arrange / Act
    sender = get_sender(_settings(EMAIL_PROVIDER="resend"))
    # Assert
    assert isinstance(sender, ResendSender)


def test_get_sender_rejects_unknown_provider() -> None:
    # Arrange / Act / Assert: an unknown provider fails loudly (fail-closed).
    with pytest.raises(ValueError):
        get_sender(_settings(EMAIL_PROVIDER="carrier-pigeon"))


# ---------------------------------------------------------------------------
# Composer — renders every §14.1 section, buttons, char count, quality report.
# ---------------------------------------------------------------------------


def test_compose_approval_subject_matches_brd_format() -> None:
    # Arrange
    cfg = _settings()
    # Act
    subject, _text, _html = compose_approval_email(
        _draft(), [SourceRef("A study", "https://s/1")], _LINKS, settings=cfg, now=_NOW
    )
    # Assert: exact BRD subject shape "VISION daily draft — {focus} — {date}".
    assert subject == "VISION daily draft — AI in clinical ops — 6 Jul 2026"


def test_compose_approval_renders_all_sections_and_buttons() -> None:
    # Arrange
    cfg = _settings()
    sources = [SourceRef("STAT News piece", "https://s/1"), SourceRef("Import AI", "https://s/2")]
    draft = _draft()

    # Act
    _subject, text, html = compose_approval_email(draft, sources, _LINKS, settings=cfg, now=_NOW)

    # Assert (plain text): post text, char count, quality, sources, action links.
    assert draft.post_text in text
    assert f"{len(draft.post_text):,} chars" in text
    assert "QUALITY REPORT" in text
    assert "Grounding: 100%" in text
    assert "STAT News piece" in text and "https://s/1" in text
    # Every signed action link is present.
    for url in _LINKS.values():
        assert url in text

    # Assert (HTML): all four buttons render as anchors to the signed links.
    for url in _LINKS.values():
        assert f'href="{url}"' in html
    assert "Approve &amp; schedule 09:00" in html  # primary button label (escaped)
    assert "Sources" in html and "Quality report" in html
    # Char-count KPI is shown in the shell header (computed from the real text).
    assert f"{len(draft.post_text):,}c" in html


def test_compose_approval_escapes_untrusted_post_text() -> None:
    # Arrange: a draft whose text contains markup must not inject into the HTML.
    draft = _draft(post_text="<script>alert(1)</script> hello")
    # Act
    _s, _t, html = compose_approval_email(draft, [], _LINKS, settings=_settings(), now=_NOW)
    # Assert: the script tag is escaped, not live.
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_compose_approval_missing_link_fails_closed() -> None:
    # Arrange: a links map missing an action must raise, never render a dead link.
    partial = {k: v for k, v in _LINKS.items() if k != "reject"}
    # Act / Assert
    with pytest.raises(KeyError):
        compose_approval_email(_draft(), [], partial, settings=_settings(), now=_NOW)


def test_compose_approval_embeds_image_preview(tmp_path) -> None:
    # Arrange: a real (tiny) image file the composer should inline as a data URI.
    img = tmp_path / "card.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nFAKEPNGBYTES")
    draft = _draft(image_type="informative-card", image_path=str(img))

    # Act
    _s, text, html = compose_approval_email(draft, [], _LINKS, settings=_settings(), now=_NOW)

    # Assert: the HTML carries an inline base64 preview; the text notes the image.
    expected_b64 = base64.b64encode(img.read_bytes()).decode("ascii")
    assert f"data:image/png;base64,{expected_b64}" in html
    assert "IMAGE — type: informative-card" in text


def test_compose_confirmation_email_contains_post_url() -> None:
    # Arrange / Act
    subject, text, html = compose_confirmation_email(
        _draft(), "https://linkedin.com/feed/post/123", settings=_settings()
    )
    # Assert
    assert subject == "VISION posted — AI in clinical ops"
    assert "https://linkedin.com/feed/post/123" in text
    assert 'href="https://linkedin.com/feed/post/123"' in html


# ---------------------------------------------------------------------------
# Composer — council draft: post + Council block + raw-debate peek + Overrule.
# ---------------------------------------------------------------------------


def test_council_email_renders_post_council_block_and_overrule_button() -> None:
    # Arrange: a council draft with the extra council_meta + an overrule link.
    draft = _council_draft(post_text="We keep pretending there is one right answer.")

    # Act.
    _subject, text, html = compose_approval_email(
        draft, [], _COUNCIL_LINKS, settings=_settings(), now=_NOW
    )

    # Assert (HTML): the POST, the Council block (all 3 viewpoints), and the
    # Overrule button linking to the signed overrule endpoint all render.
    assert "one right answer" in html
    assert "Council" in html
    assert "The real risk is pretending it&#x27;s binary" in html  # escaped apostrophe
    assert "Overrule" in html
    assert f'href="{_COUNCIL_LINKS["overrule"]}"' in html
    # The raw debate is offered as a collapsible peek.
    assert "<details" in html and "Raw debate" in html

    # Assert (plain text): the Council block appears for non-HTML clients too.
    assert "[ COUNCIL ]" in text
    assert "Move fast, the upside is huge" in text
    assert _COUNCIL_LINKS["overrule"] in text


def test_council_email_escapes_transcript_and_council_names() -> None:
    # Arrange: a transcript whose voice text contains markup must be neutralised,
    # and voice NAMES that appear only in the internal transcript must be escaped
    # (never rendered as live markup) in the owner-facing peek.
    draft = _council_draft()
    meta = dict(draft.council_meta)
    meta["transcript"] = {
        "Gemini": {"round1": "<script>steal()</script> speed matters"},
    }
    draft.council_meta = meta

    # Act.
    _s, _t, html = compose_approval_email(
        draft, [], _COUNCIL_LINKS, settings=_settings(), now=_NOW
    )

    # Assert: the injected script is escaped (inert), not live.
    assert "<script>steal()</script>" not in html
    assert "&lt;script&gt;steal()&lt;/script&gt;" in html


def test_council_email_missing_overrule_link_fails_closed() -> None:
    # Arrange: a council links map lacking 'overrule' must raise, never a dead link.
    partial = {k: v for k, v in _COUNCIL_LINKS.items() if k != "overrule"}
    # Act / Assert.
    with pytest.raises(KeyError):
        compose_approval_email(_council_draft(), [], partial, settings=_settings(), now=_NOW)


def test_news_draft_has_no_council_block_or_overrule() -> None:
    # Arrange: a plain news draft (no content_mode/council_meta).
    # Act.
    _s, text, html = compose_approval_email(
        _draft(), [], _LINKS, settings=_settings(), now=_NOW
    )
    # Assert: the council-only surfaces are absent; the 4 news buttons stand.
    assert "[ COUNCIL ]" not in text
    assert "Overrule" not in html
    assert "Raw debate" not in html


# ---------------------------------------------------------------------------
# Theme — palette parsing is config-driven (navy/gold).
# ---------------------------------------------------------------------------


def test_parse_palette_reads_navy_and_gold() -> None:
    # Arrange / Act
    pal = theme.parse_palette("navy=#0B1F3A;gold=#C9A24B")
    # Assert
    assert pal.navy == "#0B1F3A"
    assert pal.gold == "#C9A24B"


def test_parse_palette_falls_back_on_malformed_spec() -> None:
    # Arrange / Act: a broken spec must still yield brand defaults, not crash.
    pal = theme.parse_palette("garbage;;navy=")
    # Assert
    assert pal.navy == "#0B1F3A" and pal.gold == "#C9A24B"


# ---------------------------------------------------------------------------
# Dedup — suppresses a second identical send within the window.
# ---------------------------------------------------------------------------


def test_dedup_suppresses_second_identical_send(tmp_path) -> None:
    # Arrange
    deduper = SendDeduper(tmp_path / "dedup.json")
    key = "VISION daily draft — AI in clinical ops — 6 Jul 2026"

    # Act / Assert: first send is allowed, then recorded, then suppressed.
    assert deduper.is_suppressed(key) is False
    deduper.mark_sent(key)
    assert deduper.is_suppressed(key) is True


def test_dedup_state_survives_a_new_instance(tmp_path) -> None:
    # Arrange: mark on one instance; a fresh instance (a re-run) reads the file.
    path = tmp_path / "dedup.json"
    SendDeduper(path).mark_sent("key-1")
    # Act
    reloaded = SendDeduper(path)
    # Assert
    assert reloaded.is_suppressed("key-1") is True


def test_dedup_expires_after_window(tmp_path) -> None:
    # Arrange: a 10s window; a mark 20s in the past must no longer suppress.
    deduper = SendDeduper(tmp_path / "dedup.json", window_secs=10)
    now = 1_000_000.0
    deduper.mark_sent("key", now=now - 20)
    # Act / Assert
    assert deduper.is_suppressed("key", now=now) is False


def test_dedup_does_not_mark_on_check(tmp_path) -> None:
    # Arrange: is_suppressed must be a pure read (check/mark split).
    deduper = SendDeduper(tmp_path / "dedup.json")
    # Act
    deduper.is_suppressed("key")
    # Assert: nothing recorded, so a real send is still allowed.
    assert deduper.is_suppressed("key") is False
