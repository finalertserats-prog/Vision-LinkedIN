"""Approval domain logic: state machine, atomic consume, publisher port (§14.3).

WHY this module exists: ``web.py`` owns HTTP + security; this module owns the
*business* of an approval action — moving a draft through its state machine while
consuming the single-use token in the SAME database transaction, and calling the
(Phase-2 mocked) publisher exactly once. Isolating it here keeps the endpoints
thin and makes the state logic unit-testable without a web layer.

Security invariants realised here (threat model §1/§2 + hardening checklist):
  * **Compare-and-set transition** — the draft state is advanced with a single
    ``UPDATE ... WHERE id=? AND state IN (allowed)`` whose row-count MUST be 1.
    Two concurrent approvals therefore cannot both win: the loser matches zero
    rows and is rejected. This is the "transactional compare-and-set" the threat
    model demands against double-approval/publish.
  * **Atomic single-use** — in the very same transaction we INSERT the token's
    single-use key into ``used_tokens`` (a UNIQUE column). A replay hits the
    unique constraint and the whole unit of work rolls back. State-change and
    nonce-consumption commit together or not at all.
  * **Commit-then-publish (transactional outbox, BRD §10.2 / §22.9 fail-closed)**
    — the CAS transition, the nonce consumption, and the audit row are COMMITTED
    atomically FIRST; ONLY after that durable commit is the publisher invoked
    once, keyed by the draft id (``idempotency_key``). WHY this order: if publish
    ran first (or before commit) and then the commit failed — or publish
    succeeded and the commit then rolled back — the nonce would be un-consumed and
    the signed link would go live again, letting the owner re-approve and DOUBLE
    publish. By committing first we spend the nonce the instant the approval
    becomes durable, so the link can never be reused; a publish failure is
    survivable because the committed ``scheduled``/``published`` row is the source
    of truth and Phase 3's publisher worker re-drives any un-published draft
    idempotently (skipping those whose ``post_urn`` is already set). Publish is
    therefore at-most-once here and exactly-once across the worker's retries.
  * **Editing invalidates on failure only by non-consumption** — a hand-edit is
    re-validated BEFORE any consume; invalid edits raise without touching state
    or the token, so the owner can retry with the same link.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vision.approval import tokens
from vision.approval.edit_page import validate_edited_post
from vision.approval.tokens import VerifiedToken
from vision.config import Settings
from vision.db.models import AuditLog, Draft, UsedToken

_log = logging.getLogger(__name__)


# --- State machine (BRD §10.4) ---------------------------------------------
# The draft states relevant to the approval loop. Kept as plain string
# constants matching ``drafts.state`` (default "new") so the values stay
# human-readable in the DB and portable across SQLite/Postgres.
STATE_NEW = "new"  # freshly generated, awaiting the owner's decision
STATE_SCHEDULED = "scheduled"  # approved; queued for the next publish slot
STATE_PUBLISHED = "published"  # published now (post-now) — terminal
STATE_REJECTED = "rejected"  # discarded by the owner — terminal

# The set of states from which each action may legally fire. Expressed as a
# table (not scattered ``if``s) so the allowed transitions are auditable in one
# place — the compare-and-set uses exactly these as its WHERE guard. A draft that
# is already scheduled/published/rejected is NOT re-actionable by these links
# (defence against double-approval and against a replayed link that somehow
# passed the nonce check).
_APPROVE_FROM: frozenset[str] = frozenset({STATE_NEW})
_POST_NOW_FROM: frozenset[str] = frozenset({STATE_NEW, STATE_SCHEDULED})
_REJECT_FROM: frozenset[str] = frozenset({STATE_NEW, STATE_SCHEDULED})
_EDIT_FROM: frozenset[str] = frozenset({STATE_NEW})


# --- Publisher port (Phase 2: MOCKED) --------------------------------------
@runtime_checkable
class PublisherPort(Protocol):
    """The one seam between an approval and the act of publishing (§14.3).

    Phase 2 wires a :class:`NoopPublisher` that merely records the call; Phase 3
    swaps in a real LinkedIn-backed implementation with the SAME signature, so no
    approval logic changes. ``scheduled_for=None`` means "publish immediately"
    (the post-now path); a datetime means "enqueue for that UTC instant" (the
    approve path). Implementations return an opaque reference string (a job id or
    post URN) for the audit trail.

    ``idempotency_key`` (the draft id) is REQUIRED by the contract: the approval is
    committed durably before this call, so the same draft may be handed to
    ``publish`` more than once (an inline retry here plus the Phase-3 poller). An
    implementation MUST treat the key as a dedupe token — a repeat for a draft
    whose ``post_urn`` is already set is a no-op — so the post lands exactly once.
    """

    def publish(
        self,
        *,
        draft_id: str,
        text: str,
        image_path: str | None,
        scheduled_for: datetime | None,
        idempotency_key: str,
    ) -> str:
        """Publish now (``scheduled_for is None``) or enqueue for ``scheduled_for``.

        ``idempotency_key`` (the draft id) MUST dedupe duplicate deliveries.
        """
        ...


@dataclass(frozen=True)
class PublishCall:
    """An immutable record of one publisher invocation (for tests + audit).

    ``idempotency_key`` is the draft id the caller passes so the (Phase-3) worker
    can dedupe retries; it defaults to ``""`` only so historical constructions
    stay valid — real calls always carry the draft id.
    """

    draft_id: str
    text: str
    image_path: str | None
    scheduled_for: datetime | None
    idempotency_key: str = ""


@dataclass
class NoopPublisher:
    """Phase-2 mock publisher: records calls, performs NO network I/O.

    WHY a class with a list (not a bare function): tests assert the publisher was
    called EXACTLY once with the right arguments, so we need to capture calls.
    The real LinkedIn publisher lands in Phase 3 behind the identical port.
    """

    calls: list[PublishCall] = field(default_factory=list)

    def publish(
        self,
        *,
        draft_id: str,
        text: str,
        image_path: str | None,
        scheduled_for: datetime | None,
        idempotency_key: str,
    ) -> str:
        """Record the call and return a deterministic fake reference."""
        self.calls.append(
            PublishCall(
                draft_id=draft_id,
                text=text,
                image_path=image_path,
                scheduled_for=scheduled_for,
                idempotency_key=idempotency_key,
            )
        )
        # A stable, non-secret reference so the audit log has something to key on.
        return f"noop:{draft_id}"


# --- Typed service errors ---------------------------------------------------
class ServiceError(Exception):
    """Base for approval-service failures the endpoints render generically."""


class DraftNotFound(ServiceError):
    """The token verified but its draft id does not resolve to a row.

    Fail-closed: a valid signature over a non-existent draft is still a dead end.
    """


class StateConflict(ServiceError):
    """The compare-and-set matched zero rows — the draft is not in an actionable
    state (already approved/published/rejected, or a concurrent action won)."""


class ReplayDetected(ServiceError):
    """The single-use key was already present in ``used_tokens`` (unique-violation
    on insert) — a replay that raced past the pre-check is caught here."""


class ValidationFailed(ServiceError):
    """A hand-edited post failed length/format/compliance re-validation (§14.3).

    Carries the list of human-readable problems so the edit page can re-render
    them. The token is deliberately NOT consumed for this failure.
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__("edited post failed validation")
        self.problems = problems


@dataclass(frozen=True)
class ActionResult:
    """The immutable outcome of a completed POST action, for the result page."""

    action: str
    draft_id: str
    new_state: str
    heading: str
    message: str
    publisher_ref: str | None = None


# --- Scheduling -------------------------------------------------------------
# On a host without the IANA tz database (e.g. bare Windows) ``ZoneInfo`` raises;
# we fall back to a tiny fixed-offset map so scheduling still resolves. Prod runs
# on Linux with full tz data, so ``ZoneInfo`` is used there.
_FALLBACK_OFFSETS: dict[str, timedelta] = {
    "Asia/Kolkata": timedelta(hours=5, minutes=30),
    "UTC": timedelta(0),
}


def _resolve_tz(name: str) -> timezone:
    """Resolve a tz name to a ``tzinfo``, degrading to a fixed offset if needed.

    Returns a ``datetime.timezone`` (fixed offset) on fallback rather than
    raising, so a missing tz database never blocks an approval. This is a
    deliberate fail-*open* on TIMEZONE only (never on security) — the worst case
    is a slot computed at a fixed offset instead of a DST-aware one.
    """
    try:
        from zoneinfo import ZoneInfo

        # ZoneInfo is a valid tzinfo; the annotation says timezone for simplicity
        # but any tzinfo is acceptable to astimezone/replace below.
        return ZoneInfo(name)  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — any resolution failure means "fall back"
        offset = _FALLBACK_OFFSETS.get(name, timedelta(0))
        _log.warning(
            "tz database unavailable for %s; using fixed offset fallback", name
        )
        return timezone(offset)


def next_publish_slot(now_utc: datetime, settings: Settings) -> datetime:
    """Compute the next ``PUBLISH_SLOT_LOCAL`` instant at/after ``now_utc`` (UTC).

    Parses ``HH:MM`` from settings in the configured ``TZ`` and returns the next
    future occurrence as a timezone-aware UTC datetime. ``now_utc`` is injected
    (not read from the clock) so the calculation is pure and testable.
    """
    tz = _resolve_tz(settings.tz)
    local_now = now_utc.astimezone(tz)
    hour_str, _, minute_str = settings.publish_slot_local.partition(":")
    slot = local_now.replace(
        hour=int(hour_str), minute=int(minute_str), second=0, microsecond=0
    )
    # If today's slot has already passed, roll to tomorrow's slot.
    if slot <= local_now:
        slot = slot + timedelta(days=1)
    return slot.astimezone(timezone.utc)


# --- Internal helpers -------------------------------------------------------
def _single_use_key(verified: VerifiedToken) -> str:
    """Return the token's single-use key exactly as the ISSUER derived it.

    WHY reuse ``tokens._single_use_key``: the verifier hands this same key to the
    ``is_used_callback``, and ``used_tokens`` must key on the identical value for
    the pre-check and the atomic consume to agree. Recomputing it here (rather
    than duplicating the ``sha256(draft_id|nonce)`` recipe) guarantees we never
    drift from the canonical derivation.
    """
    return tokens._single_use_key(verified.draft_id, verified.nonce)


def token_is_used(session: Session, single_use_key: str) -> bool:
    """Return True iff ``single_use_key`` is already recorded in ``used_tokens``.

    This is the read-only single-use check the verifier calls on BOTH GET and
    POST. It never mutates — the actual consumption happens in
    :func:`_consume_and_transition` under the write transaction.
    """
    row = (
        session.query(UsedToken.id)
        .filter(UsedToken.nonce == single_use_key)
        .first()
    )
    return row is not None


def make_is_used(session: Session):
    """Build the ``is_used_callback`` the token verifier expects, bound to a session."""

    def _is_used(single_use_key: str) -> bool:
        return token_is_used(session, single_use_key)

    return _is_used


def load_draft(session: Session, draft_id: str) -> Draft:
    """Load a draft by its string id, or raise :class:`DraftNotFound`.

    The token's ``draft_id`` is a UUID string; a value that does not parse as a
    UUID is treated as "not found" (fail-closed) rather than surfacing a parse
    error.
    """
    try:
        pk = uuid.UUID(draft_id)
    except (ValueError, AttributeError) as exc:
        raise DraftNotFound(f"draft id {draft_id!r} is not a valid UUID") from exc
    draft = session.get(Draft, pk)
    if draft is None:
        raise DraftNotFound(f"no draft for id {draft_id!r}")
    return draft


def _audit(
    session: Session,
    *,
    draft_id: str,
    action: str,
    single_use_key: str,
    actor_ip: str | None,
    meta: dict[str, object] | None = None,
) -> None:
    """Append an audit row for a completed action (§11.7 / threat-model §1).

    Records only a PREFIX of the single-use key hash (never the raw token, never
    the full key) so the append-only trail is correlatable for repudiation
    defence without becoming a credential store.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {"token_key_prefix": single_use_key[:12]}
    if meta:
        payload.update(meta)
    session.add(
        AuditLog(
            entity="draft",
            entity_id=draft_id,
            action=action,
            actor="owner",
            ip=actor_ip,
            meta=payload,
            at=now,
        )
    )


def _consume_and_transition(
    session: Session,
    *,
    draft: Draft,
    verified: VerifiedToken,
    allowed_from: frozenset[str],
    new_state: str,
    extra_values: dict[str, object] | None = None,
) -> None:
    """Atomically advance the draft state AND consume the single-use token.

    Both happen in the caller's transaction (committed together on success):

      1. Compare-and-set: ``UPDATE drafts SET state=<new> WHERE id=? AND state IN
         (<allowed_from>)``. A row-count of exactly 1 proves the draft was in a
         legal source state and no concurrent action beat us; anything else
         raises :class:`StateConflict`.
      2. Consume: INSERT the single-use key into ``used_tokens``. The UNIQUE
         constraint means a replay that raced past the read-side check fails here
         with an ``IntegrityError`` → :class:`ReplayDetected`, rolling back (1).

    Doing (1) before (2) means an out-of-state link never even records a spent
    nonce; doing both under one transaction means they are all-or-nothing.
    """
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {"state": new_state, "updated_at": now}
    if extra_values:
        values.update(extra_values)

    # (1) Compare-and-set on the state — the atomic guard against double action.
    result = session.execute(
        update(Draft)
        .where(Draft.id == draft.id, Draft.state.in_(allowed_from))
        .values(**values)
    )
    if result.rowcount != 1:
        # Zero rows ⇒ the draft was not in an actionable state (already
        # approved/published/rejected, or a concurrent action won the race).
        raise StateConflict(
            f"draft {draft.id} not in an actionable state for {verified.action!r}"
        )

    # (2) Consume the single-use key in the SAME transaction. Storing the derived
    # single-use key (never the raw token) matches what the verifier checks.
    single_use_key = _single_use_key(verified)
    session.add(
        UsedToken(
            nonce=single_use_key,
            draft_id=draft.id,
            action=verified.action,
            used_at=now,
        )
    )
    try:
        # Flush now so a duplicate-key replay surfaces HERE (inside our control)
        # rather than at commit time in the caller.
        session.flush()
    except IntegrityError as exc:
        # A concurrent/replayed consumer already inserted this key.
        raise ReplayDetected("single-use token already consumed") from exc


def _publish_after_commit(
    publisher: PublisherPort,
    *,
    draft_id: str,
    text: str,
    image_path: str | None,
    scheduled_for: datetime | None,
) -> str | None:
    """Invoke the publisher ONCE, AFTER the approval has durably committed.

    Best-effort by contract (never re-raises) — this is the second half of the
    transactional outbox (BRD §10.2). WHY it must not raise: the caller has
    ALREADY committed the state transition + nonce consumption + audit row, so the
    approval is durable and the nonce is spent. Propagating a publish error would
    make the outer session context attempt a rollback and surface a failure page
    for an approval that actually SUCCEEDED — and, worse, could tempt a design that
    reverts state (re-opening the signed link → double publish). Instead we log and
    return ``None``; the committed ``scheduled``/``published`` row is the source of
    truth and Phase 3's publisher worker re-drives every un-published draft
    idempotently (``idempotency_key`` = draft id, and drafts whose ``post_urn`` is
    set are skipped). Net guarantee: at-most-once here, exactly-once overall, with
    NO replay window on the signed link.
    """
    try:
        return publisher.publish(
            draft_id=draft_id,
            text=text,
            image_path=image_path,
            scheduled_for=scheduled_for,
            idempotency_key=draft_id,  # the draft id IS the dedupe key (§10.2)
        )
    except Exception:  # noqa: BLE001 — a durable approval must survive ANY publish error
        _log.exception(
            "post-commit publish failed; approval is durable, worker will retry",
            extra={"draft_id": draft_id},
        )
        return None


# --- Public actions ---------------------------------------------------------
def approve(
    session: Session,
    *,
    verified: VerifiedToken,
    settings: Settings,
    publisher: PublisherPort,
    actor_ip: str | None,
) -> ActionResult:
    """Approve a draft: schedule it for the next publish slot (§14.3).

    Transitions ``new → scheduled`` (compare-and-set), consumes the token, sets
    ``scheduled_for`` to the next slot, and — AFTER committing all of that durably
    — calls the publisher once to enqueue it.

    Ordering is the security guarantee (BRD §10.2 / §22.9): the transition, the
    nonce consumption, and the audit row COMMIT together FIRST; only then does the
    publish fire. So a publish failure can neither un-consume the nonce nor lose
    the approval (no replay window on the link), and a committed approval is
    durable. Publish is at-most-once here; the Phase-3 worker makes it
    exactly-once via ``idempotency_key`` + the ``post_urn``-already-set no-op.
    """
    draft = load_draft(session, verified.draft_id)
    slot = next_publish_slot(datetime.now(timezone.utc), settings)
    _consume_and_transition(
        session,
        draft=draft,
        verified=verified,
        allowed_from=_APPROVE_FROM,
        new_state=STATE_SCHEDULED,
        extra_values={"scheduled_for": slot},
    )
    draft_id = str(draft.id)
    post_text = draft.post_text or ""
    image_path = draft.image_path
    _audit(
        session,
        draft_id=draft_id,
        action="approved",
        single_use_key=_single_use_key(verified),
        actor_ip=actor_ip,
        meta={"scheduled_for": slot.isoformat()},
    )
    # Commit the approval + nonce consumption + audit ATOMICALLY and DURABLY
    # BEFORE any publish attempt. The signed link's nonce is spent the instant
    # this returns, so it can never be replayed even if publishing later fails.
    session.commit()
    # Now (and only now) enqueue via the port. Best-effort: a failure here leaves
    # the durable approval intact and the Phase-3 worker re-publishes idempotently.
    ref = _publish_after_commit(
        publisher,
        draft_id=draft_id,
        text=post_text,
        image_path=image_path,
        scheduled_for=slot,
    )
    _log.info("draft approved", extra={"draft_id": draft_id})
    return ActionResult(
        action="approve",
        draft_id=draft_id,
        new_state=STATE_SCHEDULED,
        heading="Post approved",
        message=f"It is scheduled for {slot.isoformat(timespec='minutes')} UTC.",
        publisher_ref=ref,
    )


def post_now(
    session: Session,
    *,
    verified: VerifiedToken,
    settings: Settings,
    publisher: PublisherPort,
    actor_ip: str | None,
) -> ActionResult:
    """Publish a draft immediately (the "post now" link, §14.3).

    Transitions to ``published`` (compare-and-set), consumes the token, COMMITS
    that durably, and only then calls the publisher once with ``scheduled_for=None``
    (publish now). Same ordering guarantee as :func:`approve`: the nonce is spent on
    commit, so a publish failure cannot re-open the link; the Phase-3 worker
    re-publishes any draft with an unset ``post_urn`` idempotently.
    """
    draft = load_draft(session, verified.draft_id)
    _consume_and_transition(
        session,
        draft=draft,
        verified=verified,
        allowed_from=_POST_NOW_FROM,
        new_state=STATE_PUBLISHED,
    )
    draft_id = str(draft.id)
    post_text = draft.post_text or ""
    image_path = draft.image_path
    _audit(
        session,
        draft_id=draft_id,
        action="posted",
        single_use_key=_single_use_key(verified),
        actor_ip=actor_ip,
        meta={},
    )
    # Durable commit BEFORE publishing (transactional outbox, §10.2 / §22.9).
    session.commit()
    ref = _publish_after_commit(
        publisher,
        draft_id=draft_id,
        text=post_text,
        image_path=image_path,
        scheduled_for=None,  # None ⇒ publish immediately
    )
    _log.info("draft posted now", extra={"draft_id": draft_id})
    return ActionResult(
        action="post_now",
        draft_id=draft_id,
        new_state=STATE_PUBLISHED,
        heading="Post published",
        message="Your post has been sent to LinkedIn.",
        publisher_ref=ref,
    )


def reject(
    session: Session,
    *,
    verified: VerifiedToken,
    settings: Settings,
    actor_ip: str | None,
    regenerate: bool = False,
) -> ActionResult:
    """Reject (discard) a draft (§14.3).

    Transitions to ``rejected`` (compare-and-set) and consumes the token. The
    publisher is NEVER called on a reject. ``regenerate`` records the owner's wish
    for a single fresh attempt (acted on by the daily job, out of scope here).
    """
    draft = load_draft(session, verified.draft_id)
    _consume_and_transition(
        session,
        draft=draft,
        verified=verified,
        allowed_from=_REJECT_FROM,
        new_state=STATE_REJECTED,
    )
    _audit(
        session,
        draft_id=str(draft.id),
        action="rejected",
        single_use_key=_single_use_key(verified),
        actor_ip=actor_ip,
        meta={"regenerate": regenerate},
    )
    _log.info(
        "draft rejected", extra={"draft_id": str(draft.id), "regenerate": regenerate}
    )
    message = (
        "Discarded. A fresh version will be attempted."
        if regenerate
        else "Discarded. No post was published."
    )
    return ActionResult(
        action="reject",
        draft_id=str(draft.id),
        new_state=STATE_REJECTED,
        heading="Post rejected",
        message=message,
    )


def edit_apply(
    session: Session,
    *,
    verified: VerifiedToken,
    settings: Settings,
    publisher: PublisherPort,
    new_post_text: str,
    new_hashtags: list[str],
    actor_ip: str | None,
) -> ActionResult:
    """Apply a hand-edit then approve the edited version (§14.3).

    Order matters for fail-closed semantics:

      1. Re-validate the edited text/hashtags (length/format/compliance, NOT an
         LLM pass). On failure raise :class:`ValidationFailed` — WITHOUT touching
         state or the token, so the same edit link can be retried.
      2. Only if valid: replace ``post_text``/``hashtags``, transition
         ``new → scheduled`` (compare-and-set), consume the token, COMMIT that
         durably, and only then call the publisher once to enqueue the edited post.

    The commit-then-publish ordering matches :func:`approve` (BRD §10.2 / §22.9):
    a publish failure can never un-consume the nonce or lose the edited approval,
    and the exact edited revision is what the worker later publishes idempotently.
    """
    # (1) Re-validate FIRST — never consume the token for an invalid edit.
    problems = validate_edited_post(new_post_text, new_hashtags, settings)
    if problems:
        raise ValidationFailed(problems)

    draft = load_draft(session, verified.draft_id)
    slot = next_publish_slot(datetime.now(timezone.utc), settings)
    # (2) Persist the edited content AS PART OF the same atomic transition so the
    # approved, scheduled draft is exactly the edited revision (threat model:
    # "publish only the exact approved revision").
    _consume_and_transition(
        session,
        draft=draft,
        verified=verified,
        allowed_from=_EDIT_FROM,
        new_state=STATE_SCHEDULED,
        extra_values={
            "post_text": new_post_text,
            "hashtags": list(new_hashtags),
            "scheduled_for": slot,
        },
    )
    draft_id = str(draft.id)
    image_path = draft.image_path
    _audit(
        session,
        draft_id=draft_id,
        action="edited_approved",
        single_use_key=_single_use_key(verified),
        actor_ip=actor_ip,
        meta={"scheduled_for": slot.isoformat()},
    )
    # Durable commit BEFORE publishing (transactional outbox, §10.2 / §22.9).
    session.commit()
    ref = _publish_after_commit(
        publisher,
        draft_id=draft_id,
        text=new_post_text,
        image_path=image_path,
        scheduled_for=slot,
    )
    _log.info("draft edited and approved", extra={"draft_id": draft_id})
    return ActionResult(
        action="edit",
        draft_id=draft_id,
        new_state=STATE_SCHEDULED,
        heading="Edited post approved",
        message=f"Your edited post is scheduled for "
        f"{slot.isoformat(timespec='minutes')} UTC.",
        publisher_ref=ref,
    )
