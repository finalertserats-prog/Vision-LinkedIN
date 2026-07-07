"""Real LinkedIn publisher worker (BRD §15.2/§15.4, FR-12/13/14).

WHY this module exists: Phase 2 wired a *mock* publisher behind
``vision.approval.service.PublisherPort`` (``NoopPublisher``) so the approval
loop could be built and tested without ever touching LinkedIn. This module is the
Phase-3 REAL implementation: :class:`LinkedInPublisher` drives an *approved,
durable* draft all the way to a live LinkedIn post — decrypting the stored OAuth
token, uploading an approved image (if any), posting via the Posts API, recording
the resulting URN, advancing the draft's state machine, and emailing the owner a
confirmation.

DESIGN / SECURITY invariants realised here (BRD §15.4 + threat model §3):

  * **At-most-once publish (idempotency).** The idempotency key is the draft id.
    A draft whose ``post_urn`` is already set is a NO-OP (never re-posted): the
    Posts API is not natively idempotent, so the guard lives above the client.
    Before any network call the worker also *claims* the draft with a guarded
    compare-and-set (``scheduled`` -> ``queued``) so a second concurrent runner
    cannot post the same draft.

  * **Authenticated envelope encryption for tokens.** Access/refresh tokens are
    stored as AES-256-GCM ciphertext (versioned envelope: ``version || nonce ||
    ct+tag``) with the wrapping key derived from ``settings.TOKEN_ENC_KEY`` — the
    key lives in config/secret-manager, SEPARATE from the ciphertext in the DB
    (threat model §3). Tokens are decrypted only into local variables, never
    logged, never placed on a CLI argument, and are re-encrypted atomically on
    refresh under a per-account lock (defends the "refresh race" DoS).

  * **§15.4 error matrix.** 401 -> refresh the token and retry ONCE; if still 401,
    alert the owner to re-authorise and KEEP the approved draft (never lost). 403
    -> alert (hard config error, not retryable). 429 / 5xx -> exponential backoff
    via ``tenacity``, capped; on exhaustion the draft is dead-lettered and an
    alert is raised. The typed exceptions from :mod:`vision.publish.errors` drive
    the branching so no HTTP status code leaks into this business logic.

  * **Graceful image degradation.** An approved image is attached when present,
    but ANY image failure (upload error, unreadable file) degrades to a text-only
    post — an image is never allowed to block publishing (BRD §13.6).

  * **Run modes (FR-20).** ``dry_run`` logs and posts NOTHING; ``staging``
    publishes a clearly-marked test post and immediately deletes it (the §18.1
    post-then-delete E2E); ``live`` performs the real publish + confirmation.

Everything security-critical (state CAS, audit rows) reuses the existing
:mod:`vision.approval.state_machine` for the edges it models; the two edges that
involve the wired ``scheduled`` state (which predates that enum) use a local
guarded CAS that mirrors :mod:`vision.approval.service`.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from vision.approval.state_errors import TransitionConflict
from vision.approval.state_machine import DraftState, transition
from vision.config import Settings, SignatureMode, VisionEnv, get_settings
from vision.council.compose import find_forbidden_name
from vision.db.models import AuditLog, Draft, OAuthToken
from vision.logging_setup import get_logger
from vision.mailer.sender import EmailSender, get_sender
from vision.publish import crypto
from vision.publish.errors import (
    LinkedInError,
    NeedsReauth,
    RateLimited,
    TransientLinkedInError,
)
from vision.publish.linkedin import LinkedInClient

_log = get_logger(__name__)

# The actor recorded on every audit row this worker writes (threat model §1
# repudiation control: a system actor, distinct from the human 'owner').
_ACTOR = "vision-publisher"

# The DB ``provider`` value for LinkedIn tokens (§11.6). A constant, not a literal
# scattered at call sites, so the token lookup is auditable in one place.
_LINKEDIN_PROVIDER = "linkedin"

# Draft states this worker treats as "approved & ready to publish". The wired
# approval loop (``vision.approval.service.approve``) sets ``scheduled``; the
# richer state_machine vocabulary uses ``approved`` — we accept BOTH so the worker
# is correct today and forward-compatible if the approval path is re-wired later.
PUBLISHABLE_STATES: frozenset[str] = frozenset({"scheduled", "approved"})

# The transient in-flight state (a draft this worker has claimed). Mirrors the
# state_machine ``QUEUED`` value; a draft here is being published right now.
_CLAIMED_STATE = DraftState.QUEUED.value  # "queued"

# States the poll loop will DRIVE: the approved-and-ready states plus the claimed
# ``queued`` state. Including ``queued`` is what lets a draft stranded by a crash
# (or a publish whose outcome was unknown) be RE-DRIVEN and reconciled, instead of
# sitting forever in an un-polled state and being silently lost (ISSUE 4/5).
_REDRIVE_STATES: frozenset[str] = PUBLISHABLE_STATES | frozenset({_CLAIMED_STATE})

# Where the per-draft publish idempotency marker + lease live. Persisted inside the
# existing ``drafts.model_trace`` JSON column (no schema change), nested under this
# key so it never collides with the generation/critique model trace already stored
# there. The marker is written BEFORE the create call so an at-most-once publish is
# recoverable across a crash (threat model — "idempotency keys prevent duplicate
# LinkedIn posts during retries").
_PUBLISH_META_KEY = "publish"

# Default lease duration: how long a single publish attempt may hold a claimed
# draft before another runner is allowed to reconcile + re-drive it. Long enough to
# outlast a normal publish + backoff, short enough that a crashed runner's draft is
# reclaimed promptly.
_DEFAULT_LEASE_TTL = timedelta(minutes=10)

# Default "stuck" grace beyond lease expiry after which the reaper ALERTS: a draft
# still queued this long past its lease is treated as an approved post at risk of
# being silently lost, and the owner is notified (ISSUE 4 reaper/alert).
_DEFAULT_STUCK_AFTER = timedelta(hours=1)

# --- Council publishing (§5 evolution) --------------------------------------
# The sentinel ``content_mode`` marking a council-generated draft. A constant so
# the branch that assembles the extra Council block is auditable in one place.
_COUNCIL_MODE = "council"

# The ONLY attribution a council post ever carries (all three voices are de-named
# upstream — see the council composer). It is the exact, owner-approved wording and
# must appear EXACTLY ONCE in the published text, so it is a single constant that
# both the de-dup strip and the re-append below share.
_BRAHMASTRA_SIGNATURE = "Powered by Brahmastra"

class PublisherError(Exception):
    """Base for publisher-layer failures that are not raw LinkedIn errors."""


class TokenDecryptError(PublisherError):
    """The stored token ciphertext failed authenticated decryption.

    WHY its own type: a bad tag means tampering, a wrong/rotated key, or a
    corrupt row — all fail-closed (never publish with an unverifiable token) and
    all surface an operator alert, distinct from an ordinary LinkedIn error.
    """


class MissingTokens(PublisherError):
    """No usable LinkedIn OAuth token is on record — the owner must (re)authorise."""


class ReauthRequired(PublisherError):
    """A 401 persisted even after a token refresh — re-authorisation is required.

    The approved draft is deliberately KEPT (not dead-lettered) so it publishes
    automatically once the owner re-authorises (BRD §15.4 401).
    """


class ForbiddenNameInPost(PublisherError):
    """The FINAL LinkedIn text names an AI/model/vendor — publish FAILS CLOSED.

    The #1 published-text rule is that NO model name reaches LinkedIn. The council
    composer already fails closed on a leak, but this is the belt-and-braces gate
    on the EXACT bytes about to be posted (body + Council block + signature +
    staging marker) — so an edited draft, a pre-composer draft, or any future path
    that assembles text is re-checked before a single network call. Carries the
    offending ``match`` (token only) for the alert; the surrounding text is never
    logged. Fatal by design — a leak aborts publish rather than shipping.
    """

    def __init__(self, match: str) -> None:
        self.match = match
        super().__init__(f"final post text names a forbidden AI/model: {match!r}")


def encrypt_token(
    plaintext: str, key: str, *, provider: str = _LINKEDIN_PROVIDER, member_urn: str
) -> bytes:
    """Encrypt a token into the CANONICAL AES-256-GCM envelope (bytes).

    Delegates to :func:`vision.publish.crypto.encrypt` — the single source of
    truth for the envelope layout and the HKDF-SHA256 key derivation — binding the
    token to its account via the shared :func:`crypto.oauth_aad` scheme
    (``provider`` + ``member_urn``). WHY delegate rather than re-implement: the OAuth
    save path, this publisher load path, and the token-refresh job MUST agree on
    exactly one key derivation AND one AAD, or a token sealed at OAuth time cannot
    be opened at publish time. Returns raw bytes for the ``LargeBinary`` column.
    """
    return crypto.encrypt(plaintext, key, associated_data=crypto.oauth_aad(provider, member_urn))


def decrypt_token(
    blob: bytes, key: str, *, provider: str = _LINKEDIN_PROVIDER, member_urn: str
) -> str:
    """Decrypt a canonical token envelope back to its plaintext string.

    Reconstructs the SAME associated data used at encryption time from the row's
    ``provider`` + ``member_urn`` (via :func:`crypto.oauth_aad`) and delegates to
    :func:`crypto.decrypt`. Fails closed: any structural problem, unknown version,
    wrong/rotated key, or AAD/tag mismatch surfaces as :class:`TokenDecryptError`
    (translated from :class:`crypto.CryptoError`) so the worker never publishes
    with an unverifiable token — and never logs or returns the token plaintext.
    """
    try:
        return crypto.decrypt(
            blob, key, associated_data=crypto.oauth_aad(provider, member_urn)
        )
    except crypto.CryptoError as exc:
        # Re-type into the publisher's error taxonomy so the §15.4 error matrix can
        # branch on it; the message stays generic (no key/AAD/plaintext leaked).
        raise TokenDecryptError("token failed authenticated decryption") from exc


# --- Per-account refresh lock (threat model §3 DoS) -------------------------
# A process-wide registry of per-provider locks so two threads never refresh the
# same account's token concurrently (a refresh race can invalidate a freshly
# minted token). The guard protects the registry itself.
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_registry_guard = threading.Lock()


def _refresh_lock_for(provider: str) -> threading.Lock:
    """Return the singleton refresh lock for ``provider``, creating it once."""
    with _refresh_registry_guard:
        lock = _refresh_locks.get(provider)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[provider] = lock
        return lock


def _parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 string back to an aware ``datetime``, or ``None``.

    Lease timestamps are stored as ISO strings inside the JSON ``model_trace``
    column. A missing/malformed value must never crash the lease check — it
    degrades to ``None`` (treated as "no lease", i.e. reclaimable), so a corrupt
    marker fails toward reconciliation rather than stranding the draft.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _post_url_from_urn(post_urn: str) -> str:
    """Build the public post URL from a LinkedIn post URN (§15.2).

    LinkedIn does not return a browser URL on create, only the URN; the canonical
    feed-update permalink is derived from it so the confirmation email can link
    the owner straight to their live post.
    """
    return f"https://www.linkedin.com/feed/update/{post_urn}/"


def _strip_brahmastra_signature(block: str) -> str:
    """Return ``block`` with a trailing 'Powered by Brahmastra' line removed.

    WHY: the council composer bakes the signature into the Council block, but the
    publisher re-appends exactly one signature under ``POST_SIGNATURE_MODE`` control
    — so the block must be handed on WITHOUT its own trailing sign-off or the post
    would be signed twice. Case-insensitive on the final non-empty line only; if the
    block does not end with the signature it is returned unchanged (trimmed). Builds
    a new string — the input is never mutated (§22 immutability).
    """
    lines = block.rstrip().splitlines()
    # Drop trailing blank lines, then a final signature line if present.
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip().casefold() == _BRAHMASTRA_SIGNATURE.casefold():
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines).rstrip()


class LinkedInPublisher:
    """The Phase-3 real publisher: turns an approved draft into a live post.

    Collaborators are injected so the whole worker is unit-testable with NO real
    network and NO real post (BRD §18): ``client`` is a :class:`LinkedInClient`
    (mocked in tests), ``mailer`` is an :class:`EmailSender` (mocked in tests),
    and ``sleep`` / ``now`` are injectable clocks so backoff and timestamps are
    deterministic under test.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: LinkedInClient | None = None,
        mailer: EmailSender | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        lease_ttl: timedelta = _DEFAULT_LEASE_TTL,
        stuck_after: timedelta = _DEFAULT_STUCK_AFTER,
        owner_id: str | None = None,
    ) -> None:
        # One validated settings source (§22 config-over-code).
        self._settings = settings or get_settings()
        # Track ownership so ``close`` only tears down a client WE created (never a
        # caller-injected mock).
        self._owns_client = client is None
        self._client = client or LinkedInClient(self._settings)
        # Mailer is built lazily from settings if not injected, so constructing a
        # publisher never requires email config until a send actually happens.
        self._mailer = mailer
        # Injectable clock + sleeper keep backoff and audit timestamps hermetic.
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Default sleeper is a no-op so tests never actually wait during backoff;
        # production passes ``time.sleep`` (or leaves it and relies on the client
        # timeout) via the constructor when real pacing is wanted.
        self._sleep: Callable[[float], None] = sleep if sleep is not None else (lambda _seconds: None)
        # Retry policy knobs (§15.4) — capped attempts + bounded exponential wait.
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        # Lease knobs: how long a claim is held, and how long past expiry a still-
        # queued draft is considered "stuck" (reaper alert). Injectable for tests.
        self._lease_ttl = lease_ttl
        self._stuck_after = stuck_after
        # A per-runner identity recorded on each lease so ops can tell which runner
        # holds a claim (and a crashed runner apart from a live one). Random per
        # process unless injected for deterministic tests.
        self._owner_id = owner_id or uuid.uuid4().hex

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release the underlying HTTP client if this worker created it."""
        if self._owns_client:
            self._client.close()

    # -- public API ---------------------------------------------------------

    def publish(self, session: Session, draft: Draft) -> Draft:
        """Publish a single approved ``draft`` at most once; return the draft.

        Steps (BRD §15.2/§15.4):
          1. Idempotency guard — a draft with a stored ``post_urn`` is a no-op.
          2. Compose the post text (signature footer + staging marker) and load
             the approved image bytes (if any, degrading on failure).
          3. ``dry_run`` -> log and return WITHOUT posting or mutating state.
          4. Load + decrypt the OAuth token; missing/undecryptable -> reauth alert.
          5. Claim the draft (guarded CAS) so it cannot be double-published.
          6. Publish with the §15.4 error policy (refresh-on-401, backoff-on-5xx).
          7. ``staging`` -> delete the marked test post and restore state.
          8. ``live`` -> record the URN, transition to ``published``, email the
             owner a confirmation exactly once.
        """
        draft_id = str(draft.id)

        # (1) Idempotency: at-most-once. A published draft is never re-posted.
        if draft.post_urn:
            _log.info(
                "publish no-op: draft already published (idempotent)",
                extra={"draft_id": draft_id, "post_urn": draft.post_urn},
            )
            return draft

        env = self._settings.vision_env
        text = self._compose_text(draft)
        image_bytes = self._load_image_bytes(draft)

        # (3) DRY_RUN: the safest mode — prove the pipeline without any side effect.
        if env is VisionEnv.DRY_RUN:
            _log.info(
                "dry_run: not publishing (no post, no state change)",
                extra={"draft_id": draft_id, "has_image": image_bytes is not None},
            )
            return draft

        # (4) Credentials. A missing/undecryptable token is fatal for THIS draft
        # but recoverable: alert the owner to (re)authorise and keep the draft.
        try:
            token_row, access_token, member_urn = self._load_credentials(session)
        except (MissingTokens, TokenDecryptError) as exc:
            _log.error(
                "publish blocked: credentials unavailable",
                extra={"draft_id": draft_id, "reason": exc.__class__.__name__},
            )
            self._alert_reauth(draft, reason=exc.__class__.__name__)
            return draft

        # (5) Claim so a concurrent runner cannot also post this draft. The claim
        # persists the durable idempotency marker + lease BEFORE any create, and —
        # for a draft already in ``queued`` (a crash re-drive) — RECONCILES rather
        # than blindly re-posting. A False return means "not ours to publish now".
        if not self._claim(session, draft, access_token, member_urn, text):
            _log.info(
                "publish skipped: draft not claimable (already handled or racing)",
                extra={"draft_id": draft_id, "state": draft.state},
            )
            return draft

        # (6) Publish under the §15.4 error matrix.
        try:
            post_urn, image_urn, used_token = self._publish_with_policy(
                session, token_row, access_token, member_urn, text, image_bytes
            )
        except ReauthRequired:
            # 401 persisted after a refresh: keep the approved draft, alert owner.
            self._revert_claim(session, draft)
            self._alert_reauth(draft, reason="unauthorised_after_refresh")
            return draft
        except RateLimited as exc:
            # 429 exhausted its backoff budget. A 429 is a KNOWN rejection — the
            # request was throttled, never processed, so NO post was created — which
            # makes giving up safe: dead-letter + alert (no post can be stranded).
            self._fail(session, draft, terminal=True)
            self._alert_failure(draft, exc, terminal=True)
            return draft
        except TransientLinkedInError as exc:
            # 5xx / timeout AFTER the request was sent: an UNKNOWN outcome. LinkedIn
            # may already have created the (non-idempotent) post, so we must NEVER
            # blind-retry the create (ISSUE 6). Leave the draft CLAIMED (``queued``)
            # with its durable idempotency marker so the next poll RECONCILES, and
            # alert so an approved post is never silently lost (ISSUE 4).
            _log.warning(
                "publish outcome unknown (transient after send); left for reconciliation",
                extra={"draft_id": draft_id, "reason": exc.__class__.__name__},
            )
            self._alert_unknown_outcome(draft, exc)
            return draft
        except LinkedInError as exc:
            # Non-retryable hard error (e.g. 403 forbidden): alert; leave the draft
            # in ``publish_failed`` (recoverable by ops) rather than dead-lettering.
            self._fail(session, draft, terminal=False)
            self._alert_failure(draft, exc, terminal=False)
            return draft

        # (7) STAGING: prove the end-to-end path, then remove the test post so it
        # never lingers on the profile, and restore the draft to its approved slot.
        if env is VisionEnv.STAGING:
            self._client.delete(used_token, post_urn)
            self._revert_claim(session, draft)
            _log.info(
                "staging: published then deleted marked test post",
                extra={"draft_id": draft_id, "post_urn": post_urn},
            )
            return draft

        # (8) LIVE success: persist the URN + advance the state machine atomically,
        # then send the confirmation exactly once.
        self._finalise_published(session, draft, post_urn, image_urn)
        self._send_confirmation(draft)
        _log.info(
            "draft published",
            extra={"draft_id": draft_id, "post_urn": post_urn, "has_image": bool(image_urn)},
        )
        return draft

    def poll_and_publish(self, session: Session, now: datetime) -> int:
        """Publish every approved draft due at/before ``now``; return the count.

        The cron entry point's core loop (BRD §10.2): select drafts that are
        approved (``scheduled``/``approved``) OR stranded in ``queued`` by a crash /
        unknown outcome, that are due (``scheduled_for <= now``) and not yet
        published (``post_urn IS NULL``), then publish/reconcile each. Including
        ``queued`` is what lets a stranded, approved draft be re-driven (and
        reconciled, never double-posted) instead of being silently lost. One
        draft's failure never aborts the batch — the worker logs and moves on so a
        single bad draft cannot stall the whole run.
        """
        drafts = (
            session.query(Draft)
            .filter(
                Draft.state.in_(tuple(_REDRIVE_STATES)),
                Draft.scheduled_for.isnot(None),
                Draft.scheduled_for <= now,
                Draft.post_urn.is_(None),
            )
            .all()
        )
        _log.info("publisher poll: %d due draft(s)", len(drafts))

        published = 0
        for draft in drafts:
            draft_id = str(draft.id)
            try:
                self.publish(session, draft)
            except (PublisherError, LinkedInError):
                # publish() handles its own error matrix; this is a belt-and-braces
                # guard so an unexpected escape still doesn't kill the batch.
                _log.exception("draft publish failed in poll loop", extra={"draft_id": draft_id})
                continue
            # A set post_urn is the durable proof the draft actually went live.
            if draft.post_urn:
                published += 1
        return published

    # -- text + image assembly ---------------------------------------------

    def _compose_text(self, draft: Draft) -> str:
        """Assemble the final post text: body + optional footer + staging marker.

        A COUNCIL draft (``content_mode == 'council'``) is assembled differently:
        the post body, then the unnamed Council block, then a SINGLE
        'Powered by Brahmastra' attribution — see :meth:`_compose_council_text`. A
        news draft keeps its historic behaviour: the ``POST_SIGNATURE_TEXT`` footer
        is appended only when ``POST_SIGNATURE_MODE`` selects a text footer (D9). In
        ``staging`` the text is clearly marked as an auto-deleted test so that, if
        the delete ever fails, the stray post is unmistakably identifiable.
        """
        if self._is_council(draft):
            text = self._compose_council_text(draft)
        else:
            text = draft.post_text or ""
            mode = self._settings.post_signature_mode
            if (
                mode in {SignatureMode.TEXT_FOOTER, SignatureMode.BOTH}
                and self._settings.post_signature_text
            ):
                # Two newlines keep the footer visually separate from the body.
                text = f"{text}\n\n{self._settings.post_signature_text}"

        # FINAL de-naming gate — FAIL CLOSED on the exact content about to be
        # published, BEFORE the VISION-internal staging marker is prepended (the
        # marker is our own text, not user/model content). WHY here and not only in
        # the composer: this is the single choke point every draft (council, news,
        # or human-edited) flows through on its way to LinkedIn, so a forbidden
        # name can never slip past — regardless of how the text was assembled.
        leak = find_forbidden_name(text)
        if leak is not None:
            _log.error(
                "final post text names a forbidden AI/model; aborting publish",
                extra={"draft_id": str(getattr(draft, "id", "")), "forbidden_match": leak},
            )
            raise ForbiddenNameInPost(leak)

        if self._settings.vision_env is VisionEnv.STAGING:
            text = f"[VISION STAGING TEST — auto-deleted]\n\n{text}"
        return text

    @staticmethod
    def _is_council(draft: Draft) -> bool:
        """Return whether ``draft`` is a council draft with usable council meta.

        Read defensively (``getattr``) so a draft/ORM row that predates the
        ``content_mode`` / ``council_meta`` columns is simply treated as a news
        draft rather than raising (the mailer applies the same guard). Requires BOTH
        the council content_mode AND a non-empty ``council_meta``: without the meta
        there is no Council block to assemble, so we fall back to the news path.
        """
        mode = getattr(draft, "content_mode", None)
        meta = getattr(draft, "council_meta", None)
        return mode == _COUNCIL_MODE and isinstance(meta, dict) and bool(meta)

    def _compose_council_text(self, draft: Draft) -> str:
        """Assemble a council post: body + Council block + ONE Brahmastra sign-off.

        The final LinkedIn text is ``post_text`` + a blank line + the unnamed
        'Council' block + a single ``Powered by Brahmastra`` line. WHY the explicit
        de-dup: the council composer already ends its Council block with
        'Powered by Brahmastra', so blindly appending the signature would sign the
        post TWICE. We therefore strip any trailing signature from the block first
        and re-append exactly one — honouring "appears exactly once" regardless of
        whether the upstream block carried it. ``POST_SIGNATURE_MODE`` is respected:
        the textual attribution is added only when the mode selects a text footer
        (``text_footer``/``both``); ``off``/``card_watermark`` leave the sign-off to
        the card, so the council text is body + block with NO doubled signature.
        """
        post_text = (draft.post_text or "").rstrip()
        # Owner decision (2026-07-08): the public LinkedIn post is JUST the body —
        # NO Council block AND NO 'Powered by Brahmastra' sign-off by default, so the
        # post reads as authored by the owner (a visible signature signals "an AI
        # wrote this" and undercuts authenticity — BRD D9). The Council block stays
        # in ``council_meta`` for the approval email only. The signature remains
        # available as an opt-in: it is appended ONLY when POST_SIGNATURE_MODE selects
        # a text footer (``text_footer``/``both``); ``off``/``card_watermark`` = clean post.
        mode = self._settings.post_signature_mode
        if mode in {SignatureMode.TEXT_FOOTER, SignatureMode.BOTH}:
            return f"{post_text}\n\n{_BRAHMASTRA_SIGNATURE}"
        return post_text

    def _load_image_bytes(self, draft: Draft) -> bytes | None:
        """Return the approved image bytes, or ``None`` to fall back to text-only.

        An image is attached only when the visual lane approved one
        (``image_type != 'none'`` with a path) AND images are enabled. ANY read
        problem degrades to text-only — an image must never block a publish
        (BRD §13.6). Reads are done with ``pathlib`` per §22.
        """
        if not self._settings.image_enabled:
            return None
        if draft.image_type == "none" or not draft.image_path:
            return None
        path = Path(draft.image_path)
        try:
            if path.exists() and path.stat().st_size > 0:
                return path.read_bytes()
        except OSError as exc:
            # File vanished / permissions / IO error — degrade, don't crash.
            _log.warning(
                "approved image unreadable; degrading to text-only",
                extra={"draft_id": str(draft.id), "error": exc.__class__.__name__},
            )
            return None
        # Path missing or empty: same graceful degradation.
        _log.warning(
            "approved image missing or empty; degrading to text-only",
            extra={"draft_id": str(draft.id), "image_path": str(path)},
        )
        return None

    # -- credentials --------------------------------------------------------

    def _load_credentials(self, session: Session) -> tuple[OAuthToken, str, str]:
        """Load the LinkedIn token row and return ``(row, access_token, member_urn)``.

        Decrypts the access token into a LOCAL variable (never logged, never a CLI
        arg — threat model §3). The author URN comes from the stored ``member_urn``
        when present, else a single userinfo lookup, so publishing does not need a
        redundant round-trip.
        """
        row = (
            session.query(OAuthToken)
            .filter(OAuthToken.provider == _LINKEDIN_PROVIDER)
            .order_by(OAuthToken.created_at.desc())
            .first()
        )
        if row is None or row.access_token_enc is None:
            raise MissingTokens("no LinkedIn OAuth token on record")
        # ``member_urn`` is NOT NULL and half the AAD, so it is read FIRST and used
        # to reconstruct the exact associated data the OAuth save path sealed with
        # — the two paths must agree on (provider, member_urn) or decrypt fails.
        member_urn = row.member_urn
        access_token = decrypt_token(
            row.access_token_enc,
            self._settings.token_enc_key,
            provider=row.provider,
            member_urn=member_urn,
        )
        return row, access_token, member_urn

    def _refresh_access_token(self, session: Session, token_row: OAuthToken) -> str | None:
        """Refresh the access token under a per-account lock; return the new token.

        Returns ``None`` when refresh is impossible (no refresh token) or fails —
        the caller then surfaces a re-auth alert. On success the new access token
        (and any rotated refresh token / expiries) is re-encrypted and written
        back ATOMICALLY (single commit), so a crash can never leave a half-updated
        credential (threat model §3 "atomic token replacement").
        """
        lock = _refresh_lock_for(token_row.provider)
        # Serialise refreshes for this account so racing runners can't clobber a
        # freshly minted token (threat model §3 refresh-race DoS).
        with lock:
            if token_row.refresh_token_enc is None:
                _log.error("cannot refresh: no refresh token stored")
                return None
            refresh_token = decrypt_token(
                token_row.refresh_token_enc,
                self._settings.token_enc_key,
                provider=token_row.provider,
                member_urn=token_row.member_urn,
            )
            try:
                data = self._client.refresh(refresh_token)
            except LinkedInError:
                # A failed refresh (expired/revoked refresh token) is recoverable
                # only by re-authorisation. Never log the token or the body.
                _log.exception("token refresh call failed")
                return None

            new_access = data.get("access_token")
            if not new_access or not isinstance(new_access, str):
                _log.error("token refresh returned no access token")
                return None

            key = self._settings.token_enc_key
            # Re-encrypt the fresh access token; rotate refresh token if returned.
            # Sealed under the SAME canonical (provider, member_urn) AAD so the next
            # load path can open it (the incompatibility that broke live publish).
            token_row.access_token_enc = encrypt_token(
                new_access, key, provider=token_row.provider, member_urn=token_row.member_urn
            )
            new_refresh = data.get("refresh_token")
            if isinstance(new_refresh, str) and new_refresh:
                token_row.refresh_token_enc = encrypt_token(
                    new_refresh, key, provider=token_row.provider, member_urn=token_row.member_urn
                )
            # Best-effort expiry bookkeeping so the token job can pre-refresh later.
            self._apply_expiries(token_row, data)
            session.add(token_row)
            session.commit()
            _log.info("access token refreshed and re-encrypted")
            return new_access

    def _apply_expiries(self, token_row: OAuthToken, data: dict[str, object]) -> None:
        """Set access/refresh expiry timestamps from a token response (if present)."""
        now = self._now()
        access_ttl = data.get("expires_in")
        if isinstance(access_ttl, (int, float)):
            token_row.access_expires_at = now + timedelta(seconds=int(access_ttl))
        refresh_ttl = data.get("refresh_token_expires_in")
        if isinstance(refresh_ttl, (int, float)):
            token_row.refresh_expires_at = now + timedelta(seconds=int(refresh_ttl))

    # -- publish with retry / refresh policy (§15.4) ------------------------

    def _publish_with_policy(
        self,
        session: Session,
        token_row: OAuthToken,
        access_token: str,
        member_urn: str,
        text: str,
        image_bytes: bytes | None,
    ) -> tuple[str, str | None, str]:
        """Publish honouring the §15.4 matrix; return ``(post_urn, image_urn, token)``.

        429/5xx are retried with capped exponential backoff inside
        :meth:`_retrying_post`. A 401 (``NeedsReauth``) is NOT retried blindly:
        the token is refreshed ONCE and the publish retried with the new token; a
        second 401 becomes :class:`ReauthRequired`. The returned token is the one
        actually used (post-refresh), so callers (staging delete) use a live token.
        """
        try:
            post_urn, image_urn = self._retrying_post(access_token, member_urn, text, image_bytes)
            return post_urn, image_urn, access_token
        except NeedsReauth:
            # (401) Refresh exactly once, then retry the publish a single time.
            _log.info("publish hit 401; attempting one token refresh")
            new_access = self._refresh_access_token(session, token_row)
            if new_access is None:
                raise ReauthRequired("token refresh failed") from None
            try:
                post_urn, image_urn = self._retrying_post(
                    new_access, member_urn, text, image_bytes
                )
                return post_urn, image_urn, new_access
            except NeedsReauth as exc:
                # Still unauthorised with a fresh token -> the owner must re-auth.
                raise ReauthRequired("unauthorised after refresh") from exc

    def _retrying_post(
        self, access_token: str, member_urn: str, text: str, image_bytes: bytes | None
    ) -> tuple[str, str | None]:
        """Run one publish attempt with capped exponential backoff on 429 ONLY.

        CRITICAL (ISSUE 6): only :class:`RateLimited` (429) is retried here, because
        a 429 is a KNOWN rejection — the request was throttled and never processed,
        so no post was created and re-issuing the (non-idempotent) create is safe.
        A :class:`TransientLinkedInError` (5xx / mapped timeout) is deliberately NOT
        retried: after such a failure LinkedIn may already have created the post, so
        a blind retry would duplicate it. It propagates immediately to the caller,
        which treats it as an UNKNOWN outcome to RECONCILE rather than re-post.
        401/403 also propagate immediately. ``reraise=True`` surfaces the LAST error
        (not a ``RetryError``) once attempts are exhausted. The injected ``sleep``
        keeps tests instant.
        """
        retryer = Retrying(
            retry=retry_if_exception_type(RateLimited),
            wait=wait_exponential(multiplier=self._backoff_base, max=self._backoff_max),
            stop=stop_after_attempt(self._max_attempts),
            sleep=self._sleep,
            reraise=True,
        )
        return retryer(self._post_once, access_token, member_urn, text, image_bytes)

    def _post_once(
        self, access_token: str, member_urn: str, text: str, image_bytes: bytes | None
    ) -> tuple[str, str | None]:
        """Perform exactly one publish call (image path or text-only path).

        WHY a single unit: this is what the retry wrapper re-invokes on a transient
        failure. Image upload is best-effort (degrades to text-only) so a broken
        image never blocks the post.
        """
        image_urn: str | None = None
        if image_bytes is not None:
            image_urn = self._upload_image_best_effort(access_token, member_urn, image_bytes)
        if image_urn:
            post_urn = self._client.publish_with_image(
                access_token, member_urn, text, image_urn
            )
        else:
            post_urn = self._client.publish_text(access_token, member_urn, text)
        return post_urn, image_urn

    def _upload_image_best_effort(
        self, access_token: str, member_urn: str, image_bytes: bytes
    ) -> str | None:
        """Upload the image, returning its URN or ``None`` to degrade to text-only.

        A 401 is re-raised so the refresh path can handle it; every other image
        failure degrades gracefully (BRD §13.6 — an image never blocks a publish).
        """
        try:
            return self._client.upload_image(
                access_token, image_bytes, owner_urn=member_urn
            )
        except NeedsReauth:
            # Auth failures must flow through the token-refresh path, not degrade.
            raise
        except LinkedInError as exc:
            _log.warning(
                "image upload failed; degrading to text-only post",
                extra={"error": exc.__class__.__name__},
            )
            return None

    # -- state transitions --------------------------------------------------

    def _claim(
        self,
        session: Session,
        draft: Draft,
        access_token: str,
        member_urn: str,
        text: str,
    ) -> bool:
        """Atomically claim ``draft`` for publishing; return whether we won it.

        The claim moves the draft into the in-flight ``queued`` state so only one
        runner proceeds to post it, and — before returning True — persists the
        durable idempotency marker + lease via :meth:`_begin_attempt`, so a crash
        between here and the URN being stored is recoverable (ISSUE 4). The wired
        ``scheduled`` state predates the state_machine enum, so its edge uses a
        local guarded CAS; the enum-modelled states reuse
        :func:`vision.approval.state_machine.transition`.

        A draft ALREADY in ``queued`` is NOT re-published on sight (the old bug,
        ISSUE 5): it is routed through :meth:`_reclaim_queued`, which requires an
        EXPIRED lease AND reconciliation confirming no post exists before any
        re-attempt — otherwise it returns False (leave it alone / already handled).
        """
        state = draft.state
        if state == _CLAIMED_STATE:
            # Already claimed — a crash re-drive or a concurrent runner. Never post
            # on sight; require lease expiry + reconciliation first (ISSUE 4/5/6).
            return self._reclaim_queued(session, draft, access_token, member_urn, text)
        if state == "scheduled":
            if not self._cas(
                session, draft, from_states={"scheduled"}, to_state=_CLAIMED_STATE, action="queued"
            ):
                return False
            self._begin_attempt(session, draft)
            return True
        if state in {"approved", "publish_failed"}:
            try:
                transition(session, draft, DraftState.QUEUED, actor=_ACTOR)
            except TransitionConflict:
                # Lost the race to another runner.
                return False
            self._begin_attempt(session, draft)
            return True
        # Any other state is not a publishable source.
        return False

    def _reclaim_queued(
        self,
        session: Session,
        draft: Draft,
        access_token: str,
        member_urn: str,
        text: str,
    ) -> bool:
        """Decide whether a draft already in ``queued`` may be (re)published.

        The at-most-once heart of the crash-recovery path:

          * A still-VALID lease means another runner is actively publishing this
            draft right now — return False so a second caller never double-posts
            (ISSUE 5).
          * An EXPIRED (or absent) lease means the prior claimer crashed / had an
            unknown outcome. Before ANY re-post we RECONCILE (ISSUE 4/6): ask
            LinkedIn whether a post already exists for this exact approved text.
              - If one exists → ADOPT it (persist its URN, finalise, confirm) and
                return False — never create a duplicate.
              - If reconciliation itself fails → fail closed: leave the draft queued
                and return False (never re-post on ambiguity).
              - If it confirms NO post exists → refresh the lease (a new attempt)
                and return True so publishing safely proceeds.
        """
        now = self._now()
        meta = self._publish_meta(draft)
        lease_expires = _parse_iso(meta.get("lease_expires_at"))

        # (a) Lease still held — another live runner owns it. Do not touch.
        if lease_expires is not None and now < lease_expires:
            _log.info(
                "queued draft lease still held; skipping (avoids double-post)",
                extra={"draft_id": str(draft.id)},
            )
            return False

        # (b) Lease expired / missing — reconcile BEFORE any re-post.
        try:
            existing_urn = self._reconcile(access_token, member_urn, text)
        except LinkedInError as exc:
            # Cannot prove absence of a post → fail closed, leave queued for a later
            # run (the reaper alerts if it stays stuck). NEVER re-post on ambiguity.
            _log.warning(
                "reconciliation lookup failed; leaving draft queued for retry",
                extra={"draft_id": str(draft.id), "reason": exc.__class__.__name__},
            )
            return False

        if existing_urn is not None:
            # A post already exists for this draft — adopt it, never duplicate.
            _log.info(
                "reconciled stranded draft to an already-published post",
                extra={"draft_id": str(draft.id), "post_urn": existing_urn},
            )
            self._finalise_published(session, draft, existing_urn, None)
            self._send_confirmation(draft)
            return False

        # (c) Confirmed no post exists — safe to re-claim. Refresh the lease (a new
        # attempt under this runner) and let the caller proceed to publish.
        self._begin_attempt(session, draft)
        return True

    def _reconcile(self, access_token: str, member_urn: str, text: str) -> str | None:
        """Return the URN of an already-created post for ``text``, or ``None``.

        Thin seam over :meth:`LinkedInClient.find_existing_post` so the reconcile
        strategy lives behind one name. Any :class:`LinkedInError` propagates to the
        caller, which fails closed (declines to re-post) on a lookup failure.
        """
        return self._client.find_existing_post(access_token, member_urn, text)

    def reap_stuck(self, session: Session, now: datetime) -> int:
        """Alert on drafts stuck in ``queued`` past the stuck TTL; return the count.

        The safety net for ISSUE 4: a draft a publish left claimed but never
        finalised (a crash between create and persist, or a persistently unknown
        outcome that reconciliation can't yet resolve) would otherwise sit
        invisibly in ``queued``. Run alongside the poll, this reaper finds every
        such draft whose lease expired more than ``stuck_after`` ago and alerts the
        owner, guaranteeing an approved post is never SILENTLY dropped. It only
        alerts (never posts), so it can never itself cause a duplicate.
        """
        stuck = (
            session.query(Draft)
            .filter(Draft.state == _CLAIMED_STATE, Draft.post_urn.is_(None))
            .all()
        )
        alerted = 0
        for draft in stuck:
            meta = self._publish_meta(draft)
            lease_expires = _parse_iso(meta.get("lease_expires_at"))
            # Only "stuck" once the lease has expired AND the grace has elapsed.
            if lease_expires is None or now < lease_expires + self._stuck_after:
                continue
            _log.error(
                "draft stuck in queued past lease TTL; alerting owner",
                extra={"draft_id": str(draft.id)},
            )
            self._alert_stuck(draft)
            alerted += 1
        return alerted

    # -- durable idempotency marker + lease (in model_trace['publish']) -----

    def _publish_meta(self, draft: Draft) -> dict[str, object]:
        """Return the draft's publish marker dict (idempotency key + lease), or {}.

        Reads from the ``model_trace`` JSON column defensively — a missing column,
        a non-dict trace, or a missing ``publish`` key all yield an empty dict so
        callers never have to special-case a fresh draft.
        """
        trace = draft.model_trace
        publish = trace.get(_PUBLISH_META_KEY) if isinstance(trace, dict) else None
        return dict(publish) if isinstance(publish, dict) else {}

    def _write_publish_meta(
        self, session: Session, draft: Draft, publish_meta: dict[str, object]
    ) -> None:
        """Persist ``publish_meta`` under ``model_trace['publish']`` (immutably).

        A NEW ``model_trace`` dict is assigned (never mutated in place) so both the
        immutability principle (§22) and SQLAlchemy's change detection on the JSON
        column are honoured, then committed so the marker is durable BEFORE the
        network call that depends on it.
        """
        trace = dict(draft.model_trace) if isinstance(draft.model_trace, dict) else {}
        draft.model_trace = {**trace, _PUBLISH_META_KEY: publish_meta}
        session.add(draft)
        session.commit()

    def _clear_publish_meta(self, session: Session, draft: Draft) -> None:
        """Drop the publish marker/lease (e.g. when re-queuing a deferred draft)."""
        trace = draft.model_trace
        if not isinstance(trace, dict) or _PUBLISH_META_KEY not in trace:
            return
        draft.model_trace = {k: v for k, v in trace.items() if k != _PUBLISH_META_KEY}
        session.add(draft)
        session.commit()

    def _begin_attempt(self, session: Session, draft: Draft) -> None:
        """Persist the durable idempotency marker + a fresh lease BEFORE any create.

        WHY before the create (ISSUE 4): if the process crashes between the create
        call and the URN being persisted, the marker + lease are already durable, so
        the draft is reconcilable on re-drive instead of being silently lost or
        blindly re-posted. The idempotency key is generated ONCE and preserved
        across re-leases so it stably identifies this draft's publish intent.
        """
        now = self._now()
        meta = self._publish_meta(draft)
        key = meta.get("idempotency_key")
        if not isinstance(key, str) or not key:
            key = uuid.uuid4().hex
        self._write_publish_meta(
            session,
            draft,
            {
                "idempotency_key": key,
                "attempted_at": now.isoformat(),
                "lease_owner": self._owner_id,
                "lease_expires_at": (now + self._lease_ttl).isoformat(),
            },
        )

    def _revert_claim(self, session: Session, draft: Draft) -> None:
        """Return a claimed draft to its approved slot (keeps it publishable).

        Used when a publish is deferred (persistent 401) or was a throwaway staging
        post: the draft goes back to ``scheduled`` so the next poll re-attempts it,
        and its now-stale lease/idempotency marker is cleared so the re-queued draft
        starts a clean attempt. The state_machine has no ``queued -> scheduled``
        edge, so the state change is a local guarded CAS.
        """
        self._cas(
            session,
            draft,
            from_states={_CLAIMED_STATE},
            to_state="scheduled",
            action="publish_deferred",
        )
        self._clear_publish_meta(session, draft)

    def _finalise_published(
        self, session: Session, draft: Draft, post_urn: str, image_urn: str | None
    ) -> None:
        """Record the URN + advance ``queued -> published`` in one transaction.

        The URN/URL/image-URN are set on the ORM object and the state is advanced
        via the shared state machine; both flush in the SAME commit, so a live post
        and its recorded state are atomic (no window where a post exists but is
        unrecorded).
        """
        draft.post_urn = post_urn
        draft.post_url = _post_url_from_urn(post_urn)
        if image_urn:
            draft.image_urn = image_urn
        # The state machine performs the guarded CAS + append-only audit + commit.
        transition(session, draft, DraftState.PUBLISHED, actor=_ACTOR, meta={"post_urn": post_urn})

    def _fail(self, session: Session, draft: Draft, *, terminal: bool) -> None:
        """Move a claimed draft into the failure lane (retryable or dead-letter).

        A failed publish first becomes ``publish_failed`` (the retry lane); when
        the failure is terminal (retries exhausted / hard error we choose to give
        up on) it is further advanced to the terminal ``dead_letter`` so it is not
        re-attempted (BRD §15.4). Both edges are modelled by the state machine.
        """
        transition(session, draft, DraftState.PUBLISH_FAILED, actor=_ACTOR)
        if terminal:
            transition(session, draft, DraftState.DEAD_LETTER, actor=_ACTOR)

    def _cas(
        self,
        session: Session,
        draft: Draft,
        *,
        from_states: set[str],
        to_state: str,
        action: str,
    ) -> bool:
        """Guarded compare-and-set of ``draft.state`` + append-only audit row.

        Mirrors :mod:`vision.approval.service`: the UPDATE only matches while the
        row is still in an expected source state (row-count MUST be 1), so a
        concurrent change loses. On a win it writes one audit row and commits both
        atomically; on a miss it rolls back and returns ``False``.
        """
        result = session.execute(
            update(Draft)
            .where(Draft.id == draft.id, Draft.state.in_(tuple(from_states)))
            .values(state=to_state),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount != 1:
            # Someone else moved the row first — nothing applied.
            session.rollback()
            return False
        session.add(
            AuditLog(
                entity="draft",
                entity_id=str(draft.id),
                action=action,
                actor=_ACTOR,
                meta={"to_state": to_state},
                at=self._now(),
            )
        )
        session.commit()
        # Re-sync the ORM object with the committed state (the CAS used core SQL).
        session.refresh(draft)
        return True

    # -- notifications ------------------------------------------------------

    def _mailer_or_build(self) -> EmailSender:
        """Return the injected mailer, or lazily build one from settings.

        Lazy construction means a publisher can be created without email config;
        the sender is only materialised when a send actually occurs.
        """
        if self._mailer is None:
            self._mailer = get_sender(self._settings)
        return self._mailer

    def _send_confirmation(self, draft: Draft) -> None:
        """Email the owner that their post is live (§10.2). Never raises.

        A confirmation failure must not undo a successful publish, so we rely on
        the sender's bool contract (it never raises for an ordinary failure) and
        merely log a non-delivery — the post is already live regardless.
        """
        url = draft.post_url or ""
        subject = "VISION — your LinkedIn post is live"
        text = f"Your approved post has been published.\n\n{url}\n"
        html = (
            "<p>Your approved post has been published.</p>"
            f'<p><a href="{url}">View it on LinkedIn</a></p>'
        )
        ok = self._mailer_or_build().send(subject, text, html)
        if not ok:
            _log.warning(
                "confirmation email not delivered (post is live regardless)",
                extra={"draft_id": str(draft.id)},
            )

    def _alert_failure(self, draft: Draft, exc: LinkedInError, *, terminal: bool) -> None:
        """Alert the owner/ops that a publish failed (§15.4). Never raises.

        The alert carries only the error CLASS and status code — never a token or
        response body — so nothing sensitive can leak into an email.
        """
        state = "dead-lettered" if terminal else "failed (will need attention)"
        subject = f"VISION — publish {state}"
        detail = f"{exc.__class__.__name__} (HTTP {getattr(exc, 'status_code', 'n/a')})"
        text = (
            f"Publishing draft {draft.id} {state}.\nReason: {detail}\n"
            "No sensitive data is included in this alert."
        )
        self._mailer_or_build().send(subject, text, f"<p>{text}</p>")

    def _alert_unknown_outcome(self, draft: Draft, exc: LinkedInError) -> None:
        """Alert that a publish had an UNKNOWN outcome (5xx/timeout after send).

        The draft is deliberately KEPT in ``queued`` with its idempotency marker so
        the next poll reconciles it; this alert makes the ambiguous state visible
        instead of silently stranding an approved post (ISSUE 4). Carries only the
        error CLASS + status code — never a token or response body. Never raises.
        """
        subject = "VISION — publish outcome unknown (pending reconciliation)"
        detail = f"{exc.__class__.__name__} (HTTP {getattr(exc, 'status_code', 'n/a')})"
        text = (
            f"Publishing draft {draft.id} returned an unknown outcome: {detail}.\n"
            "The post may or may not have been created; VISION will reconcile on a "
            "later run and will NOT re-post blindly. No sensitive data is included."
        )
        self._mailer_or_build().send(subject, text, f"<p>{text}</p>")

    def _alert_stuck(self, draft: Draft) -> None:
        """Alert that a draft has been stuck in ``queued`` beyond its lease TTL.

        The reaper's notification: an approved draft that never reached
        ``published`` (a crash after claim, or a persistently unknown outcome) is
        surfaced to the owner so it is never silently lost (ISSUE 4). No sensitive
        data is included. Never raises.
        """
        subject = "VISION — approved post stuck awaiting publish"
        text = (
            f"Draft {draft.id} has been queued for publishing far longer than "
            "expected and has not gone live. It needs attention so the approved "
            "post is not lost. No sensitive data is included."
        )
        self._mailer_or_build().send(subject, text, f"<p>{text}</p>")

    def _alert_reauth(self, draft: Draft, *, reason: str) -> None:
        """Alert the owner that LinkedIn re-authorisation is required. Never raises.

        The approved draft is kept intact; it publishes automatically once the
        owner re-authorises (BRD §15.4 401).
        """
        subject = "VISION — LinkedIn re-authorisation required"
        text = (
            f"Publishing draft {draft.id} is blocked: {reason}.\n"
            "Please re-authorise LinkedIn; the approved post will publish "
            "automatically once access is restored."
        )
        self._mailer_or_build().send(subject, text, f"<p>{text}</p>")


def main() -> int:
    """``vision-publisher`` console entry point (cron, every ~5 min, §10.2).

    Builds the publisher from settings, opens one DB session, and publishes every
    approved draft due now. Returns a process exit code (0 = clean run) so the
    cron wrapper can alert on a non-zero exit.
    """
    # Imported here (not at module top) to avoid building the DB engine at import
    # time — keeps the module cheap to import for tests that never touch a DB.
    from vision.db.session import get_session
    from vision.logging_setup import configure_logging

    configure_logging()
    settings = get_settings()
    publisher = LinkedInPublisher(settings)
    _log.info("vision-publisher starting", extra={"env": settings.vision_env.value})
    stuck = 0
    try:
        with get_session() as session:
            now = datetime.now(timezone.utc)
            published = publisher.poll_and_publish(session, now)
            # Safety net: alert on any draft stranded in ``queued`` past its lease
            # TTL so an approved post is never silently lost (ISSUE 4 reaper/alert).
            stuck = publisher.reap_stuck(session, now)
    finally:
        # Always release the HTTP connection pool, even on error.
        publisher.close()
    _log.info(
        "vision-publisher finished", extra={"published": published, "stuck_alerts": stuck}
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
