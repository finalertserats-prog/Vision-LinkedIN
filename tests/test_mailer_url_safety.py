"""Regression tests for href safety in the approval/confirmation emails.

Covers the high-severity Codex finding: untrusted source URLs and the published
post URL were interpolated into HTML href attributes without escaping or scheme
validation, allowing attribute break-out / javascript: injection. See
vision.mailer.theme.safe_url.
"""

from __future__ import annotations

from vision.mailer import theme


def test_safe_url_collapses_javascript_scheme_to_dead_link() -> None:
    # Arrange / Act
    result = theme.safe_url("javascript:alert(1)")
    # Assert — a non-http(s) scheme is never emitted as a live link.
    assert result == "#"


def test_safe_url_collapses_data_scheme() -> None:
    assert theme.safe_url("data:text/html,<script>alert(1)</script>") == "#"


def test_safe_url_escapes_attribute_breakout_quote() -> None:
    # Arrange — a URL crafted to break out of href="..." and inject a handler.
    hostile = 'https://x.com/a" onmouseover="alert(1)'
    # Act
    result = theme.safe_url(hostile)
    # Assert — no raw double-quote survives to close the attribute early.
    assert '"' not in result
    assert "&quot;" in result


def test_safe_url_passes_through_clean_https_link_unchanged() -> None:
    # A signed, server-minted link (base64url token) has no escapable characters.
    url = "https://vps.example/approve?token=abc-DEF_123.signaturepart"
    assert theme.safe_url(url) == url


def test_button_href_is_scheme_checked() -> None:
    # Act — the confirmation "View post" link uses the untrusted post URL.
    html = theme.button("View post", "javascript:alert(1)", primary=True)
    # Assert — the live javascript href is neutralised to '#'.
    assert 'href="#"' in html
    assert "javascript:" not in html


def test_button_keeps_valid_https_href() -> None:
    url = "https://www.linkedin.com/feed/update/urn:li:share:12345"
    html = theme.button("View post", url)
    assert f'href="{url}"' in html
