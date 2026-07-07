"""LinkedIn 3-legged OAuth glue over the existing ``LinkedInClient`` (BRD §15.1/§15.3).

WHY this module exists: ``LinkedInClient`` is a *stateless* HTTP boundary — it
builds the authorize URL and exchanges/refreshes tokens but deliberately holds no
tokens and touches no database (see its module docstring). This module is the
stateful glue that ties that client to VISION's persistence and security model:

  * ``start_authorize``  — validate the anti-CSRF ``state`` and hand back the
                           consent URL the owner is sent to (§15.1 step 4).
  * ``handle_callback``  — verify ``state`` (CSRF), exchange the code, resolve the
                           member URN, and store the tokens ENCRYPTED (§15.3).
  * ``save_tokens`` /
    ``load_tokens``      — the encrypt-on-write / decrypt-on-read helpers, with a
                           per-account lock and atomic replacement so concurrent
                           refreshes cannot corrupt or race the stored record
                           (threat model §3: "per-account refresh lock; atomic
                           token replacement").

Security posture (threat model §3):
  * Tokens are encrypted at rest with AES-256-GCM (see ``crypto``), keyed from
    ``settings.TOKEN_ENC_KEY`` and bound to the member URN as associated data.
  * OAuth ``state`` is compared in constant time to defeat CSRF/forged callbacks.
  * No token value is ever logged, put in an exception, or passed as a CLI arg —
    only non-secret metadata (member URN, expiries) appears in audit-friendly logs.
"""

from __future__ import annotations

import hmac
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vision.config import Settings, get_settings

from . import crypto
from .linkedin import LinkedInClient

# Import the model lazily-safe: models register on import, and this is a stored
# credential record, so the dependency is intrinsic to this module's job.
from vision.db.models import OAuthToken

_log = logging.getLogger(__name__)

# Provider label for the ``oauth_tokens`` row — matches the model default and is
# the natural key (with ``member_urn``) for locating a stored credential.
_PROVIDER = "linkedin"

# Per-account refresh/replace locks (threat model §3 "per-account refresh lock").
# WHY a lock per member: two concurrent refreshes racing on the same account can
# invalidate each other's refresh token or write a torn record. A ``defaultdict``
# of ``threading.Lock`` serialises the read-modify-write of a single account's
# row within this process; the DB transaction provides the cross-process guard.
_account_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


class OAuthError(Exception):
    """Base error for the OAuth glue (BRD §22 typed, specific exceptions)."""


class OAuthStateError(OAuthError):
    """Raised when the returned OAuth ``state`` fails CSRF validation (§15.1).

    WHY its own type: a state mismatch is a *security* event (forged or replayed
    callback), not a transient failure — callers must reject the callback and MUST
    NOT proceed to exchange the code. The message never echoes the offending state
    value, so it is safe to log/audit.
    """


def _now_utc() -> datetime:
    """Return the current UTC time.

    Isolated so expiry maths uses one clock source (server-side UTC, threat model
    §1 "server-side UTC Unix time") and tests can monkeypatch a single seam.
    """
    return datetime.now(tz=timezone.utc)


def _expiry_from_seconds(seconds: Any | None) -> datetime | None:
    """Convert a LinkedIn ``expires_in``-style value into an absolute UTC instant.

    LinkedIn returns token lifetimes as *relative* seconds; we persist absolute
    timestamps so the token job can compare against wall-clock time without
    re-deriving the issue moment. Returns ``None`` when LinkedIn omits the field
    (some refresh responses reuse the existing refresh token and its expiry).
    """
    if seconds is None:
        return None
    try:
        # LinkedIn sends an integer; coerce defensively in case it arrives as str.
        ttl = int(seconds)
    except (TypeError, ValueError) as exc:
        raise OAuthError("token response had a non-integer expiry") from exc
    return _now_utc() + timedelta(seconds=ttl)


def _validate_state(returned_state: str, expected_state: str) -> None:
    """Constant-time CSRF check of the OAuth ``state`` (threat model §1/§3).

    WHY ``hmac.compare_digest``: a naive ``==`` on the state can leak, via timing,
    how many leading characters matched, helping an attacker forge a valid state.
    Constant-time comparison returns a uniform result regardless of where the
    mismatch is. Both values must be present and equal, or the callback is
    rejected as forged/replayed.
    """
    if not returned_state or not expected_state:
        raise OAuthStateError("missing OAuth state on callback")
    if not hmac.compare_digest(returned_state, expected_state):
        # Do NOT include either state value in the message (avoid leaking the
        # expected nonce into logs).
        raise OAuthStateError("OAuth state mismatch — rejecting forged callback")


def start_authorize(state: str, *, client: LinkedInClient | None = None) -> str:
    """Return the LinkedIn consent URL for a validated anti-CSRF ``state`` (§15.1).

    The caller generates a cryptographically-random ``state``, stores it in the
    initiating user session (so it can be compared on the callback), and passes it
    here. We validate the nonce is present and non-trivial, then delegate URL
    construction to the existing ``LinkedInClient.build_authorize_url`` — this glue
    adds only the guard, never a second URL implementation (reuse, §22).

    ``client`` is injectable purely to keep tests hermetic; production passes
    nothing and a real client is created.
    """
    if not state or not state.strip():
        # A blank/whitespace state provides no CSRF protection — refuse to start.
        raise OAuthStateError("refusing to build authorize URL without a state nonce")
    li = client or LinkedInClient()
    url = li.build_authorize_url(state)
    # Log the *event*, never the state nonce (it is a CSRF secret for this flow).
    _log.info("oauth_authorize_started", extra={"provider": _PROVIDER})
    return url


def save_tokens(
    session: Session,
    *,
    member_urn: str,
    token_json: dict[str, Any],
    settings: Settings | None = None,
) -> OAuthToken:
    """Encrypt and persist a LinkedIn token bundle for ``member_urn`` (§15.3).

    ``token_json`` is LinkedIn's raw grant response (``access_token``,
    ``refresh_token``, ``expires_in``, ``refresh_token_expires_in``). Both token
    values are encrypted with AES-256-GCM bound to ``member_urn`` as associated
    data, then written to the single ``oauth_tokens`` row for this account —
    updating in place if one exists so there is exactly one live credential per
    member (atomic replacement, threat model §3).

    Concurrency (threat model §3): writers serialise on the DATABASE — a
    ``SELECT ... FOR UPDATE`` row lock plus a ``UNIQUE (provider, member_urn)``
    constraint mean a racing refresh in another process either blocks on the lock
    or fails its INSERT fast (recovered here as an in-place update). The in-process
    ``threading.Lock`` is kept purely as a local optimisation, not the guarantee.
    Returns the persisted (unflushed-secret) row. No token value is logged.
    """
    cfg = settings or get_settings()
    key = cfg.token_enc_key

    # Encrypt BEFORE taking the lock is fine (pure/CPU), but we hold the lock for
    # the read-modify-write of the DB row to keep replacement atomic per account.
    access_plain = token_json.get("access_token")
    refresh_plain = token_json.get("refresh_token")
    if not access_plain:
        # An access token is mandatory; a grant without one is unusable.
        raise OAuthError("token response missing access_token")

    # Bind the ciphertext to the member URN so a row cannot be transplanted.
    access_enc = crypto.encrypt(access_plain, key, associated_data=member_urn)
    # Refresh token may be absent on some refresh responses; only encrypt if present.
    refresh_enc = (
        crypto.encrypt(refresh_plain, key, associated_data=member_urn)
        if refresh_plain
        else None
    )

    access_expires_at = _expiry_from_seconds(token_json.get("expires_in"))
    refresh_expires_at = _expiry_from_seconds(token_json.get("refresh_token_expires_in"))

    def _apply(target: OAuthToken) -> None:
        """Copy the freshly-encrypted material + expiries onto the row.

        Assigning fresh ciphertext (never mutating the old bytes in place) keeps
        the record's history clean and the update immutable at the value level.
        """
        target.access_token_enc = access_enc
        target.access_expires_at = access_expires_at
        if refresh_enc is not None:
            # Only overwrite the refresh token when LinkedIn returned a new one —
            # a refresh response that omits it must not wipe the stored one.
            target.refresh_token_enc = refresh_enc
            target.refresh_expires_at = refresh_expires_at

    # Locate an existing credential for this provider+member. ``with_for_update``
    # takes a row-level lock on Postgres so a concurrent refresher BLOCKS here
    # until we commit — serialising the read-modify-write across processes, not
    # just across threads. On SQLite (dev, single-writer) it is a harmless no-op.
    stmt = (
        select(OAuthToken)
        .where(
            OAuthToken.provider == _PROVIDER,
            OAuthToken.member_urn == member_urn,
        )
        .with_for_update()
    )

    # The in-process lock is now only an OPTIMISATION (avoids two local threads
    # racing to the DB); the UNIQUE (provider, member_urn) constraint is the real
    # cross-process guard (threat model §3 "atomic token replacement").
    with _account_locks[member_urn]:
        row = session.execute(stmt).scalar_one_or_none()

        if row is not None:
            # Existing credential — replace in place inside the caller's tx.
            _apply(row)
            session.flush()
        else:
            # No row yet: INSERT inside a SAVEPOINT so a lost race (a concurrent
            # process committed the same (provider, member_urn) first) surfaces as
            # an IntegrityError we can RECOVER from — re-read the winner's row and
            # update it — instead of leaving a duplicate or aborting the whole tx.
            row = OAuthToken(provider=_PROVIDER, member_urn=member_urn)
            _apply(row)
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                # Lost the insert race; the savepoint has rolled back the failed
                # INSERT. Re-select FOR UPDATE (the winner's row now exists) and
                # update it in place so there is still exactly one live credential.
                row = session.execute(stmt).scalar_one()
                _apply(row)
                session.flush()
                _log.info(
                    "oauth_token_insert_race_recovered",
                    extra={"provider": _PROVIDER, "member_urn": member_urn},
                )

    # Metadata-only log: member + expiries are not secret; tokens never appear.
    _log.info(
        "oauth_tokens_saved",
        extra={
            "provider": _PROVIDER,
            "member_urn": member_urn,
            "access_expires_at": access_expires_at.isoformat() if access_expires_at else None,
        },
    )
    return row


def load_tokens(
    session: Session,
    *,
    member_urn: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Load and DECRYPT the stored token bundle for ``member_urn`` (§15.3).

    Returns a dict with the plaintext ``access_token`` / ``refresh_token`` (the
    latter may be ``None``) plus the absolute expiry instants. The plaintext lives
    only in the returned dict for the caller's immediate use — it is never logged
    and its in-memory lifetime should be kept short (threat model §3 "restrict
    plaintext token lifetime in memory").

    Raises :class:`OAuthError` if no credential exists; decryption failures
    surface as :class:`crypto.CryptoError` (fail-closed — a tampered/rotated
    record must NOT yield a usable token).
    """
    cfg = settings or get_settings()
    key = cfg.token_enc_key

    stmt = select(OAuthToken).where(
        OAuthToken.provider == _PROVIDER,
        OAuthToken.member_urn == member_urn,
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None or row.access_token_enc is None:
        raise OAuthError(f"no stored LinkedIn tokens for member {member_urn}")

    # Decrypt with the SAME associated data used at encryption time (the member
    # URN); a mismatch here would fail authentication and raise CryptoError.
    access_token = crypto.decrypt(row.access_token_enc, key, associated_data=member_urn)
    refresh_token = (
        crypto.decrypt(row.refresh_token_enc, key, associated_data=member_urn)
        if row.refresh_token_enc is not None
        else None
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_expires_at": row.access_expires_at,
        "refresh_expires_at": row.refresh_expires_at,
        "member_urn": member_urn,
    }


def handle_callback(
    session: Session,
    *,
    code: str,
    state: str,
    expected_state: str,
    client: LinkedInClient | None = None,
    settings: Settings | None = None,
) -> str:
    """Complete the OAuth callback: verify CSRF, exchange, store encrypted (§15.1/§15.3).

    Flow (order matters for security):
      1. Validate ``state`` against ``expected_state`` in constant time — a
         mismatch means a forged/replayed callback and aborts BEFORE any token is
         requested (never spend a code on an unverified callback).
      2. Exchange the authorization ``code`` for tokens via the existing client.
      3. Resolve the member URN from the fresh access token (OpenID ``sub``, §6) —
         this is the author identity AND the associated data the tokens are bound
         to, so it must be known before encryption.
      4. Persist the tokens ENCRYPTED and bound to that member URN.

    Returns the authenticated ``member_urn``. Raises :class:`OAuthStateError` on
    CSRF failure; propagates ``LinkedInError`` subclasses from the client on HTTP
    failure. The ``code`` is never logged (it is a one-time credential).
    """
    # Step 1 — CSRF gate. Fail closed before touching LinkedIn.
    _validate_state(state, expected_state)

    li = client or LinkedInClient(settings=settings)

    # Step 2 — trade the one-time code for tokens (client handles TLS/secret).
    token_json = li.exchange_code(code)
    access_token = token_json.get("access_token")
    if not access_token:
        # A 2xx with no access token is a contract violation; fail loudly (§22).
        raise OAuthError("token exchange returned no access_token")

    # Step 3 — resolve the member URN (author identity + encryption AAD).
    member_urn = li.get_member_urn(access_token)

    # Step 4 — encrypt-at-rest and persist atomically for this account.
    save_tokens(
        session,
        member_urn=member_urn,
        token_json=token_json,
        settings=settings,
    )

    _log.info("oauth_callback_completed", extra={"member_urn": member_urn})
    return member_urn
