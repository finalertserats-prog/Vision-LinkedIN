"""Proactive LinkedIn OAuth token refresh job (BRD §15.3, FR-17).

WHY this module exists: LinkedIn access tokens live ~60 days and refresh tokens
~365 days (§11.6). If the access token silently expires, every publish begins to
fail with 401s and the owner's daily posting quietly breaks. This cron job
(``vision-token``, run daily) closes that gap: it refreshes any access token that
is within the configured window of expiry *before* it dies, and — when a refresh
can no longer succeed (refresh token near/​past expiry, or LinkedIn rejects the
grant) — it emails the owner a "please re-authorise" alert **without losing any
stored state**, so a failed refresh never leaves VISION with no credentials.

SECURITY (threat model §3, hardening checklist):
  * OAuth tokens are stored with **authenticated envelope encryption**
    (AES-256-GCM) via the shared :mod:`vision.publish.crypto` contract — the ONE
    key derivation and AAD scheme used by every path. The wrapping key comes from
    ``settings.token_enc_key`` and is kept entirely separate from the ciphertext
    (which lives in the DB); the account's ``(provider, member_urn)`` is bound in
    as GCM *associated data* (see :func:`crypto.oauth_aad`) so a ciphertext cannot
    be lifted from one account row and replayed into another (tamper/swap defence),
    AND so a token this job refreshes stays openable by the publisher.
  * A one-byte **ciphertext version** prefixes every envelope so the wrapping key
    can be rotated and old ciphertexts re-encrypted in future without ambiguity.
  * Token plaintext exists only transiently in memory; it is **never logged,
    never placed in a CLI argument**, and never included in an alert email or an
    audit row (only non-secret metadata is recorded).
  * A **per-account refresh lock** (atomic O_EXCL lock file) prevents two
    overlapping runs from racing a refresh — a race can otherwise invalidate a
    freshly rotated refresh token (threat model §3 "Refresh races").
  * Token replacement is **atomic**: the new access token, refresh token and
    their expiries are assigned together inside one transaction, so a partial
    write can never leave a half-rotated, unusable credential.

RESILIENCE (BRD §15.4 error matrix): transient LinkedIn failures (429 / 5xx) are
retried with capped exponential backoff (``tenacity``); once retries are
exhausted the account is *dead-lettered* and the owner alerted, never crashed.
A hard rejection (401 / 400 invalid_grant) is not retried — it goes straight to a
re-auth alert, keeping the stored (approved) state intact.

MODES (``settings.vision_env`` — FR-20): ``dry_run`` performs **no** network
refresh and sends **no** email — it only logs what it *would* do, so an
un-configured checkout is completely side-effect free. ``staging`` and ``live``
both perform a real refresh (a token refresh has no "post" to fake, so the two
modes behave identically here).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import gettempdir
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from vision.config import Settings, VisionEnv, get_settings
from vision.db.models import AuditLog, OAuthToken
from vision.db.session import get_session
from vision.logging_setup import configure_logging, get_logger
from vision.mailer.sender import EmailSender, get_sender
from vision.publish import crypto
from vision.publish.errors import (
    LinkedInError,
    NeedsReauth,
    RateLimited,
    TransientLinkedInError,
)
from vision.publish.linkedin import LinkedInClient

_log = get_logger("vision.publish.token_refresh")

# --- Provider constant ------------------------------------------------------
# Only LinkedIn tokens exist today, but keying the query on the provider keeps
# the job correct if a second provider is ever added (config over code).
_PROVIDER: Final[str] = "linkedin"

# --- Default tuning knobs (config over code, all env-overridable) -----------
# How close to expiry (days) an access token must be before we proactively
# refresh it. Default 7 days gives ample margin under the ~60-day access-token
# lifetime while avoiding churn.
_DEFAULT_REFRESH_WINDOW_DAYS: Final[float] = 7.0
# If the *refresh* token itself is within this many days of expiry, a refresh may
# not succeed (or would rotate into an about-to-die token) — so we alert for
# re-auth instead of attempting it. Default 7 days.
_DEFAULT_REFRESH_TOKEN_MIN_TTL_DAYS: Final[float] = 7.0
# Capped retry policy for transient (429/5xx) refresh failures (§15.4).
_DEFAULT_MAX_ATTEMPTS: Final[int] = 5
# Exponential-backoff base multiplier (seconds) and ceiling (seconds).
_DEFAULT_BACKOFF_BASE: Final[float] = 2.0
_DEFAULT_BACKOFF_MAX: Final[float] = 60.0


# --- Outcome type -----------------------------------------------------------
# Frozen (immutable) per the project's immutability principle: once the job has
# decided what happened to an account, callers/tests read the verdict but cannot
# mutate it. A typed object (not a raw dict) gives callers type-checked fields.


@dataclass(frozen=True)
class RefreshOutcome:
    """The result of evaluating one OAuth account for refresh.

    ``status`` is one of the module-level ``STATUS_*`` constants. ``detail`` is a
    short, **non-secret** human string for logs/tests. ``account_id`` is the
    stable token-row id (never a token value).
    """

    account_id: str
    status: str
    detail: str


# Status vocabulary — a closed set so callers/tests branch on known values.
STATUS_HEALTHY: Final[str] = "healthy"  # not near expiry; nothing to do
STATUS_REFRESHED: Final[str] = "refreshed"  # new tokens fetched + stored
STATUS_REAUTH_ALERTED: Final[str] = "reauth_alerted"  # cannot refresh; owner emailed
STATUS_DEAD_LETTERED: Final[str] = "dead_lettered"  # transient retries exhausted; alerted
STATUS_SKIPPED_LOCKED: Final[str] = "skipped_locked"  # another run holds the account lock
STATUS_DRY_RUN_SKIPPED: Final[str] = "dry_run_skipped"  # dry_run: logged, not executed


# --- Internal signalling exception -----------------------------------------


class _RefreshLocked(RuntimeError):
    """Raised internally when another run already holds an account's refresh lock.

    Kept private: it is a control-flow signal converted into a
    ``STATUS_SKIPPED_LOCKED`` outcome, never propagated to the caller (the job
    must not crash just because a sibling run is mid-refresh).
    """


# ===========================================================================
# Envelope encryption (authenticated, versioned, account-bound)
# ===========================================================================


def encrypt_token(
    plaintext: str, key_material: str, *, provider: str = _PROVIDER, member_urn: str
) -> bytes:
    """Encrypt a token into the CANONICAL authenticated envelope (bytes).

    Delegates to :func:`vision.publish.crypto.encrypt` — the one source of truth
    for the envelope layout and HKDF-SHA256 key derivation — binding the token to
    its account via the shared :func:`crypto.oauth_aad` scheme (``provider`` +
    ``member_urn``). WHY delegate: the OAuth save path, the publisher load path, and
    THIS refresh job must all seal/open under exactly one key derivation and one
    AAD, or a freshly refreshed token becomes un-openable at publish time.
    """
    return crypto.encrypt(
        plaintext, key_material, associated_data=crypto.oauth_aad(provider, member_urn)
    )


def decrypt_token(
    blob: bytes, key_material: str, *, provider: str = _PROVIDER, member_urn: str
) -> str:
    """Decrypt a canonical token envelope produced by :func:`encrypt_token`.

    Reconstructs the SAME associated data from the row's ``provider`` +
    ``member_urn`` (via :func:`crypto.oauth_aad`) and delegates to
    :func:`crypto.decrypt`. Any structural problem, unknown version, wrong key, or
    AAD/tag mismatch surfaces as :class:`crypto.CryptoError` (fail-closed) — the
    caller acts on a re-auth alert rather than a half-decrypted value, and no key
    or plaintext detail ever leaks.
    """
    return crypto.decrypt(
        blob, key_material, associated_data=crypto.oauth_aad(provider, member_urn)
    )


# ===========================================================================
# Per-account refresh lock (atomic, cross-platform)
# ===========================================================================


def _default_lock_dir() -> Path:
    """Return the directory used for account lock files.

    Overridable via ``VISION_LOCK_DIR`` (config over code) so an operator can
    point locks at a tmpfs/host path; defaults to the system temp dir so a bare
    checkout still works.
    """
    configured = os.environ.get("VISION_LOCK_DIR")
    return Path(configured) if configured else Path(gettempdir())


def _safe_account_component(account_id: str) -> str:
    """Sanitise an account id into a filesystem-safe lock-file component.

    Token ids are UUIDs and member URNs contain ``:``; both are reduced to
    ``[A-Za-z0-9_-]`` so the lock path is portable across Windows/POSIX.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", account_id)


@contextmanager
def _account_refresh_lock(account_id: str, lock_dir: Path) -> Iterator[None]:
    """Hold an exclusive per-account lock for the duration of a refresh.

    Uses ``os.open`` with ``O_CREAT | O_EXCL`` — an atomic "create only if
    absent" on every mainstream OS — so two concurrent runs can never both enter
    the critical section for the same account. If the lock already exists the
    context raises :class:`_RefreshLocked`, which the caller converts into a
    ``STATUS_SKIPPED_LOCKED`` outcome (the sibling run will do the refresh).

    The lock file is always removed on exit, even on error, so a crashed run does
    not wedge the account permanently for the *next* scheduled run.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"vision-token-refresh-{_safe_account_component(account_id)}.lock"
    try:
        # O_EXCL makes this fail if the file exists — the atomicity we rely on.
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise _RefreshLocked(account_id) from exc
    try:
        # Record the owning pid for operator debugging (not a secret).
        os.write(fd, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            # Already gone (e.g. manual cleanup) — the goal state is "absent".
            pass


# ===========================================================================
# Config helpers (env-overridable defaults)
# ===========================================================================


def _env_float(name: str, default: float) -> float:
    """Read a float tuning knob from the environment, falling back to ``default``.

    A malformed value falls back to the safe default rather than crashing the
    cron job (config over code, fail-safe).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        _log.warning("ignoring non-numeric %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int tuning knob from the environment, falling back to ``default``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("ignoring non-integer %s=%r; using default %s", name, raw, default)
        return default


# ===========================================================================
# Time helpers
# ===========================================================================


def _as_utc(moment: datetime | None) -> datetime | None:
    """Return a timezone-aware UTC datetime (or None), assuming UTC if naive.

    Stored expiries are tz-aware, but a naive value (older row / SQLite edge) is
    coerced to UTC rather than raising, so comparisons never blow up on tzinfo.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ===========================================================================
# Refresh with capped exponential backoff (§15.4)
# ===========================================================================


def _refresh_with_backoff(
    client: LinkedInClient,
    refresh_token: str,
    *,
    max_attempts: int,
    backoff_base: float,
    backoff_max: float,
) -> dict[str, object]:
    """Call ``client.refresh`` retrying only *transient* failures, capped.

    Retries on ``RateLimited`` (429) and ``TransientLinkedInError`` (5xx) with
    exponential backoff via ``tenacity`` (§15.4). ``NeedsReauth`` (401) and any
    other ``LinkedInError`` (e.g. 400 invalid_grant) are **not** retried — they
    propagate immediately so the caller can raise a re-auth alert instead of
    hammering a dead grant. On exhausted transient retries, ``tenacity`` raises
    ``RetryError`` which the caller maps to a dead-letter.
    """
    retryer = Retrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=backoff_base, max=backoff_max),
        retry=retry_if_exception_type((RateLimited, TransientLinkedInError)),
        reraise=False,  # exhausted retries surface as RetryError for the caller
    )
    # The refresh token is passed positionally (never as a CLI arg / never logged).
    return retryer(client.refresh, refresh_token)


# ===========================================================================
# Alerting + audit (never carry token values)
# ===========================================================================


def _send_reauth_alert(
    sender: EmailSender, settings: Settings, account_id: str, reason: str
) -> None:
    """Email the owner that LinkedIn re-authorisation is required.

    Contains only the account id and a short reason — **no token values**. Send
    failures are swallowed (logged) so a mail outage cannot crash the token job;
    the account is still left in a recoverable state.
    """
    subject = "VISION: LinkedIn re-authorisation required"
    body = (
        "VISION could not refresh your LinkedIn access token and needs you to "
        f"re-authorise.\n\nAccount: {account_id}\nReason: {reason}\n\n"
        "Your approved drafts are preserved — please reconnect LinkedIn to resume "
        "publishing."
    )
    html = (
        "<p>VISION could not refresh your LinkedIn access token and needs you to "
        "re-authorise.</p>"
        f"<p><b>Account:</b> {account_id}<br><b>Reason:</b> {reason}</p>"
        "<p>Your approved drafts are preserved — please reconnect LinkedIn to "
        "resume publishing.</p>"
    )
    # get_sender's providers return False (never raise) on ordinary failure; guard
    # anyway so an unexpected provider error cannot escape the job.
    try:
        delivered = sender.send(subject, body, html, to=settings.email_to)
    except Exception:  # noqa: BLE001 - last-resort guard around a 3rd-party sender
        _log.exception("reauth alert send raised for account %s", account_id)
        return
    if not delivered:
        _log.error("reauth alert could not be delivered for account %s", account_id)


def _audit(session: Session, account_id: str, action: str, reason: str, now: datetime) -> None:
    """Append a non-secret audit row for a token-lifecycle event (§11.7).

    Records the account id, action and reason only — never a token value — giving
    a tamper-evident history of refreshes and re-auth alerts for security review.
    """
    session.add(
        AuditLog(
            entity="oauth_token",
            entity_id=account_id,
            action=action,
            actor="vision-token",
            meta={"reason": reason},
            at=now,
        )
    )


# ===========================================================================
# Core per-account evaluation
# ===========================================================================


class _RefreshResponse(BaseModel):
    """Validated shape of a LinkedIn OAuth refresh response (§22.9 fail-closed).

    WHY a schema gate: the raw JSON from LinkedIn is *untrusted input* — a
    malformed or hostile body (missing/null ``access_token``, a non-numeric or
    negative expiry) must be rejected **before** we mutate the stored credential,
    never encrypted as the literal string ``"None"`` and never allowed to raise a
    ``KeyError``/``ValueError`` half-way through the write. Validation here is the
    single choke point that guarantees a stored token is only ever replaced with a
    complete, well-typed value.

    Fields:
      * ``access_token`` — REQUIRED, non-empty (``min_length=1``); a missing,
        null, or empty token is a validation failure, not a stored credential.
      * ``expires_in`` / ``refresh_token_expires_in`` — OPTIONAL, non-negative
        (``ge=0``) integers; LinkedIn may omit them, but a negative or
        non-numeric value is rejected rather than producing a backwards expiry.
      * ``refresh_token`` — OPTIONAL; LinkedIn does not always rotate it.

    Unknown extra fields (``scope``, ``token_type``, …) are ignored, not errors.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str = Field(min_length=1)
    expires_in: int | None = Field(default=None, ge=0)
    refresh_token: str | None = Field(default=None, min_length=1)
    refresh_token_expires_in: int | None = Field(default=None, ge=0)


def _store_refreshed_tokens(
    token: OAuthToken,
    response: _RefreshResponse,
    *,
    key_material: str,
    now: datetime,
) -> None:
    """Atomically replace an account's tokens from a *validated* refresh response.

    Receives an already-validated :class:`_RefreshResponse` (never a raw dict) so
    this function can assume well-typed, non-hostile values. All new values are
    assigned together (access token + expiry, and — only if LinkedIn rotated it —
    the refresh token + its expiry) so the surrounding transaction commits a
    complete, consistent credential or nothing at all. LinkedIn does not always
    rotate the refresh token; when absent we keep the existing one rather than
    wiping it. New ciphertext is sealed under the CANONICAL (provider, member_urn)
    AAD so the refreshed token is loadable by the publisher (the save/load contract
    every path shares).
    """
    token.access_token_enc = encrypt_token(
        response.access_token,
        key_material,
        provider=token.provider,
        member_urn=token.member_urn,
    )
    if response.expires_in is not None:
        token.access_expires_at = now + timedelta(seconds=response.expires_in)

    if response.refresh_token:
        token.refresh_token_enc = encrypt_token(
            response.refresh_token,
            key_material,
            provider=token.provider,
            member_urn=token.member_urn,
        )
        if response.refresh_token_expires_in is not None:
            token.refresh_expires_at = now + timedelta(
                seconds=response.refresh_token_expires_in
            )


def _refresh_one(
    session: Session,
    token: OAuthToken,
    now: datetime,
    *,
    settings: Settings,
    client: LinkedInClient,
    sender: EmailSender,
    lock_dir: Path,
    refresh_window: timedelta,
    refresh_min_ttl: timedelta,
    max_attempts: int,
    backoff_base: float,
    backoff_max: float,
) -> RefreshOutcome:
    """Evaluate and, if needed, refresh a single OAuth account.

    Returns a :class:`RefreshOutcome`; never raises for an operational failure —
    every failure path degrades to an alert/dead-letter outcome so one bad
    account cannot abort the whole run.
    """
    # The token row id is the stable, non-secret account identity used for the
    # encryption AAD, the lock key and audit rows.
    account_id = str(token.id)
    access_expiry = _as_utc(token.access_expires_at)
    refresh_expiry = _as_utc(token.refresh_expires_at)

    # Healthy: access token is comfortably beyond the refresh window → no action.
    if access_expiry is not None and access_expiry > now + refresh_window:
        _log.info("token healthy for account %s", account_id)
        return RefreshOutcome(account_id, STATUS_HEALTHY, "not within refresh window")

    # From here the access token is missing or near expiry → a refresh is due.
    # dry_run must be fully side-effect free: log the intent and stop.
    if settings.vision_env is VisionEnv.DRY_RUN:
        _log.info("dry_run: would refresh token for account %s", account_id)
        return RefreshOutcome(account_id, STATUS_DRY_RUN_SKIPPED, "dry_run: refresh skipped")

    # Guard the whole refresh with the per-account lock so concurrent runs cannot
    # race (which could invalidate a freshly rotated refresh token).
    try:
        with _account_refresh_lock(account_id, lock_dir):
            return _do_locked_refresh(
                session,
                token,
                now,
                account_id=account_id,
                refresh_expiry=refresh_expiry,
                settings=settings,
                client=client,
                sender=sender,
                refresh_min_ttl=refresh_min_ttl,
                max_attempts=max_attempts,
                backoff_base=backoff_base,
                backoff_max=backoff_max,
            )
    except _RefreshLocked:
        # A sibling run owns the lock; it will perform the refresh. Skip cleanly.
        _log.info("refresh already in progress for account %s; skipping", account_id)
        return RefreshOutcome(account_id, STATUS_SKIPPED_LOCKED, "another run holds the lock")


def _do_locked_refresh(
    session: Session,
    token: OAuthToken,
    now: datetime,
    *,
    account_id: str,
    refresh_expiry: datetime | None,
    settings: Settings,
    client: LinkedInClient,
    sender: EmailSender,
    refresh_min_ttl: timedelta,
    max_attempts: int,
    backoff_base: float,
    backoff_max: float,
) -> RefreshOutcome:
    """Perform the refresh while holding the account lock.

    Split from :func:`_refresh_one` so the lock-critical section is small and the
    lock/skip control flow stays readable.
    """
    # If we have no usable refresh token, or it is itself about to expire, a
    # refresh cannot (safely) succeed → alert for re-auth and KEEP state intact.
    refresh_unusable = token.refresh_token_enc is None or (
        refresh_expiry is not None and refresh_expiry <= now + refresh_min_ttl
    )
    if refresh_unusable:
        reason = "refresh token missing or near expiry"
        _log.warning("cannot refresh account %s: %s", account_id, reason)
        _send_reauth_alert(sender, settings, account_id, reason)
        _audit(session, account_id, "reauth_alert", reason, now)
        return RefreshOutcome(account_id, STATUS_REAUTH_ALERTED, reason)

    # Decrypt the refresh token only now, for the shortest possible plaintext
    # lifetime, and only inside the locked section.
    try:
        refresh_token = decrypt_token(
            token.refresh_token_enc or b"",
            settings.token_enc_key,
            provider=token.provider,
            member_urn=token.member_urn,
        )
    except crypto.CryptoError:
        # A record we cannot decrypt (key rotation gone wrong / corruption) is
        # unrecoverable here → alert for re-auth rather than crash.
        reason = "stored refresh token could not be decrypted"
        _log.error("account %s: %s", account_id, reason)
        _send_reauth_alert(sender, settings, account_id, reason)
        _audit(session, account_id, "reauth_alert", reason, now)
        return RefreshOutcome(account_id, STATUS_REAUTH_ALERTED, reason)

    # Attempt the refresh with capped transient-retry backoff.
    try:
        payload = _refresh_with_backoff(
            client,
            refresh_token,
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
        )
    except NeedsReauth:
        # 401 on refresh: the grant is dead. Alert + keep state (§15.4 401).
        reason = "LinkedIn rejected the refresh token (401)"
        _log.warning("account %s: %s", account_id, reason)
        _send_reauth_alert(sender, settings, account_id, reason)
        _audit(session, account_id, "reauth_alert", reason, now)
        return RefreshOutcome(account_id, STATUS_REAUTH_ALERTED, reason)
    except RetryError:
        # Transient (429/5xx) retries exhausted → dead-letter + alert (§15.4).
        reason = "transient refresh failures exhausted retries"
        _log.error("account %s: %s", account_id, reason)
        _send_reauth_alert(sender, settings, account_id, reason)
        _audit(session, account_id, "token_refresh_dead_letter", reason, now)
        return RefreshOutcome(account_id, STATUS_DEAD_LETTERED, reason)
    except LinkedInError as exc:
        # Any other hard LinkedIn error (e.g. 400 invalid_grant / 403) is not
        # retryable → alert for re-auth, keep state.
        reason = f"refresh failed: {exc.__class__.__name__}"
        _log.error("account %s: %s", account_id, reason)
        _send_reauth_alert(sender, settings, account_id, reason)
        _audit(session, account_id, "reauth_alert", reason, now)
        return RefreshOutcome(account_id, STATUS_REAUTH_ALERTED, reason)

    # Validate the (untrusted) refresh JSON BEFORE touching the stored row so a
    # malformed/hostile body can never half-write or corrupt the credential
    # (§22.9 fail-closed). A schema failure routes to re-auth and keeps state.
    try:
        validated = _RefreshResponse.model_validate(payload)
    except ValidationError:
        # Deliberately do NOT log the ValidationError detail — it can echo the
        # offending field values, which may include token material.
        reason = "LinkedIn refresh response failed schema validation"
        _log.error("account %s: %s", account_id, reason)
        _send_reauth_alert(sender, settings, account_id, reason)
        _audit(session, account_id, "reauth_alert", reason, now)
        return RefreshOutcome(account_id, STATUS_REAUTH_ALERTED, reason)

    # Success: atomically store the newly issued (encrypted) tokens.
    _store_refreshed_tokens(
        token,
        validated,
        key_material=settings.token_enc_key,
        now=now,
    )
    _audit(session, account_id, "token_refreshed", "access token refreshed", now)
    _log.info("refreshed token for account %s", account_id)
    return RefreshOutcome(account_id, STATUS_REFRESHED, "access token refreshed")


# ===========================================================================
# Public entry point
# ===========================================================================


def refresh_if_needed(
    session: Session,
    now: datetime,
    *,
    settings: Settings | None = None,
    client: LinkedInClient | None = None,
    sender: EmailSender | None = None,
    lock_dir: Path | None = None,
    max_attempts: int | None = None,
    backoff_base: float | None = None,
    backoff_max: float | None = None,
) -> list[RefreshOutcome]:
    """Refresh every LinkedIn access token that is within the refresh window.

    For each stored LinkedIn OAuth account:
      * **healthy** (access token beyond the window) → left untouched;
      * **near expiry** → refreshed via ``LinkedInClient.refresh``, with the new
        tokens re-encrypted and atomically stored under a per-account lock;
      * **un-refreshable** (refresh token missing/near expiry, or LinkedIn
        rejects the grant) → a re-auth alert is emailed and stored state is kept.

    Dependencies are injected (``settings``/``client``/``sender``/``lock_dir`` and
    the retry knobs) so tests can supply mocks and a temp lock dir; production
    ``main`` passes nothing and gets the real, config-driven objects. Returns one
    :class:`RefreshOutcome` per account; never raises for an operational failure.
    """
    settings = settings or get_settings()
    client = client or LinkedInClient(settings=settings)
    sender = sender or get_sender(settings)
    lock_dir = lock_dir or _default_lock_dir()

    # Resolve tuning knobs: explicit args win, else env, else safe defaults.
    refresh_window = timedelta(
        days=_env_float("TOKEN_REFRESH_WINDOW_DAYS", _DEFAULT_REFRESH_WINDOW_DAYS)
    )
    refresh_min_ttl = timedelta(
        days=_env_float("TOKEN_REFRESH_TOKEN_MIN_TTL_DAYS", _DEFAULT_REFRESH_TOKEN_MIN_TTL_DAYS)
    )
    resolved_attempts = (
        max_attempts
        if max_attempts is not None
        else _env_int("TOKEN_REFRESH_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
    )
    resolved_base = (
        backoff_base
        if backoff_base is not None
        else _env_float("TOKEN_REFRESH_BACKOFF_BASE", _DEFAULT_BACKOFF_BASE)
    )
    resolved_max = (
        backoff_max
        if backoff_max is not None
        else _env_float("TOKEN_REFRESH_BACKOFF_MAX", _DEFAULT_BACKOFF_MAX)
    )

    # Reference time as aware UTC so all window comparisons are unambiguous.
    now = _as_utc(now) or datetime.now(timezone.utc)

    tokens = session.scalars(
        select(OAuthToken).where(OAuthToken.provider == _PROVIDER)
    ).all()

    outcomes: list[RefreshOutcome] = []
    for token in tokens:
        # The per-account id is non-secret and used for logging/dead-lettering.
        account_id = str(token.id)
        try:
            outcome = _refresh_one(
                session,
                token,
                now,
                settings=settings,
                client=client,
                sender=sender,
                lock_dir=lock_dir,
                refresh_window=refresh_window,
                refresh_min_ttl=refresh_min_ttl,
                max_attempts=resolved_attempts,
                backoff_base=resolved_base,
                backoff_max=resolved_max,
            )
        except Exception:  # noqa: BLE001 - last-resort guard: contract §15.4
            # An UNEXPECTED error (a bug, a library fault, an un-mapped exception)
            # in one account must never abort refresh for the remaining accounts.
            # Log the account id ONLY (never the token) and dead-letter it, then
            # let the loop continue so every other account still gets its chance.
            _log.exception("unexpected error refreshing account %s", account_id)
            outcome = RefreshOutcome(
                account_id, STATUS_DEAD_LETTERED, "unexpected error during refresh"
            )
        outcomes.append(outcome)
    return outcomes


def main() -> int:
    """``vision-token`` console entry point (daily cron, BRD §10.2 / §15.3).

    Opens a transactional session (which commits atomically on success / rolls
    back on error), refreshes any near-expiry tokens, and returns a process exit
    code: ``0`` when no account was left needing re-auth, ``1`` when at least one
    account requires the owner to reconnect — so the cron wrapper can surface a
    non-zero status for monitoring without the job itself crashing.
    """
    configure_logging()
    settings = get_settings()
    _log.info("vision-token invoked", extra={"vision_env": settings.vision_env.value})

    # One transaction wraps the whole run so token replacements + audit rows commit
    # together (atomic replacement) or roll back as a unit.
    with get_session() as session:
        outcomes = refresh_if_needed(session, datetime.now(timezone.utc), settings=settings)

    # Summarise without leaking anything sensitive.
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    _log.info("vision-token complete", extra={"counts": counts})

    # Signal a non-zero exit if any account needs manual re-auth or was dead-lettered.
    needs_attention = any(
        o.status in (STATUS_REAUTH_ALERTED, STATUS_DEAD_LETTERED) for o in outcomes
    )
    return 1 if needs_attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
