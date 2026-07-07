"""Unit tests for the LinkedIn token-refresh job (BRD §15.3, FR-17).

WHY these tests / how they stay hermetic (BRD §18):
  * ``LinkedInClient`` is replaced by a tiny in-memory stub — **no real network,
    no real refresh call** ever leaves the process.
  * The email sender is a ``Mock`` — no real mail is sent; we only assert an
    alert *would* be sent.
  * The DB is the in-memory SQLite ``db_session`` fixture from ``conftest``.
  * The lock directory is a pytest ``tmp_path`` so lock files never touch a real
    shared dir and each test is isolated.

Every test follows AAA (Arrange → Act → Assert) with a single behavioural focus.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from vision.cli import token as token_cli
from vision.config import Settings, VisionEnv, get_settings
from vision.db.models import AuditLog, OAuthToken
from vision.publish import crypto
from vision.publish.errors import NeedsReauth, TransientLinkedInError
from vision.publish.token_refresh import (
    STATUS_DEAD_LETTERED,
    STATUS_HEALTHY,
    STATUS_REAUTH_ALERTED,
    STATUS_REFRESHED,
    STATUS_SKIPPED_LOCKED,
    decrypt_token,
    encrypt_token,
    refresh_if_needed,
)

# --- Deterministic test constants ------------------------------------------
# Fixed reference "now" so every expiry-window comparison is fully deterministic.
_NOW: datetime = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
# Obviously-fake key material and token values — nothing here is a real secret.
_ENC_KEY = "unit-test-token-enc-key"  # noqa: S105 - test placeholder
_OLD_ACCESS = "old-access-token-value"  # noqa: S105 - test placeholder
_OLD_REFRESH = "old-refresh-token-value"  # noqa: S106 - test placeholder
_NEW_ACCESS = "brand-new-access-token"  # noqa: S105 - test placeholder
_NEW_REFRESH = "brand-new-refresh-token"  # noqa: S106 - test placeholder


class _StubLinkedInClient:
    """Minimal stand-in for ``LinkedInClient`` capturing refresh calls.

    ``result`` is either a token-JSON dict to return or an exception instance to
    raise. ``calls`` records each refresh-token argument so tests can assert the
    call happened (or did not) without any network.
    """

    def __init__(self, result: object) -> None:
        self._result = result
        self.calls: list[str] = []

    def refresh(self, refresh_token: str) -> dict[str, object]:
        self.calls.append(refresh_token)
        if isinstance(self._result, Exception):
            raise self._result
        return dict(self._result)  # type: ignore[arg-type]


class _FlakyLinkedInClient:
    """Client whose FIRST refresh raises an unexpected (non-LinkedIn) error.

    Used to prove the per-account loop is resilient: the first account hits an
    error that is *not* one of the anticipated ``LinkedInError`` subclasses (a
    programming/library fault), and every later account must still be refreshed.
    """

    def __init__(self, good_payload: dict[str, object]) -> None:
        self._good = good_payload
        self.calls: list[str] = []

    def refresh(self, refresh_token: str) -> dict[str, object]:
        self.calls.append(refresh_token)
        if len(self.calls) == 1:
            # An error the refresh code does NOT anticipate (not a LinkedInError):
            # before the fix this aborts the whole run.
            raise RuntimeError("unexpected library fault")
        return dict(self._good)


@pytest.fixture
def settings() -> Settings:
    """Deterministic LIVE settings with a known encryption key (hermetic)."""
    get_settings.cache_clear()
    return get_settings().model_copy(
        update={"vision_env": VisionEnv.LIVE, "token_enc_key": _ENC_KEY}
    )


def _seed_token(
    session: Session,
    *,
    access_expires_at: datetime | None,
    refresh_expires_at: datetime | None,
    member_urn: str = "urn:li:person:TEST",
) -> OAuthToken:
    """Insert one LinkedIn OAuth row with encrypted tokens and return it.

    The tokens are encrypted with the SAME envelope the job uses, binding them to
    the row id — so the seeded record is exactly what production would persist.
    ``member_urn`` is overridable because (provider, member_urn) is unique, so
    tests seeding multiple accounts must give each a distinct URN.
    """
    token = OAuthToken(provider="linkedin", member_urn=member_urn)
    session.add(token)
    session.flush()  # assign the UUID PK (account id used for locks/audit)
    token.access_token_enc = encrypt_token(
        _OLD_ACCESS, _ENC_KEY, provider="linkedin", member_urn=member_urn
    )
    token.refresh_token_enc = encrypt_token(
        _OLD_REFRESH, _ENC_KEY, provider="linkedin", member_urn=member_urn
    )
    token.access_expires_at = access_expires_at
    token.refresh_expires_at = refresh_expires_at
    session.flush()
    return token


# --- Envelope round-trip (security primitive) ------------------------------


def test_encrypt_decrypt_round_trips_and_binds_to_account() -> None:
    # Arrange: encrypt a token bound to member "A" under the canonical AAD.
    member_a = "urn:li:person:A"
    member_b = "urn:li:person:B"
    blob = encrypt_token(_OLD_ACCESS, _ENC_KEY, provider="linkedin", member_urn=member_a)

    # Act: decrypting bound to a DIFFERENT member must fail authentication.
    with pytest.raises(crypto.CryptoError):
        decrypt_token(blob, _ENC_KEY, provider="linkedin", member_urn=member_b)

    # Assert: decrypting bound to the correct member returns the plaintext.
    assert (
        decrypt_token(blob, _ENC_KEY, provider="linkedin", member_urn=member_a)
        == _OLD_ACCESS
    )


# --- Near expiry triggers refresh + stores new encrypted tokens ------------


def test_near_expiry_refreshes_and_stores_new_encrypted_tokens(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange: access token expires in 3 days (inside the 7-day window).
    token = _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=3),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    client = _StubLinkedInClient(
        {"access_token": _NEW_ACCESS, "expires_in": 5_184_000, "refresh_token": _NEW_REFRESH}
    )

    # Act.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=Mock(), lock_dir=tmp_path,
    )

    # Assert: refreshed, and the STORED ciphertext now decrypts to the NEW token.
    assert outcomes[0].status == STATUS_REFRESHED
    stored = decrypt_token(
        token.access_token_enc, _ENC_KEY, provider="linkedin", member_urn=token.member_urn
    )
    assert stored == _NEW_ACCESS


def test_refresh_is_called_with_the_old_refresh_token(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange.
    _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    client = _StubLinkedInClient({"access_token": _NEW_ACCESS, "expires_in": 100})

    # Act.
    refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=Mock(), lock_dir=tmp_path,
    )

    # Assert: exactly one refresh, using the previously stored refresh token.
    assert client.calls == [_OLD_REFRESH]


# --- Healthy token is left untouched ---------------------------------------


def test_healthy_token_is_untouched(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange: access token valid for 30 more days (well beyond the window).
    token = _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=30),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    original_blob = token.access_token_enc
    client = _StubLinkedInClient({"access_token": _NEW_ACCESS, "expires_in": 100})

    # Act.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=Mock(), lock_dir=tmp_path,
    )

    # Assert: healthy verdict, no refresh call, ciphertext unchanged.
    assert outcomes[0].status == STATUS_HEALTHY
    assert client.calls == []
    assert token.access_token_enc == original_blob


# --- Refresh failure raises an alert, not a crash --------------------------


def test_refresh_failure_alerts_and_preserves_state(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange: LinkedIn rejects the refresh (401) — the grant is dead.
    token = _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=2),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    original_blob = token.access_token_enc
    client = _StubLinkedInClient(NeedsReauth())
    sender = Mock()

    # Act: must NOT raise.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=sender, lock_dir=tmp_path,
    )

    # Assert: re-auth alert sent once, and the stored token is preserved.
    assert outcomes[0].status == STATUS_REAUTH_ALERTED
    sender.send.assert_called_once()
    assert token.access_token_enc == original_blob


def test_refresh_token_near_expiry_alerts_without_calling_linkedin(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange: refresh token itself expires in 2 days (inside min-TTL) — a refresh
    # cannot safely succeed, so we must alert without even calling LinkedIn.
    _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=2),
    )
    client = _StubLinkedInClient({"access_token": _NEW_ACCESS, "expires_in": 100})
    sender = Mock()

    # Act.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=sender, lock_dir=tmp_path,
    )

    # Assert: alerted, and LinkedIn was never contacted.
    assert outcomes[0].status == STATUS_REAUTH_ALERTED
    assert client.calls == []
    sender.send.assert_called_once()


# --- Transient errors retry then dead-letter -------------------------------


def test_transient_errors_exhaust_retries_then_dead_letter(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange: every attempt fails transiently (5xx); backoff forced to 0 so the
    # test does not actually sleep between the capped attempts.
    _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    client = _StubLinkedInClient(TransientLinkedInError("boom", status_code=503))
    sender = Mock()

    # Act.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=settings, client=client, sender=sender,
        lock_dir=tmp_path, max_attempts=3, backoff_base=0.0, backoff_max=0.0,
    )

    # Assert: dead-lettered after exactly the capped number of attempts, alerted.
    assert outcomes[0].status == STATUS_DEAD_LETTERED
    assert len(client.calls) == 3
    sender.send.assert_called_once()


# --- Concurrent-refresh lock is respected ----------------------------------


def test_concurrent_refresh_lock_is_respected(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange: a near-expiry token, and a pre-existing lock file simulating another
    # run already refreshing this exact account.
    token = _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    account_id = str(token.id)
    lock_file = tmp_path / f"vision-token-refresh-{account_id}.lock"
    lock_file.write_text("held-by-sibling", encoding="ascii")
    client = _StubLinkedInClient({"access_token": _NEW_ACCESS, "expires_in": 100})

    # Act.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=Mock(), lock_dir=tmp_path,
    )

    # Assert: skipped because locked; no refresh attempted.
    assert outcomes[0].status == STATUS_SKIPPED_LOCKED
    assert client.calls == []


# --- dry_run performs no network / no email --------------------------------


def test_dry_run_does_not_touch_network_or_mail(
    db_session: Session, tmp_path: Path
) -> None:
    # Arrange: DRY_RUN mode with a near-expiry token.
    get_settings.cache_clear()
    dry_settings = get_settings().model_copy(
        update={"vision_env": VisionEnv.DRY_RUN, "token_enc_key": _ENC_KEY}
    )
    _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    client = _StubLinkedInClient({"access_token": _NEW_ACCESS, "expires_in": 100})
    sender = Mock()

    # Act.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=dry_settings, client=client,
        sender=sender, lock_dir=tmp_path,
    )

    # Assert: no refresh call, no mail send.
    assert outcomes[0].status == "dry_run_skipped"
    assert client.calls == []
    sender.send.assert_not_called()


# --- Audit trail records a refresh (no token values) -----------------------


def test_successful_refresh_writes_audit_row(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange.
    _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    client = _StubLinkedInClient({"access_token": _NEW_ACCESS, "expires_in": 100})

    # Act.
    refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=Mock(), lock_dir=tmp_path,
    )

    # Assert: an audit row exists for the refresh, carrying no token value.
    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "token_refreshed")
    ).all()
    assert len(rows) == 1
    assert _NEW_ACCESS not in str(rows[0].meta)


# --- Malformed / hostile refresh JSON must not corrupt stored state --------


@pytest.mark.parametrize(
    "bad_payload",
    [
        # access_token absent entirely -> would KeyError in the naive path.
        {"expires_in": 100},
        # access_token null -> naive path would encrypt the literal string "None".
        {"access_token": None, "expires_in": 100},
        # access_token empty -> a useless credential must be rejected, not stored.
        {"access_token": "", "expires_in": 100},
        # non-numeric expiry -> int(...) would raise ValueError mid-mutation.
        {"access_token": _NEW_ACCESS, "expires_in": "not-a-number"},
        # negative expiry -> a nonsensical (backwards) expiry must be rejected.
        {"access_token": _NEW_ACCESS, "expires_in": -5},
    ],
)
def test_malformed_refresh_payload_alerts_and_preserves_token(
    db_session: Session,
    settings: Settings,
    tmp_path: Path,
    bad_payload: dict[str, object],
) -> None:
    # Arrange: a near-expiry token whose refresh returns a malformed/hostile JSON.
    token = _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=300),
    )
    original_blob = token.access_token_enc
    client = _StubLinkedInClient(bad_payload)
    sender = Mock()

    # Act: must NOT raise, even on a hostile payload.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=sender, lock_dir=tmp_path,
    )

    # Assert: routed to a re-auth alert, and the STORED token is untouched — it
    # still decrypts to the OLD access token (no half-write, no corruption).
    assert outcomes[0].status == STATUS_REAUTH_ALERTED
    assert token.access_token_enc == original_blob
    assert (
        decrypt_token(
            token.access_token_enc, _ENC_KEY, provider="linkedin", member_urn=token.member_urn
        )
        == _OLD_ACCESS
    )
    sender.send.assert_called_once()


# --- One failing account must not abort refresh for the others -------------


def test_one_account_error_does_not_abort_remaining_accounts(
    db_session: Session, settings: Settings, tmp_path: Path
) -> None:
    # Arrange: two near-expiry accounts. The first refresh raises an UNEXPECTED
    # error (not a LinkedInError); the second returns a valid payload.
    _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=300),
        member_urn="urn:li:person:ONE",
    )
    _seed_token(
        db_session,
        access_expires_at=_NOW + timedelta(days=1),
        refresh_expires_at=_NOW + timedelta(days=300),
        member_urn="urn:li:person:TWO",
    )
    client = _FlakyLinkedInClient({"access_token": _NEW_ACCESS, "expires_in": 100})
    sender = Mock()

    # Act: must NOT raise — the run completes for every account.
    outcomes = refresh_if_needed(
        db_session, _NOW, settings=settings, client=client,
        sender=sender, lock_dir=tmp_path,
    )

    # Assert: both accounts were attempted; one dead-lettered, the other refreshed.
    assert len(outcomes) == 2
    assert len(client.calls) == 2
    statuses = {o.status for o in outcomes}
    assert STATUS_REFRESHED in statuses
    assert STATUS_DEAD_LETTERED in statuses


# ===========================================================================
# CLI crash-loop / secret-leak boundary (Codex HIGH — token.py ~42-54).
#
# WHY these live here: the token job is the MOST secret-sensitive cron. Its entry
# point (``token.main``) must fail closed — an unexpected fault from the refresh
# path must yield a non-zero exit and a SANITIZED log (exception class +
# correlation id ONLY), never a traceback and never raw provider/token text.
# ===========================================================================

# Marker present ONLY inside the injected exception message; if it appears in the
# logs, provider/secret text leaked (the vulnerability under test).
_TOKEN_SECRET_MARKER = "REFRESH-PROVIDER-SECRET-do-not-log-xyz789"  # noqa: S105 - test placeholder


@contextmanager
def _fake_session_cm() -> Iterator[MagicMock]:
    """Stand-in for ``get_session()`` yielding a throwaway session (raise before use)."""
    yield MagicMock(name="session")


def test_main_returns_1_and_sanitizes_when_refresh_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: the refresh path raises an unexpected fault carrying secret-ish text.
    get_settings.cache_clear()
    live = get_settings().model_copy(
        update={"vision_env": VisionEnv.LIVE, "token_enc_key": _ENC_KEY}
    )
    monkeypatch.setattr(token_cli, "get_settings", lambda: live)
    monkeypatch.setattr(token_cli, "configure_logging", lambda: None)
    monkeypatch.setattr(token_cli, "get_session", _fake_session_cm)

    def _boom(session: object, now: datetime, *, settings: Settings) -> object:
        raise RuntimeError(f"linkedin refresh rejected: {_TOKEN_SECRET_MARKER}")

    monkeypatch.setattr(token_cli, "refresh_if_needed", _boom)
    caplog.set_level(logging.ERROR)

    # Act: must NOT raise — the boundary swallows the fault and fails closed.
    exit_code = token_cli.main()

    # Assert: non-zero exit for cron alerting, and a SANITIZED record — exception
    # class + correlation id only, NO traceback, NO provider/token text.
    assert exit_code == 1
    record = caplog.records[-1]
    assert record.error_type == "RuntimeError"
    assert record.correlation_id
    assert record.exc_info is None  # no traceback captured
    assert _TOKEN_SECRET_MARKER not in caplog.text
    assert _TOKEN_SECRET_MARKER not in str(record.__dict__)


def test_main_fails_closed_when_settings_load_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: settings/secret parsing raises during startup — for the most
    # secret-sensitive job this must NEVER escape as a raw (possibly secret-bearing)
    # traceback.
    get_settings.cache_clear()
    monkeypatch.setattr(token_cli, "configure_logging", lambda: None)

    def _boom_settings() -> Settings:
        raise RuntimeError(f"bad token config: {_TOKEN_SECRET_MARKER}")

    monkeypatch.setattr(token_cli, "get_settings", _boom_settings)
    caplog.set_level(logging.ERROR)

    # Act: caught by the same fail-closed boundary.
    exit_code = token_cli.main()

    # Assert: non-zero exit, sanitized log, no traceback / provider text leaked.
    assert exit_code == 1
    record = caplog.records[-1]
    assert record.error_type == "RuntimeError"
    assert record.exc_info is None
    assert _TOKEN_SECRET_MARKER not in caplog.text
