"""Signed, single-use, expiring, action-scoped approval tokens (BRD §14.2).

WHY this module exists: every Approve / Reject / Edit / Post-now link in the
daily email can act on the owner's real LinkedIn profile, so each link must be
treated like a magic-login token (§14.2, NFR-05):

  * **Signed**   — an HMAC-SHA256 over the payload proves VISION minted it and
                   nobody tampered with the draft id, action, or expiry.
  * **Expiring**  — an embedded ``exp`` timestamp means a leaked link dies on its
                   own (default cutoff 20:00 IST).
  * **Action-scoped** — the action is inside the signed payload, so an Approve
                   link can never be replayed as a Post-now.
  * **Single-use** — only the token's nonce-derived *key* is persisted; on
                   verify we ask a callback whether that key was already
                   consumed, blocking replay of a captured link.

Token wire format mirrors the BRD literally::

    base64url(draft_id | action | exp | nonce) . base64url(HMAC-SHA256)

Security invariants enforced here:
  * The raw token is NEVER stored — only ``sha256(draft_id | nonce)`` (the
    ``token_hash``), a key derived from the SIGNED nonce so it is stable across
    any base64url re-encoding of the wire token (§14.2).
  * Both wire segments must be CANONICAL base64url (decode-then-re-encode must
    round-trip); a non-canonical re-encoding is rejected as InvalidToken.
  * Signature comparison uses :func:`hmac.compare_digest` (constant-time) so the
    verifier cannot be timing-attacked byte-by-byte.
  * Verification *fails closed* (NFR-04): any doubt raises, nothing is returned.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from vision.approval.errors import (
    BadAction,
    ExpiredToken,
    InvalidToken,
    UsedToken,
)

# --- Action scope ----------------------------------------------------------
# The actions an approval link may carry (§14.2 / FR-10). Kept as an immutable
# frozenset so the allowed scope is a single source of truth that both issue and
# verify validate against — an unknown action is rejected at the earliest
# possible point rather than mis-routing an endpoint later.
#
# ``overrule`` is a COUNCIL-only action: the owner supplies a one-line
# counter-take that overrides the council's synthesised post. It is deliberately
# an EDIT-flow variant (it reuses the edit machinery — see mailer/composer and
# the edit endpoint) rather than a whole new endpoint, so it lives in the same
# allowlist and its signed link verifies through exactly the same path as edit.
VALID_ACTIONS: frozenset[str] = frozenset(
    {"approve", "reject", "edit", "post_now", "overrule"}
)

# Field separator inside the signed payload. Chosen because none of the fields
# (a UUID draft id, a fixed action word, an integer epoch, a urlsafe nonce) can
# legitimately contain "|", so it can never be confused with real data.
_SEP = "|"

# Separator between the payload segment and the signature segment on the wire.
# "." is URL-safe and is not part of the base64url alphabet, so it unambiguously
# splits the token into its two halves.
_DOT = "."


@dataclass(frozen=True)
class VerifiedToken:
    """The trusted contents of a token that passed every verification check.

    Frozen (immutable) per the project's immutability principle: once the
    verifier has vouched for these values, no downstream code may mutate them
    before they drive a state change. Returning a typed object (not a raw dict)
    means callers get autocompletion and type-checking on the fields they act on.
    """

    draft_id: str  # which draft this link governs (provenance for the audit_log)
    action: str  # one of VALID_ACTIONS — the single action this link may perform
    exp: int  # expiry as a Unix epoch second (UTC), embedded and signed
    nonce: str  # random per-token value; makes each token (and its hash) unique


def _b64url_encode(raw: bytes) -> str:
    """Base64url-encode bytes WITHOUT padding.

    WHY strip padding: ``=`` padding is noisy in URLs and some clients mangle it;
    dropping it keeps the token clean in an email link. The decoder re-adds the
    padding, so this is lossless.
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Inverse of :func:`_b64url_encode`, restoring the stripped padding.

    Base64 needs the input length to be a multiple of 4; we re-append the exact
    number of ``=`` chars that were removed so decoding succeeds. A malformed
    segment raises ``binascii.Error`` which the caller maps to ``InvalidToken``.
    """
    # Pad up to the next multiple of 4 (-len % 4 gives 0..3 padding chars).
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _secret_bytes(secret: str | bytes) -> bytes:
    """Normalise the server secret to bytes for HMAC.

    Accepting either ``str`` (as it arrives from ``Settings.secret_hmac_key``) or
    ``bytes`` keeps callers simple while ensuring HMAC always gets bytes.
    """
    return secret if isinstance(secret, bytes) else secret.encode("utf-8")


def _sign(payload_b64: str, secret: str | bytes) -> str:
    """Return the base64url HMAC-SHA256 of the (already-encoded) payload.

    Signing the *encoded* payload string (not the raw fields) means the exact
    bytes that travel on the wire are the bytes that are signed — there is no
    room for a re-encoding ambiguity between sign and verify.
    """
    digest = hmac.new(_secret_bytes(secret), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _single_use_key(draft_id: str, nonce: str) -> str:
    """Return the single-use key: SHA-256 of the DECODED ``draft_id | nonce``.

    WHY key on the decoded nonce, not the raw wire string (BRD §14.2): the nonce
    is the token's unique identity and lives INSIDE the signed payload, so its
    value is stable across every possible re-encoding of the token. Keying the
    used-token store on a hash of the RAW wire string was a replay hole —
    base64url is non-canonical, so an attacker could re-encode a consumed link
    into a wire-distinct-but-signature-equivalent variant that hashed to a
    "fresh" key and slipped past the single-use check. Hashing the signed nonce
    (scoped by ``draft_id`` so nonces can't collide across drafts) closes that
    hole and matches the BRD's "nonce checked against ``used_tokens``" wording.

    This digest — never the raw token — is what VISION persists (``token_hash``
    on the draft) and what ``is_used_callback`` receives, so a database leak
    still cannot yield a working Approve link.
    """
    keyed = _SEP.join((draft_id, nonce))
    return hashlib.sha256(keyed.encode("utf-8")).hexdigest()


def _decode_canonical(segment: str) -> bytes:
    """Base64url-decode ``segment`` and REQUIRE it to be in canonical form.

    WHY (security): base64url without padding is non-canonical — when the
    decoded byte length is not a multiple of 3, the final character carries
    unused low bits, so several distinct characters decode to identical bytes.
    We decode, then re-encode the decoded bytes and demand the result equals the
    received segment. Any mismatch means a non-canonical (tampered / re-encoded)
    segment and raises ``InvalidToken`` — this makes the exact wire bytes of a
    valid token uniquely determined, defeating re-encoding replay attacks at the
    earliest point. Malformed base64url raises ``binascii.Error`` (a
    ``ValueError``) which the caller maps to ``InvalidToken``.
    """
    raw = _b64url_decode(segment)
    if _b64url_encode(raw) != segment:
        raise InvalidToken("token segment is not canonical base64url")
    return raw


def issue_token(
    draft_id: str,
    action: str,
    ttl_seconds: int,
    secret: str | bytes,
) -> tuple[str, str, datetime]:
    """Mint a signed, single-use, expiring, action-scoped approval token.

    Returns ``(token_str, token_hash, expires_at)`` where:
      * ``token_str``  — the value placed in the email link (given out once).
      * ``token_hash`` — the single-use key ``sha256(draft_id | nonce)`` derived
                         from the SIGNED nonce (§14.2), the ONLY thing to persist.
                         This is exactly the value ``verify_token`` recomputes and
                         hands to ``is_used_callback``, so the used-token store
                         keys on the same stable, re-encoding-proof identity.
      * ``expires_at`` — timezone-aware UTC datetime the caller stores as
                         ``token_expires_at`` for display / cleanup.

    The caller is responsible for storing ``token_hash`` + ``expires_at`` on the
    draft; this function is pure and touches no database, keeping it trivially
    testable and reusable across the daily job and the edit flow.
    """
    # Fail closed on an out-of-scope action *before* doing any crypto — a token
    # is only ever minted for one of the four known actions (§14.2).
    if action not in VALID_ACTIONS:
        raise BadAction(f"action must be one of {sorted(VALID_ACTIONS)}, got {action!r}")

    # Compute expiry from *now* in UTC. Storing an absolute epoch (not a relative
    # TTL) means the token carries its own deadline; verification needs no memory
    # of when it was issued.
    now = datetime.now(timezone.utc)
    exp = int(now.timestamp()) + int(ttl_seconds)

    # A fresh random nonce guarantees uniqueness even for two links minted in the
    # same second for the same draft+action, so their hashes differ and the
    # single-use store treats them independently. secrets (CSPRNG) not random.
    nonce = secrets.token_urlsafe(16)

    # Assemble the human-unreadable-but-structured payload, then base64url it so
    # the whole thing is one URL-safe segment (matches the BRD wire format).
    payload = _SEP.join((draft_id, action, str(exp), nonce))
    payload_b64 = _b64url_encode(payload.encode("utf-8"))

    # Sign the encoded payload and join the two segments with "." into the token.
    signature_b64 = _sign(payload_b64, secret)
    token_str = f"{payload_b64}{_DOT}{signature_b64}"

    # Derive the storable single-use key (nonce-based, §14.2) and the aware
    # expiry datetime for the caller.
    token_hash = _single_use_key(draft_id, nonce)
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)

    return token_str, token_hash, expires_at


def verify_token(
    token_str: str,
    secret: str | bytes,
    now: datetime,
    is_used_callback: Callable[[str], bool],
) -> VerifiedToken:
    """Verify a token's signature, expiry, and single-use status — or raise.

    Checks run in the safest order so the cheapest / most security-critical gate
    fails first and no expensive or side-effecting work happens on a bad token:

      1. **Structure**  — the token must split into exactly two segments.
      2. **Signature**  — constant-time HMAC compare (``compare_digest``); a
                          mismatch means tampering or wrong secret → InvalidToken.
      3. **Payload**    — decode + split into exactly four fields → else Invalid.
      4. **Action**     — must still be in scope → else BadAction.
      5. **Expiry**     — ``now`` must be strictly before ``exp`` → else Expired.
      6. **Single-use** — the token hash must be unconsumed → else UsedToken.

    ``now`` is injected (not read from the clock here) so callers and tests
    control the reference time precisely — the function stays pure and
    deterministic. ``is_used_callback`` receives the token *hash* (never the raw
    token) and returns True if it was already consumed.

    Fails closed (NFR-04): on ANY problem it raises; it never returns a partial
    or best-effort result.
    """
    # 1. Structure: exactly one "." separating payload and signature. rsplit with
    #    maxsplit avoids ambiguity even though neither segment contains ".".
    parts = token_str.split(_DOT)
    if len(parts) != 2:
        raise InvalidToken("token is not in '<payload>.<signature>' form")
    payload_b64, signature_b64 = parts

    # 2. Signature FIRST, before trusting any payload bytes. Recompute the
    #    expected signature over the received payload and compare in constant
    #    time so a partial-match attacker gains no timing signal. Decode both to
    #    raw bytes so the compare is over the actual digests, not their encodings.
    #    ``_decode_canonical`` additionally REJECTS a non-canonical signature
    #    segment: without it, an attacker could re-encode a captured signature
    #    into a byte-identical-but-wire-distinct variant, which is the exact
    #    replay hole this fix closes (see ``_single_use_key`` / _decode_canonical).
    expected_b64 = _sign(payload_b64, secret)
    try:
        got_sig = _decode_canonical(signature_b64)
        expected_sig = _b64url_decode(expected_b64)
    except (ValueError, TypeError) as exc:
        # A signature segment that isn't valid base64url is simply an invalid
        # token; surface it as InvalidToken (never leak the decode error type).
        raise InvalidToken("signature segment is not valid base64url") from exc
    if not hmac.compare_digest(got_sig, expected_sig):
        raise InvalidToken("signature does not verify")

    # 3. Payload: only now that the signature is trusted do we decode the fields.
    #    rsplit(_SEP, 3) yields exactly [draft_id, action, exp, nonce]; using
    #    rsplit means a draft_id that ever contained the separator would still be
    #    reassembled correctly from the right-hand fixed fields.
    # ``_decode_canonical`` also rejects a non-canonical payload segment, so the
    # decoded (draft_id, nonce) — and therefore the single-use key below — cannot
    # be perturbed by a re-encoding of the payload half either.
    try:
        payload = _decode_canonical(payload_b64).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidToken("payload segment is not valid base64url/utf-8") from exc
    fields = payload.rsplit(_SEP, 3)
    if len(fields) != 4:
        raise InvalidToken("payload does not contain exactly four fields")
    draft_id, action, exp_raw, nonce = fields

    # 4. Action scope: reject anything outside the allowed set even though it was
    #    signed — defence in depth against a secret compromise or a future bug in
    #    issue_token that let a bad action through.
    if action not in VALID_ACTIONS:
        raise BadAction(f"decoded action {action!r} is not an allowed action")

    # exp must be an integer epoch second; a non-numeric value means a corrupt
    # (but somehow signed) token — treat as invalid, never as "no expiry".
    try:
        exp = int(exp_raw)
    except ValueError as exc:
        raise InvalidToken("expiry field is not an integer epoch") from exc

    # 5. Expiry: compare against the injected reference time in UTC. A token is
    #    dead at exactly its exp second (>=) so there is no one-second grace.
    now_epoch = int(now.astimezone(timezone.utc).timestamp())
    if now_epoch >= exp:
        raise ExpiredToken("token expiry has passed")

    # 6. Single-use LAST: only ask the (potentially DB-hitting) callback once the
    #    token is otherwise fully valid, so we never do a lookup for a forged or
    #    expired token. The key is the nonce-based single-use key (§14.2), derived
    #    from the SIGNED, canonically-decoded draft_id + nonce — stable across any
    #    re-encoding, so a replayed variant hashes to the SAME key and is caught.
    token_hash = _single_use_key(draft_id, nonce)
    if is_used_callback(token_hash):
        raise UsedToken("token has already been consumed")

    # All gates passed: hand back the trusted, immutable contents.
    return VerifiedToken(draft_id=draft_id, action=action, exp=exp, nonce=nonce)
