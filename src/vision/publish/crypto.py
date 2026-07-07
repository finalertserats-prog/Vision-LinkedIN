"""Authenticated envelope encryption for OAuth tokens (BRD §15.3, NFR-05).

WHY this module exists: LinkedIn access/refresh tokens are long-lived bearer
credentials — anyone who reads them can impersonate the owner's account. The
threat model (``prep/security_threatmodel.md`` §3) therefore requires that they
are stored with *authenticated* encryption so that:

  * a database/backup dump reveals only ciphertext (confidentiality), and
  * any tampering with a stored token record is *detected* on decrypt rather
    than silently yielding a forged token (integrity, via the GCM auth tag).

Design (envelope format v1):

    byte 0        : version tag (``_VERSION``) — enables key/format rotation.
    bytes 1..12   : random 96-bit nonce, unique per encryption (never reused).
    bytes 13..end : AES-256-GCM ciphertext with the 128-bit auth tag appended.

The 256-bit content-encryption key is *derived* from ``settings.TOKEN_ENC_KEY``
via HKDF-SHA256, so the operator-supplied secret can be any length/entropy and
the actual AES key is domain-separated (``_HKDF_INFO``) from any other use of the
same secret. The wrapping secret lives in configuration/secret-manager, NEVER
beside the ciphertext (threat model §3: "Never store encryption keys beside
ciphertext").

The ``associated_data`` (the account / member URN) is bound into the GCM tag as
additional authenticated data (AAD). This means a ciphertext encrypted for one
member cannot be transplanted onto another member's row without failing
authentication — closing the record-swap tampering vector (threat model §3
"record/account ID as associated data").

Plaintext tokens are NEVER logged, echoed, or placed in exceptions — errors carry
only a generic reason (threat model §3 "token redaction").
"""

from __future__ import annotations

import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_log = logging.getLogger(__name__)

# --- Envelope format constants ---------------------------------------------
# Single source of truth for the on-disk layout so encrypt/decrypt can never
# drift. Changing the scheme means bumping ``_VERSION`` and teaching ``decrypt``
# to read the old value too (ciphertext versioning / rotation, threat model §3).
_VERSION = 1  # first byte of every ciphertext; rejected if unknown on decrypt
_NONCE_LEN = 12  # 96-bit GCM nonce — the size AES-GCM is designed around
_TAG_LEN = 16  # 128-bit GCM authentication tag appended to the ciphertext
_KEY_LEN = 32  # 256-bit derived key => AES-256-GCM

# Domain-separation label for HKDF so the derived AES key is unrelated to any
# other key derived from the same operator secret (e.g. the HMAC key).
_HKDF_INFO = b"vision:oauth-token-enc:v1"

# Minimum plausible ciphertext: version byte + full nonce + a bare auth tag
# (empty plaintext still produces a tag). Anything shorter is malformed.
_MIN_CIPHERTEXT_LEN = 1 + _NONCE_LEN + _TAG_LEN


class CryptoError(Exception):
    """Raised for any envelope encryption/decryption failure (BRD §22 typed errors).

    WHY a dedicated type: callers (the OAuth glue, token job) must distinguish a
    cryptographic failure — malformed ciphertext, wrong/rotated key, or detected
    tampering — from ordinary application errors, and MUST fail closed when it
    occurs (never fall back to using an unverified token). The message is always
    generic: it never contains key material or plaintext, so it is safe to log.
    """


def _derive_key(key_material: str) -> bytes:
    """Derive the 32-byte AES-256 key from the operator secret via HKDF-SHA256.

    WHY derive rather than use the secret directly: ``settings.TOKEN_ENC_KEY`` is
    an arbitrary-length, possibly low-entropy string, whereas AES-256-GCM needs
    exactly 32 uniformly-random bytes. HKDF-Expand turns the former into the
    latter deterministically (so decryption reproduces the same key) while the
    ``info`` label domain-separates this key from every other use of the secret.

    ``salt=None`` is intentional: a random salt would have to be stored to
    reproduce the key, and the per-record GCM nonce already provides uniqueness;
    HKDF's role here is key derivation/separation, not password stretching.
    """
    if not key_material:
        # Fail closed: an empty encryption key would silently produce a fixed,
        # attacker-guessable key — refuse rather than encrypt insecurely.
        raise CryptoError("token encryption key is empty")
    hkdf = HKDF(algorithm=SHA256(), length=_KEY_LEN, salt=None, info=_HKDF_INFO)
    # HKDF operates on bytes; encode the secret as UTF-8 (config is text).
    return hkdf.derive(key_material.encode("utf-8"))


def encrypt(plaintext: str, key: str, *, associated_data: str) -> bytes:
    """Encrypt ``plaintext`` under ``key``, binding ``associated_data`` into the tag.

    Returns the self-describing envelope ``version || nonce || ciphertext+tag`` as
    raw bytes suitable for the ``LargeBinary`` token columns. A fresh random nonce
    is generated on every call (nonce reuse under the same key would break GCM's
    security), so encrypting the same token twice yields different ciphertext.

    ``associated_data`` (the member/account URN) is authenticated but NOT
    encrypted — it is not secret, but binding it prevents a ciphertext from being
    replayed onto a different account's record (threat model §3).
    """
    if not plaintext:
        # A caller encrypting an empty token is a bug upstream; fail loudly (§22)
        # rather than persist a meaningless record.
        raise CryptoError("refusing to encrypt empty plaintext")

    aesgcm = AESGCM(_derive_key(key))
    # ``os.urandom`` is a CSPRNG — required for GCM nonce uniqueness/security.
    nonce = os.urandom(_NONCE_LEN)
    aad = associated_data.encode("utf-8")
    # AESGCM.encrypt returns ciphertext with the 16-byte tag already appended.
    sealed = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
    # Prepend the version byte and nonce so ``decrypt`` is fully self-contained.
    # NOTE: plaintext is never logged here or anywhere in this module.
    return bytes([_VERSION]) + nonce + sealed


def decrypt(ciphertext: bytes, key: str, *, associated_data: str) -> str:
    """Decrypt an envelope produced by :func:`encrypt`, verifying authenticity.

    Raises :class:`CryptoError` (fail-closed) if the envelope is malformed, its
    version is unknown, the key is wrong/rotated, the ``associated_data`` does not
    match, or the ciphertext was tampered with — in every one of those cases the
    GCM tag check fails and NO plaintext is returned.
    """
    # Strict length + type validation before touching crypto primitives so a
    # truncated/garbage blob yields a clean typed error, not an index error.
    if not isinstance(ciphertext, (bytes, bytearray)):
        raise CryptoError("ciphertext must be bytes")
    if len(ciphertext) < _MIN_CIPHERTEXT_LEN:
        raise CryptoError("ciphertext is too short to be a valid envelope")

    version = ciphertext[0]
    if version != _VERSION:
        # Unknown version => a rotation/format we don't understand. Reject rather
        # than guess (ciphertext versioning, threat model §3).
        raise CryptoError(f"unsupported ciphertext version: {version}")

    nonce = bytes(ciphertext[1 : 1 + _NONCE_LEN])
    sealed = bytes(ciphertext[1 + _NONCE_LEN :])
    aesgcm = AESGCM(_derive_key(key))
    aad = associated_data.encode("utf-8")

    try:
        plaintext = aesgcm.decrypt(nonce, sealed, aad)
    except InvalidTag as exc:
        # Authentication failed: wrong key, wrong AAD, or tampered ciphertext.
        # Chain the cause for debugging but expose only a generic reason — never
        # the key, AAD, or any recovered bytes (threat model §3 redaction).
        raise CryptoError("token authentication failed (wrong key or tampered)") from exc
    return plaintext.decode("utf-8")
