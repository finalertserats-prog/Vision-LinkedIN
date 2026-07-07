"""Unit tests for token envelope encryption + OAuth glue (BRD §15.1/§15.3, §18).

WHY these tests: they are the acceptance gate for the two security-critical
promises of the OAuth lane —

  1. Tokens are stored with *authenticated* encryption: a round-trip works, but
     any tampering, a wrong key, or a swapped account (AAD) is DETECTED and fails
     closed rather than yielding a forged token (threat model §3).
  2. The 3-legged callback rejects a forged/replayed ``state`` (CSRF) and, on a
     valid callback, persists tokens ENCRYPTED (ciphertext != plaintext) alongside
     the resolved member URN.

All HTTP is mocked by injecting a fake ``LinkedInClient`` — no real network and no
real LinkedIn post ever happens here (BRD §18: mock external deps). Every test
follows AAA (Arrange → Act → Assert) with one behavioural assertion each.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vision.config import Settings, get_settings
from vision.db.models import OAuthToken
from vision.publish import crypto, oauth
from vision.publish.crypto import CryptoError
from vision.publish.oauth import OAuthStateError

# --- Test constants ---------------------------------------------------------
# Obviously-fake values so nothing here could match a real credential and so
# assertions can compare against known strings.
_KEY = "unit-test-token-encryption-key"  # noqa: S105 - test placeholder secret
_OTHER_KEY = "a-different-token-encryption-key"  # noqa: S105 - test placeholder
_MEMBER = "urn:li:person:ABC123"
_OTHER_MEMBER = "urn:li:person:ZZZ999"
_ACCESS = "fake-access-token-value"  # noqa: S105 - test placeholder secret
_REFRESH = "fake-refresh-token-value"  # noqa: S105 - test placeholder secret
# The provider label the model + glue key on (with member_urn) for a stored row.
_PROVIDER = oauth._PROVIDER


# ---------------------------------------------------------------------------
# crypto.py — authenticated envelope encryption
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip_recovers_plaintext() -> None:
    # Arrange: a plaintext token bound to a member URN as associated data.
    plaintext = _ACCESS

    # Act: encrypt then decrypt with the same key + associated data.
    sealed = crypto.encrypt(plaintext, _KEY, associated_data=_MEMBER)
    recovered = crypto.decrypt(sealed, _KEY, associated_data=_MEMBER)

    # Assert: the original plaintext is recovered exactly.
    assert recovered == plaintext


def test_encrypt_output_is_not_the_plaintext() -> None:
    # Arrange / Act: seal a token.
    sealed = crypto.encrypt(_ACCESS, _KEY, associated_data=_MEMBER)

    # Assert: the stored bytes never contain the plaintext token (confidentiality).
    assert _ACCESS.encode("utf-8") not in sealed


def test_encrypt_uses_random_nonce_so_ciphertexts_differ() -> None:
    # Arrange / Act: encrypt the same plaintext twice.
    first = crypto.encrypt(_ACCESS, _KEY, associated_data=_MEMBER)
    second = crypto.encrypt(_ACCESS, _KEY, associated_data=_MEMBER)

    # Assert: a fresh random nonce makes the two envelopes differ (no nonce reuse).
    assert first != second


def test_ciphertext_carries_the_version_byte() -> None:
    # Arrange / Act: seal a token.
    sealed = crypto.encrypt(_ACCESS, _KEY, associated_data=_MEMBER)

    # Assert: the first byte is the format version tag (ciphertext versioning).
    assert sealed[0] == crypto._VERSION


def test_tampered_ciphertext_fails_authentication() -> None:
    # Arrange: a valid envelope, then flip one bit in the ciphertext body.
    sealed = bytearray(crypto.encrypt(_ACCESS, _KEY, associated_data=_MEMBER))
    sealed[-1] ^= 0x01  # corrupt the last byte (inside the GCM-protected region)

    # Act / Assert: the tag check fails and decryption raises (fail-closed).
    with pytest.raises(CryptoError):
        crypto.decrypt(bytes(sealed), _KEY, associated_data=_MEMBER)


def test_wrong_key_fails_to_decrypt() -> None:
    # Arrange: seal under one key.
    sealed = crypto.encrypt(_ACCESS, _KEY, associated_data=_MEMBER)

    # Act / Assert: a different key cannot authenticate/decrypt the envelope.
    with pytest.raises(CryptoError):
        crypto.decrypt(sealed, _OTHER_KEY, associated_data=_MEMBER)


def test_wrong_associated_data_fails_to_decrypt() -> None:
    # Arrange: seal bound to one member URN.
    sealed = crypto.encrypt(_ACCESS, _KEY, associated_data=_MEMBER)

    # Act / Assert: decrypting as a different member (swapped record) is rejected.
    with pytest.raises(CryptoError):
        crypto.decrypt(sealed, _KEY, associated_data=_OTHER_MEMBER)


def test_unknown_version_byte_is_rejected() -> None:
    # Arrange: a valid envelope with its version byte bumped to an unknown value.
    sealed = bytearray(crypto.encrypt(_ACCESS, _KEY, associated_data=_MEMBER))
    sealed[0] = 0xFF  # a version this build does not understand

    # Act / Assert: unknown-version ciphertext is refused (rotation safety).
    with pytest.raises(CryptoError):
        crypto.decrypt(bytes(sealed), _KEY, associated_data=_MEMBER)


def test_truncated_ciphertext_is_rejected() -> None:
    # Arrange: a too-short blob that cannot hold nonce + tag.
    too_short = b"\x01\x02\x03"

    # Act / Assert: strict length validation raises rather than index-erroring.
    with pytest.raises(CryptoError):
        crypto.decrypt(too_short, _KEY, associated_data=_MEMBER)


def test_encrypt_rejects_empty_plaintext() -> None:
    # Arrange / Act / Assert: encrypting an empty token is a caller bug — fail loud.
    with pytest.raises(CryptoError):
        crypto.encrypt("", _KEY, associated_data=_MEMBER)


# ---------------------------------------------------------------------------
# oauth.py — 3-legged callback glue (LinkedInClient mocked)
# ---------------------------------------------------------------------------


class _FakeLinkedInClient:
    """A stand-in for ``LinkedInClient`` with NO network (BRD §18 mock boundary).

    Records the code it was asked to exchange and returns canned token JSON +
    member URN, so the OAuth glue's real logic (state check, encryption, storage)
    runs against a fully deterministic LinkedIn.
    """

    def __init__(self, token_json: dict[str, Any], member_urn: str) -> None:
        self._token_json = token_json
        self._member_urn = member_urn
        self.exchanged_code: str | None = None
        self.seen_access_token: str | None = None

    def build_authorize_url(self, state: str) -> str:
        # Mirror the real client's shape closely enough for start_authorize tests.
        return f"https://www.linkedin.com/oauth/v2/authorization?state={state}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        # Capture the code so a test can assert it was (or was NOT) used.
        self.exchanged_code = code
        return self._token_json

    def get_member_urn(self, access_token: str) -> str:
        self.seen_access_token = access_token
        return self._member_urn


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings with a known token-encryption key.

    ``model_copy(update=...)`` sets fields by name without touching the real
    environment, keeping the test hermetic.
    """
    get_settings.cache_clear()
    return get_settings().model_copy(update={"token_enc_key": _KEY})


@pytest.fixture
def fake_client() -> _FakeLinkedInClient:
    """A fake client returning a full token bundle for the known member URN."""
    token_json = {
        "access_token": _ACCESS,
        "refresh_token": _REFRESH,
        "expires_in": 5184000,  # ~60 days
        "refresh_token_expires_in": 31536000,  # ~365 days
    }
    return _FakeLinkedInClient(token_json=token_json, member_urn=_MEMBER)


def test_handle_callback_stores_encrypted_tokens_and_returns_member_urn(
    db_session: Session,
    settings: Settings,
    fake_client: _FakeLinkedInClient,
) -> None:
    # Arrange: a callback whose returned state matches what we issued.
    state = "csrf-nonce-xyz"

    # Act: run the callback with the mocked client.
    member_urn = oauth.handle_callback(
        db_session,
        code="auth-code-123",
        state=state,
        expected_state=state,
        client=fake_client,
        settings=settings,
    )

    # Assert: the authenticated member URN is returned to the caller.
    assert member_urn == _MEMBER


def test_handle_callback_persists_ciphertext_not_plaintext(
    db_session: Session,
    settings: Settings,
    fake_client: _FakeLinkedInClient,
) -> None:
    # Arrange / Act: complete a valid callback.
    oauth.handle_callback(
        db_session,
        code="auth-code-123",
        state="s",
        expected_state="s",
        client=fake_client,
        settings=settings,
    )

    # Assert: the stored access token bytes are ciphertext, never the plaintext.
    row = db_session.execute(select(OAuthToken)).scalar_one()
    assert row.access_token_enc is not None
    assert _ACCESS.encode("utf-8") not in row.access_token_enc


def test_stored_tokens_decrypt_back_to_original(
    db_session: Session,
    settings: Settings,
    fake_client: _FakeLinkedInClient,
) -> None:
    # Arrange: persist tokens via the callback.
    oauth.handle_callback(
        db_session,
        code="auth-code-123",
        state="s",
        expected_state="s",
        client=fake_client,
        settings=settings,
    )

    # Act: load + decrypt through the round-trip helper.
    loaded = oauth.load_tokens(db_session, member_urn=_MEMBER, settings=settings)

    # Assert: the decrypted access token equals what LinkedIn returned.
    assert loaded["access_token"] == _ACCESS


def test_handle_callback_rejects_state_mismatch(
    db_session: Session,
    settings: Settings,
    fake_client: _FakeLinkedInClient,
) -> None:
    # Arrange: a returned state that does not match the expected one (forged CSRF).
    # Act / Assert: the callback aborts with a state error...
    with pytest.raises(OAuthStateError):
        oauth.handle_callback(
            db_session,
            code="auth-code-123",
            state="attacker-supplied-state",
            expected_state="the-real-state",
            client=fake_client,
            settings=settings,
        )


def test_state_mismatch_never_exchanges_the_code(
    db_session: Session,
    settings: Settings,
    fake_client: _FakeLinkedInClient,
) -> None:
    # Arrange / Act: attempt a callback with a bad state and swallow the error.
    with pytest.raises(OAuthStateError):
        oauth.handle_callback(
            db_session,
            code="auth-code-123",
            state="bad",
            expected_state="good",
            client=fake_client,
            settings=settings,
        )

    # Assert: the one-time code was NEVER spent (CSRF gate runs before exchange).
    assert fake_client.exchanged_code is None


def test_state_mismatch_stores_no_tokens(
    db_session: Session,
    settings: Settings,
    fake_client: _FakeLinkedInClient,
) -> None:
    # Arrange / Act: a forged callback must not persist anything.
    with pytest.raises(OAuthStateError):
        oauth.handle_callback(
            db_session,
            code="auth-code-123",
            state="bad",
            expected_state="good",
            client=fake_client,
            settings=settings,
        )

    # Assert: no OAuth token row was written (fail-closed).
    assert db_session.execute(select(OAuthToken)).scalar_one_or_none() is None


def test_start_authorize_rejects_blank_state(settings: Settings) -> None:
    # Arrange / Act / Assert: a blank state gives no CSRF protection — refuse.
    with pytest.raises(OAuthStateError):
        oauth.start_authorize("   ", client=_FakeLinkedInClient({}, _MEMBER))


def test_start_authorize_returns_url_carrying_state() -> None:
    # Arrange: a caller-supplied CSRF nonce.
    state = "csrf-nonce-abc"

    # Act: build the consent URL via the (fake) client.
    url = oauth.start_authorize(state, client=_FakeLinkedInClient({}, _MEMBER))

    # Assert: the state is echoed into the authorize URL for later verification.
    assert f"state={state}" in url


def test_save_tokens_updates_existing_row_atomically(
    db_session: Session,
    settings: Settings,
) -> None:
    # Arrange: an initial token bundle for the member.
    first = {
        "access_token": "first-access",
        "refresh_token": "first-refresh",
        "expires_in": 3600,
    }
    oauth.save_tokens(
        db_session, member_urn=_MEMBER, token_json=first, settings=settings
    )

    # Act: a second save (e.g. a refresh) for the SAME member.
    second = {
        "access_token": "second-access",
        "refresh_token": "second-refresh",
        "expires_in": 3600,
    }
    oauth.save_tokens(
        db_session, member_urn=_MEMBER, token_json=second, settings=settings
    )

    # Assert: exactly one row exists (atomic replacement, not an append).
    rows = db_session.execute(select(OAuthToken)).scalars().all()
    assert len(rows) == 1


def test_load_tokens_raises_when_no_credential_stored(
    db_session: Session,
    settings: Settings,
) -> None:
    # Arrange / Act / Assert: loading an unknown member fails loudly (fail-closed).
    with pytest.raises(oauth.OAuthError):
        oauth.load_tokens(db_session, member_urn="urn:li:person:UNKNOWN", settings=settings)


# ---------------------------------------------------------------------------
# oauth_tokens uniqueness — atomic token replacement / refresh races (§3)
# ---------------------------------------------------------------------------


def test_duplicate_provider_member_urn_is_rejected_at_the_db(
    db_session: Session,
) -> None:
    # Arrange: one credential row already stored for (provider, member_urn).
    db_session.add(
        OAuthToken(provider=_PROVIDER, member_urn=_MEMBER, access_token_enc=b"\x01one")
    )
    db_session.flush()

    # Act: a second, concurrent-style INSERT for the SAME (provider, member_urn).
    db_session.add(
        OAuthToken(provider=_PROVIDER, member_urn=_MEMBER, access_token_enc=b"\x02two")
    )

    # Assert: the UniqueConstraint makes the duplicate insert fail fast so two
    # racing refresh workers can never create two live rows (threat model §3).
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_save_tokens_twice_updates_the_same_row_not_duplicates(
    db_session: Session,
    settings: Settings,
) -> None:
    # Arrange: an initial saved bundle, then a refreshed bundle for the SAME member.
    oauth.save_tokens(
        db_session,
        member_urn=_MEMBER,
        token_json={"access_token": "first-access", "expires_in": 3600},
        settings=settings,
    )
    oauth.save_tokens(
        db_session,
        member_urn=_MEMBER,
        token_json={"access_token": "second-access", "expires_in": 3600},
        settings=settings,
    )

    # Assert: exactly one row survives and it decrypts to the LATEST token — the
    # second save UPDATED in place rather than appending a duplicate.
    rows = db_session.execute(select(OAuthToken)).scalars().all()
    assert len(rows) == 1
    loaded = oauth.load_tokens(db_session, member_urn=_MEMBER, settings=settings)
    assert loaded["access_token"] == "second-access"
