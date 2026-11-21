"""Tests for the LinkedIn re-auth email: one email per incident, with a fix link.

Incident 2026-11-21: an expired token made the 5-minute publisher email the owner
on every tick. The owner wants exactly one email per incident, carrying a link
that fixes it without a terminal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from vision.config import Settings, get_settings
from vision.publish.reauth_alert import (
    SUBJECT_EXPIRES_SOON,
    SUBJECT_REAUTH_REQUIRED,
    clear_reauth_incidents,
    send_reauth_alert_once,
)
from vision.publish.reauth_link import (
    REAUTH_START_PATH,
    is_valid_reauth_link_token,
    issue_reauth_link_token,
)

_NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
_SECRET = "unit-test-hmac-secret"  # noqa: S105 - test placeholder
_BASE_URL = "https://vision.example.test"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with a known HMAC secret and a public base URL for links."""
    monkeypatch.setenv("VISION_APPROVAL_BASE_URL", _BASE_URL)
    get_settings.cache_clear()
    return get_settings().model_copy(update={"secret_hmac_key": _SECRET})


def _send(session: Session, sender: Mock, settings: Settings, now: datetime, subject: str) -> bool:
    return send_reauth_alert_once(
        session,
        sender=sender,
        settings=settings,
        now=now,
        subject=subject,
        reason="LinkedIn rejected the stored token.",
    )


# --- Signed link -----------------------------------------------------------


def test_fresh_link_token_is_valid() -> None:
    token = issue_reauth_link_token(_SECRET, _NOW)
    assert is_valid_reauth_link_token(token, _SECRET, _NOW + timedelta(days=1))


def test_link_token_is_rejected_after_it_expires() -> None:
    token = issue_reauth_link_token(_SECRET, _NOW, ttl=timedelta(days=14))
    assert not is_valid_reauth_link_token(token, _SECRET, _NOW + timedelta(days=15))


def test_link_token_signed_with_another_secret_is_rejected() -> None:
    token = issue_reauth_link_token("some-other-secret", _NOW)
    assert not is_valid_reauth_link_token(token, _SECRET, _NOW)


def test_link_token_with_extended_expiry_is_rejected() -> None:
    expiry, _, signature = issue_reauth_link_token(_SECRET, _NOW).partition(".")
    forged = f"{int(expiry) + 86_400 * 365}.{signature}"
    assert not is_valid_reauth_link_token(forged, _SECRET, _NOW)


@pytest.mark.parametrize("garbage", ["", "abc", "123", "x.y", "１２３.sig", "123.ﬁ"])
def test_malformed_link_token_is_rejected(garbage: str) -> None:
    assert not is_valid_reauth_link_token(garbage, _SECRET, _NOW)


# --- One email per incident --------------------------------------------------


def test_repeated_alerts_in_one_incident_send_one_email(
    db_session: Session, settings: Settings
) -> None:
    sender = Mock()
    for hours in (0, 1, 30, 24 * 20):
        _send(db_session, sender, settings, _NOW + timedelta(hours=hours), SUBJECT_REAUTH_REQUIRED)
    assert sender.send.call_count == 1


def test_email_carries_a_working_fix_link(db_session: Session, settings: Settings) -> None:
    sender = Mock()
    _send(db_session, sender, settings, _NOW, SUBJECT_REAUTH_REQUIRED)
    html = sender.send.call_args.args[2]
    token = html.split(f'href="{_BASE_URL}{REAUTH_START_PATH}?t=')[1].split('"')[0]
    assert is_valid_reauth_link_token(token, _SECRET, _NOW)


def test_heads_up_and_expired_are_separate_single_emails(
    db_session: Session, settings: Settings
) -> None:
    sender = Mock()
    for subject in (SUBJECT_EXPIRES_SOON, SUBJECT_EXPIRES_SOON, SUBJECT_REAUTH_REQUIRED):
        _send(db_session, sender, settings, _NOW, subject)
    assert sender.send.call_count == 2


def test_reconnecting_starts_a_new_incident(db_session: Session, settings: Settings) -> None:
    sender = Mock()
    _send(db_session, sender, settings, _NOW, SUBJECT_REAUTH_REQUIRED)
    clear_reauth_incidents(db_session)
    _send(db_session, sender, settings, _NOW + timedelta(days=60), SUBJECT_REAUTH_REQUIRED)
    assert sender.send.call_count == 2


def test_oversized_link_expiry_is_rejected_not_raised() -> None:
    assert not is_valid_reauth_link_token("9" * 5000 + ".sig", _SECRET, _NOW)

