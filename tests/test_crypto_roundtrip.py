"""Cross-path OAuth-token crypto round-trip tests (BRD §15.3, NFR-05, threat model §3).

WHY these tests exist (the correctness gate for the go-live publish flow):

The OAuth *save* path (``oauth.handle_callback`` -> ``oauth.save_tokens`` ->
``crypto.encrypt``) and the publisher *load* path
(``worker.LinkedInPublisher._load_credentials`` -> ``worker.decrypt_token``) run
in DIFFERENT processes at DIFFERENT times, but they must agree on ONE crypto
contract: the same key derivation AND the same associated data (AAD). If they
diverge, a token sealed at OAuth time can never be opened at publish time — the
live LinkedIn publish fails closed with a decrypt error even though the stored
ciphertext is perfectly valid.

These tests pin that contract end-to-end:

  1. A token persisted by the SAVE path is decrypted by the LOAD path back to the
     original plaintext, for the SAME account (provider + member_urn).
  2. A load bound to the WRONG member_urn (an attempted record swap) fails closed.
  3. A token refreshed + re-persisted by ``token_refresh`` is loadable by the
     publisher — proving all three modules share the one contract.

All HTTP is mocked (BRD §18): no real network, no real post, no real refresh.
Each test follows AAA (Arrange -> Act -> Assert) with one behavioural focus.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.orm import Session

from vision.config import Settings, VisionEnv, get_settings
from vision.publish import crypto, oauth, token_refresh
from vision.publish.linkedin import LinkedInClient
from vision.publish.worker import LinkedInPublisher, TokenDecryptError

# --- Deterministic test constants ------------------------------------------
# Obviously-fake values so nothing here could match a real credential.
_KEY = "cross-path-token-encryption-key"  # noqa: S105 - test placeholder secret
_MEMBER = "urn:li:person:CROSSPATH1"
_OTHER_MEMBER = "urn:li:person:CROSSPATH2"
_ACCESS = "cross-path-access-token"  # noqa: S105 - test placeholder secret
_REFRESH = "cross-path-refresh-token"  # noqa: S106 - test placeholder secret
_NEW_ACCESS = "cross-path-refreshed-access"  # noqa: S105 - test placeholder secret
_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings() -> Settings:
    """Deterministic LIVE settings with a known token-encryption key (hermetic)."""
    get_settings.cache_clear()
    return get_settings().model_copy(
        update={"vision_env": VisionEnv.LIVE, "token_enc_key": _KEY}
    )


def _token_json() -> dict[str, object]:
    """A full LinkedIn grant response with access + refresh tokens and expiries."""
    return {
        "access_token": _ACCESS,
        "refresh_token": _REFRESH,
        "expires_in": 5_184_000,  # ~60 days
        "refresh_token_expires_in": 31_536_000,  # ~365 days
    }


def test_oauth_saved_token_is_decryptable_by_publisher_load(
    db_session: Session, settings: Settings
) -> None:
    # Arrange: persist a token exactly as the OAuth callback would (SAVE path).
    oauth.save_tokens(
        db_session, member_urn=_MEMBER, token_json=_token_json(), settings=settings
    )
    publisher = LinkedInPublisher(settings, client=MagicMock(spec=LinkedInClient))

    # Act: read the credential through the publisher's LOAD path.
    _row, access_token, member_urn = publisher._load_credentials(db_session)

    # Assert: the publish path recovers the exact plaintext the OAuth path sealed
    # for this same account — the save/load crypto contract is compatible.
    assert access_token == _ACCESS
    assert member_urn == _MEMBER


def test_publisher_load_fails_closed_on_wrong_member_urn(
    db_session: Session, settings: Settings
) -> None:
    # Arrange: seal a token bound to _MEMBER via the canonical OAuth AAD.
    sealed = crypto.encrypt(
        _ACCESS, settings.token_enc_key, associated_data=crypto.oauth_aad("linkedin", _MEMBER)
    )

    # Act / Assert: opening it as a DIFFERENT member (record swap) is rejected —
    # the AAD mismatch fails the GCM tag, so no plaintext is ever returned.
    from vision.publish.worker import decrypt_token

    with pytest.raises(TokenDecryptError):
        decrypt_token(
            sealed, settings.token_enc_key, provider="linkedin", member_urn=_OTHER_MEMBER
        )


def test_refreshed_token_saved_by_token_refresh_is_loadable_by_worker(
    db_session: Session, settings: Settings, tmp_path
) -> None:
    # Arrange: a stored, near-expiry credential (saved via the OAuth path) and a
    # stub client whose refresh returns a brand-new access token.
    oauth.save_tokens(
        db_session, member_urn=_MEMBER, token_json=_token_json(), settings=settings
    )

    class _StubClient:
        def refresh(self, refresh_token: str) -> dict[str, object]:
            return {"access_token": _NEW_ACCESS, "expires_in": 5_184_000}

    # Force the stored access token to look near-expiry so a refresh is due.
    from vision.db.models import OAuthToken

    stored = db_session.query(OAuthToken).one()
    stored.access_expires_at = _NOW + timedelta(days=1)
    stored.refresh_expires_at = _NOW + timedelta(days=300)
    db_session.flush()

    # Act: refresh + re-persist via token_refresh, then load via the worker.
    outcomes = token_refresh.refresh_if_needed(
        db_session,
        _NOW,
        settings=settings,
        client=_StubClient(),
        sender=Mock(),
        lock_dir=tmp_path,
    )
    publisher = LinkedInPublisher(settings, client=MagicMock(spec=LinkedInClient))
    _row, access_token, _member_urn = publisher._load_credentials(db_session)

    # Assert: token_refresh sealed the NEW token under the same contract the worker
    # opens with — the publisher loads the refreshed token, not a decrypt error.
    assert outcomes[0].status == token_refresh.STATUS_REFRESHED
    assert access_token == _NEW_ACCESS
