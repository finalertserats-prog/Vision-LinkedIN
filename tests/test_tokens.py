"""Unit tests for the signed approval-token module (BRD §14.2).

These tests are DB-free by design: the single-use check is injected as a plain
callback, so we exercise the full security contract (signature, expiry, replay,
action scope, constant-time compare, hash-only storage) without any database or
external system — mocking only at the true boundary (the used-token store).

All tests follow AAA (Arrange → Act → Assert) with one behaviour per test.
"""

from __future__ import annotations

import hashlib
import string
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from vision.approval import tokens
from vision.approval.errors import (
    BadAction,
    ExpiredToken,
    InvalidToken,
    UsedToken,
)
from vision.approval.tokens import VerifiedToken, issue_token, verify_token

# A fixed secret and draft id used across tests — constants, not shared mutable
# state, so tests remain independent.
_SECRET = "unit-test-hmac-secret"
_DRAFT_ID = "11111111-2222-3333-4444-555555555555"

# The base64url alphabet, used by the non-canonical mutation helper below to
# search for an alternate final character that decodes to the SAME bytes.
_B64URL_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"


def _never_used(_token_hash: str) -> bool:
    """Callback stand-in for a token that has NOT been consumed yet."""
    return False


def _always_used(_token_hash: str) -> bool:
    """Callback stand-in for a token already recorded in ``used_tokens``."""
    return True


def _noncanonical_variant(segment: str) -> str | None:
    """Return a DIFFERENT base64url string that decodes to the same bytes.

    WHY: base64url without padding is non-canonical — when the decoded byte
    length is not a multiple of 3, the final character carries unused low bits.
    Several distinct final characters therefore decode to identical bytes. This
    helper mutates the last character and returns the first alternative whose
    decoded bytes exactly match the original, or ``None`` if the segment has no
    slack (so the test can assert a variant genuinely exists before relying on
    it). This is the exact primitive an attacker uses to forge a wire-distinct
    but signature-equivalent token.
    """
    original_bytes = tokens._b64url_decode(segment)
    for candidate_char in _B64URL_ALPHABET:
        if candidate_char == segment[-1]:
            continue
        candidate = segment[:-1] + candidate_char
        if tokens._b64url_decode(candidate) == original_bytes:
            return candidate
    return None


def _single_use_key_from_wire(token_str: str) -> str:
    """Derive the single-use key from a wire token by decoding its payload.

    Mirrors what the verifier must do: decode the payload, extract the signed
    ``draft_id`` and ``nonce``, and hash those DECODED fields. Because it keys on
    decoded content (never the raw wire bytes), two wire encodings of the same
    payload yield an identical key — the property the security fix guarantees.
    """
    payload_b64 = token_str.split(".")[0]
    payload = tokens._b64url_decode(payload_b64).decode("utf-8")
    draft_id, _action, _exp_raw, nonce = payload.rsplit("|", 3)
    return tokens._single_use_key(draft_id, nonce)


def test_valid_token_round_trips_to_verified_contents() -> None:
    # Arrange: mint a fresh approve token with a 1-hour TTL.
    token_str, _token_hash, expires_at = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    now = datetime.now(timezone.utc)

    # Act: verify it well before expiry with an unused-token callback.
    verified = verify_token(token_str, _SECRET, now, _never_used)

    # Assert: the trusted contents match what was issued.
    assert isinstance(verified, VerifiedToken)
    assert verified.draft_id == _DRAFT_ID
    assert verified.action == "approve"
    assert verified.exp == int(expires_at.timestamp())
    assert verified.nonce  # a non-empty random nonce was embedded


def test_tampered_signature_raises_invalid_token() -> None:
    # Arrange: mint a valid token, then flip a character in its signature segment
    # to simulate tampering / a forged link.
    token_str, _hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    payload_b64, signature_b64 = token_str.split(".")
    flipped = "A" if signature_b64[-1] != "A" else "B"
    tampered = f"{payload_b64}.{signature_b64[:-1]}{flipped}"
    now = datetime.now(timezone.utc)

    # Act / Assert: the HMAC no longer verifies.
    with pytest.raises(InvalidToken):
        verify_token(tampered, _SECRET, now, _never_used)


def test_noncanonical_base64_is_rejected_or_not_replayable() -> None:
    # Arrange: mint a valid token, then re-encode its signature segment into a
    # DIFFERENT wire string that base64url-decodes to the SAME signature bytes.
    # Under the original bug this variant passed the signature check (which
    # compared decoded bytes) yet produced a different sha256(token_str), so a
    # captured, already-consumed link could be replayed as "fresh".
    token_str, token_hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    payload_b64, signature_b64 = token_str.split(".")
    variant_sig = _noncanonical_variant(signature_b64)
    # Sanity: a 32-byte HMAC has final-character slack, so a variant must exist.
    assert variant_sig is not None and variant_sig != signature_b64
    mutated = f"{payload_b64}.{variant_sig}"
    now = datetime.now(timezone.utc)

    # Act / Assert: the security property is that a re-encoded variant of a token
    # must NOT verify as fresh. Two acceptable defences satisfy this:
    #   * canonical-rejection: verify_token raises InvalidToken outright, OR
    #   * nonce-keyed single-use: it verifies but hands the store the SAME key as
    #     the original, so a consumed token is still caught as used.
    seen: list[str] = []

    def _recording(value: str) -> bool:
        seen.append(value)
        return False

    try:
        verify_token(mutated, _SECRET, now, _recording)
    except InvalidToken:
        return  # canonical-rejection defence — nothing more to assert
    # Fell through: the variant was accepted, so its single-use key MUST equal
    # the original's, or a replay would slip past the is_used check.
    assert seen == [token_hash]


def test_single_use_key_is_stable_across_reencoding() -> None:
    # Arrange: two wire strings that decode to the same payload + signature (the
    # signature re-encoded to an equivalent non-canonical form).
    token_str, token_hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    payload_b64, signature_b64 = token_str.split(".")
    variant_sig = _noncanonical_variant(signature_b64)
    assert variant_sig is not None and variant_sig != signature_b64
    wire_a = token_str
    wire_b = f"{payload_b64}.{variant_sig}"
    assert wire_a != wire_b

    # Act: derive the single-use key from each wire string's DECODED payload.
    key_a = _single_use_key_from_wire(wire_a)
    key_b = _single_use_key_from_wire(wire_b)

    # Assert: the key is invariant to wire encoding and matches issue_token's
    # returned hash — so the used-token store keys on stable, forge-proof data.
    assert key_a == key_b == token_hash


def test_tampered_payload_raises_invalid_token() -> None:
    # Arrange: keep the original signature but swap the payload — the signature
    # was computed over the ORIGINAL payload, so it must fail to verify.
    token_str, _hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    _payload_b64, signature_b64 = token_str.split(".")
    other, _h2, _e2 = issue_token("99999999-0000-0000-0000-000000000000", "approve", 3600, _SECRET)
    other_payload = other.split(".")[0]
    forged = f"{other_payload}.{signature_b64}"
    now = datetime.now(timezone.utc)

    # Act / Assert.
    with pytest.raises(InvalidToken):
        verify_token(forged, _SECRET, now, _never_used)


def test_wrong_secret_raises_invalid_token() -> None:
    # Arrange: a token signed with one secret must not verify under another.
    token_str, _hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    now = datetime.now(timezone.utc)

    # Act / Assert.
    with pytest.raises(InvalidToken):
        verify_token(token_str, "a-different-secret", now, _never_used)


def test_expired_token_raises_expired_token() -> None:
    # Arrange: mint a token that is already expired (negative TTL puts exp in the
    # past), then verify at the real current time.
    token_str, _hash, _exp = issue_token(_DRAFT_ID, "approve", -10, _SECRET)
    now = datetime.now(timezone.utc)

    # Act / Assert: expiry is caught distinctly from a bad signature.
    with pytest.raises(ExpiredToken):
        verify_token(token_str, _SECRET, now, _never_used)


def test_verification_at_a_future_time_raises_expired_token() -> None:
    # Arrange: a short-lived token verified at a time past its expiry — proves
    # the injected ``now`` (not the wall clock) governs expiry.
    token_str, _hash, expires_at = issue_token(_DRAFT_ID, "approve", 60, _SECRET)
    future = expires_at + timedelta(seconds=1)

    # Act / Assert.
    with pytest.raises(ExpiredToken):
        verify_token(token_str, _SECRET, future, _never_used)


def test_replayed_token_raises_used_token() -> None:
    # Arrange: a valid, unexpired token whose hash the store reports as already
    # consumed — i.e. the link is being clicked a second time.
    token_str, _hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    now = datetime.now(timezone.utc)

    # Act / Assert: single-use is enforced.
    with pytest.raises(UsedToken):
        verify_token(token_str, _SECRET, now, _always_used)


def test_used_callback_receives_hash_not_raw_token() -> None:
    # Arrange: capture whatever value the single-use callback is handed so we can
    # prove the raw token never reaches the store.
    token_str, token_hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    now = datetime.now(timezone.utc)
    seen: list[str] = []

    def _recording_callback(value: str) -> bool:
        seen.append(value)
        return False

    # Act.
    verify_token(token_str, _SECRET, now, _recording_callback)

    # Assert: the callback saw the sha256 hash, never the raw token.
    assert seen == [token_hash]
    assert token_str not in seen


def test_issue_rejects_action_outside_scope_with_bad_action() -> None:
    # Arrange / Act / Assert: an action outside {approve,reject,edit,post_now} is
    # refused at mint time — a token is only ever scoped to a known action.
    with pytest.raises(BadAction):
        issue_token(_DRAFT_ID, "delete", 3600, _SECRET)


def test_each_valid_action_round_trips() -> None:
    # Arrange: every allowed action must mint and verify cleanly.
    now = datetime.now(timezone.utc)
    for action in ("approve", "reject", "edit", "post_now"):
        # Act.
        token_str, _hash, _exp = issue_token(_DRAFT_ID, action, 3600, _SECRET)
        verified = verify_token(token_str, _SECRET, now, _never_used)

        # Assert: the verified action matches what was minted (scope preserved).
        assert verified.action == action


def test_malformed_token_string_raises_invalid_token() -> None:
    # Arrange: a token with no "." separator cannot be split into the two
    # required segments.
    now = datetime.now(timezone.utc)

    # Act / Assert.
    with pytest.raises(InvalidToken):
        verify_token("not-a-valid-token", _SECRET, now, _never_used)


def test_constant_time_compare_is_used_for_signature_check() -> None:
    # Arrange: wrap the real hmac.compare_digest so we both preserve behaviour
    # and can assert it was invoked — the signature check MUST be constant-time.
    token_str, _hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)
    now = datetime.now(timezone.utc)
    real_compare = tokens.hmac.compare_digest

    with patch.object(
        tokens.hmac, "compare_digest", side_effect=real_compare
    ) as spy_compare:
        # Act.
        verify_token(token_str, _SECRET, now, _never_used)

    # Assert: verification routed the signature check through compare_digest.
    assert spy_compare.called


def test_issue_returns_nonce_single_use_key_never_the_raw_token() -> None:
    # Arrange / Act: mint a token and inspect the returned single-use key.
    token_str, token_hash, _exp = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)

    # Assert: the persisted value is the nonce-derived single-use key (§14.2),
    # NOT the raw token and NOT sha256(token_str). Keying on the decoded nonce is
    # what makes it stable across wire re-encodings; it must equal the key
    # recomputed from the token's own decoded payload.
    assert token_hash != token_str
    assert token_hash != hashlib.sha256(token_str.encode("utf-8")).hexdigest()
    assert token_hash == _single_use_key_from_wire(token_str)
    assert len(token_hash) == 64  # sha256 hex digest length


def test_expires_at_is_timezone_aware_utc() -> None:
    # Arrange / Act: the returned expiry must be an aware UTC datetime so callers
    # store an unambiguous instant (portable across SQLite/Postgres per §22).
    _token, _hash, expires_at = issue_token(_DRAFT_ID, "approve", 3600, _SECRET)

    # Assert.
    assert expires_at.tzinfo is not None
    assert expires_at.utcoffset() == timedelta(0)
