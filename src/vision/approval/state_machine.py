"""Draft lifecycle state machine (BRD §10.4, threat model §2).

WHY this module exists: a draft is the one object in VISION whose state, when it
reaches ``published``, mutates the owner's real LinkedIn profile. Every state
change therefore has to be (a) an explicitly *allowed* edge, (b) *authorised*
where it matters (approval needs a valid token), (c) *auditable* (an append-only
row per transition), and (d) *atomic* (state + nonce consumption + audit all
commit together, or none do). Centralising that here — rather than scattering
``draft.state = ...`` assignments across endpoints and jobs — makes the rules a
single, testable source of truth and satisfies the threat model's "explicit
state machine + transactional compare-and-set" elevation control (§2).

Lifecycle (BRD §10.4)::

    new -> drafted -> pending_approval -> approved -> queued -> published
                              |                          |
                              +--> rejected              +--> publish_failed -> dead_letter
                              +--> expired                    publish_failed -> queued (retry)

``published``, ``rejected``, ``expired`` and ``dead_letter`` are terminal.

Security invariants enforced here (threat model §1/§2, BRD §14.2):
  * A GET never calls this — only a POST performs the state change; this module
    is the POST-side sink, and it verifies the token BEFORE any mutation.
  * Reaching ``approved`` requires a signed, unexpired, single-use token bound to
    this exact draft and the ``approve`` action; verification (signature +
    expiry + single-use) runs *before* the state change (fail-closed).
  * The single-use nonce is consumed ATOMICALLY with the transition: the guarded
    compare-and-set UPDATE, the ``used_tokens`` insert and the ``audit_log``
    insert share one transaction, so a replay can never yield a second approval.
  * ``published`` is idempotent: re-publishing or re-approving an already
    published draft is a no-op (threat model "idempotency keys prevent duplicate
    posts"), never an error and never a second publish.
  * Any ambiguity (unknown source state, lost CAS race, duplicate nonce) rolls
    back and raises — nothing is half-applied.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vision.approval.errors import UsedToken as TokenAlreadyUsed
from vision.approval.state_errors import (
    IllegalTransition,
    StateMachineError,
    TokenBindingError,
    TokenRequired,
    TransitionConflict,
)
from vision.approval.tokens import _single_use_key, verify_token
from vision.config import get_settings
from vision.db.models import AuditLog, Draft, UsedToken
from vision.logging_setup import get_logger

# Module logger. The root redaction filter (logging_setup.RedactionFilter) scrubs
# any accidental secret, but we additionally never pass a raw token into a log
# call here — only the non-sensitive nonce hash and state names travel.
_logger = get_logger(__name__)


class DraftState(str, Enum):
    """The finite set of states a draft may hold (BRD §10.4).

    Subclasses ``str`` so a member compares/serialises exactly as its value — the
    ``drafts.state`` column stores a plain human-readable string, so
    ``DraftState.APPROVED == "approved"`` holds and the enum can be written to /
    read from the DB with no adapter. Using an enum (not free strings) means an
    unknown state fails loudly at the boundary instead of silently mis-routing.
    """

    NEW = "new"  # freshly created row, nothing generated yet
    DRAFTED = "drafted"  # synthesis produced candidate post text
    PENDING_APPROVAL = "pending_approval"  # email sent; awaiting the owner's decision
    APPROVED = "approved"  # owner approved via a valid single-use token
    QUEUED = "queued"  # approved + scheduled for its publish slot
    PUBLISHED = "published"  # live on LinkedIn — TERMINAL, idempotent
    REJECTED = "rejected"  # owner rejected — TERMINAL
    EXPIRED = "expired"  # approval window lapsed — TERMINAL
    PUBLISH_FAILED = "publish_failed"  # publish attempt failed; retry or dead-letter
    DEAD_LETTER = "dead_letter"  # gave up after retries — TERMINAL


# --- The transition graph --------------------------------------------------
# The ONLY permitted edges. Each key maps to the frozenset of states reachable
# from it in a single step; a terminal state maps to the empty frozenset (no
# outgoing edges). This map is the single source of truth for legality — the
# ``transition`` function never hard-codes an edge, it always consults this.
ALLOWED_TRANSITIONS: dict[DraftState, frozenset[DraftState]] = {
    DraftState.NEW: frozenset({DraftState.DRAFTED}),
    DraftState.DRAFTED: frozenset({DraftState.PENDING_APPROVAL}),
    # The approval fork: the owner approves or rejects, or the window expires.
    DraftState.PENDING_APPROVAL: frozenset(
        {DraftState.APPROVED, DraftState.REJECTED, DraftState.EXPIRED}
    ),
    DraftState.APPROVED: frozenset({DraftState.QUEUED}),
    # Publishing either succeeds (terminal) or fails into the retry lane.
    DraftState.QUEUED: frozenset({DraftState.PUBLISHED, DraftState.PUBLISH_FAILED}),
    # A failed publish may be retried (back to queued) or given up (dead-letter).
    DraftState.PUBLISH_FAILED: frozenset({DraftState.QUEUED, DraftState.DEAD_LETTER}),
    # Terminal states — no outgoing edges.
    DraftState.PUBLISHED: frozenset(),
    DraftState.REJECTED: frozenset(),
    DraftState.EXPIRED: frozenset(),
    DraftState.DEAD_LETTER: frozenset(),
}

# Terminal states are documented as a first-class constant so consumers (e.g. the
# scheduler, the ops dashboard) can ask "is this draft done?" without re-deriving
# it from the empty-frozenset rows above.
TERMINAL_STATES: frozenset[DraftState] = frozenset(
    state for state, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def _coerce_state(value: DraftState | str) -> DraftState:
    """Return ``value`` as a :class:`DraftState`, failing closed on anything else.

    ``draft.state`` is a plain string column and callers may pass either the enum
    or its string value, so both are accepted. An unrecognised string means a
    corrupt/tampered row or a typo'd argument — we raise :class:`IllegalTransition`
    (never silently coerce) so the transition is refused rather than mis-applied.
    """
    if isinstance(value, DraftState):
        return value
    try:
        return DraftState(value)
    except ValueError as exc:  # unknown state string — fail closed
        raise IllegalTransition(f"unknown draft state {value!r}") from exc


def _is_idempotent_noop(from_state: DraftState, to_state: DraftState) -> bool:
    """Return True when the requested move on a PUBLISHED draft must be a no-op.

    ``published`` is terminal and idempotent (threat model — idempotency keys
    prevent duplicate posts): a repeated publish, or a late re-approve of a link
    for a draft that already went live, must do nothing rather than raise or
    publish twice. Every other repeated/terminal move stays an
    :class:`IllegalTransition` so genuine mistakes still surface.
    """
    return from_state is DraftState.PUBLISHED and to_state in {
        DraftState.PUBLISHED,
        DraftState.APPROVED,
    }


def _make_is_used(session: Session):
    """Build the single-use callback ``verify_token`` needs, bound to ``session``.

    ``verify_token`` is DB-agnostic: it hands us the token's *hash* (never the raw
    token, threat model §1) and asks whether it was already consumed. We answer by
    probing ``used_tokens`` — where a spent nonce hash lives behind a UNIQUE
    constraint. Returning a plain closure keeps ``verify_token`` pure and testable.
    """

    def _is_used(token_hash: str) -> bool:
        # Presence of the hash means the token was already spent -> replay.
        return (
            session.query(UsedToken).filter(UsedToken.nonce == token_hash).first()
            is not None
        )

    return _is_used


def transition(
    session: Session,
    draft: Draft,
    to_state: DraftState | str,
    actor: str,
    meta: dict[str, object] | None = None,
) -> Draft:
    """Move ``draft`` to ``to_state``, enforcing every rule, atomically.

    Steps, in the security-critical order (verify BEFORE mutate, fail closed):

      1. Resolve the current and target states (unknown source -> IllegalTransition).
      2. If the draft is already ``published`` and this is a repeat publish /
         re-approve, return it unchanged — idempotent no-op (no audit row).
      3. Reject any edge not present in :data:`ALLOWED_TRANSITIONS`.
      4. For ``-> approved`` ONLY: require a token in ``meta['token']`` and verify
         its signature + expiry + single-use status and its binding to this draft
         and the ``approve`` action — all BEFORE the state changes.
      5. In ONE transaction: a guarded compare-and-set UPDATE (state changes only
         if the row is still in the expected source state), the ``used_tokens``
         insert (approval only — consumes the nonce), and the append-only
         ``audit_log`` insert. Commit them together, or roll everything back.

    Args:
      session: the active SQLAlchemy session (its transaction is the atomic unit).
      draft:   the ORM row to transition (must be persistent).
      to_state: target state, as a :class:`DraftState` or its string value.
      actor:   who initiated it — ``'owner'``, ``'system'``, a job name — recorded
               verbatim in the audit trail (threat model §1 repudiation control).
      meta:    optional context. Recognised keys, both stripped from the stored
               audit ``meta`` so they never leak: ``'token'`` (raw approval token,
               required for ``-> approved``) and ``'ip'`` (source IP, stored in the
               dedicated audit ``ip`` column). Any other keys are persisted as
               non-sensitive audit context.

    Returns:
      The same ``draft`` instance, refreshed to reflect the committed state.

    Raises:
      IllegalTransition: the edge is not allowed / the source state is corrupt.
      TokenRequired:     ``-> approved`` attempted without a token.
      TokenBindingError: a valid token is not bound to this draft / ``approve``.
      ExpiredToken / InvalidToken / BadAction: propagated from ``verify_token``.
      UsedToken:         the approval token was already consumed (replay).
      TransitionConflict: the compare-and-set lost a race (concurrent change).
    """
    # Never mutate the caller's dict (immutability principle); work on a copy.
    meta = dict(meta or {})

    from_state = _coerce_state(draft.state)
    target = _coerce_state(to_state)

    # (2) Idempotent no-op on a terminal, already-published draft. Return early
    # WITHOUT touching the DB or writing an audit row — the action already
    # happened; repeating it is a benign duplicate, not an event.
    if _is_idempotent_noop(from_state, target):
        _logger.info(
            "draft transition no-op (idempotent)",
            extra={"draft_id": str(draft.id), "from_state": from_state.value,
                   "to_state": target.value, "actor": actor},
        )
        return draft

    # (3) Legality: the edge must exist in the graph. This also rejects every
    # move out of a terminal state (their target sets are empty).
    if target not in ALLOWED_TRANSITIONS[from_state]:
        raise IllegalTransition(
            f"{from_state.value} -> {target.value} is not an allowed transition"
        )

    # Single event timestamp reused for verification reference, the used-token
    # ledger, and the audit row so they are mutually consistent.
    now = datetime.now(timezone.utc)

    # Build the audit meta up front, EXCLUDING the raw token and ip (ip has its
    # own column; the raw token must never be persisted — threat model §1).
    audit_meta: dict[str, object] = {
        key: value for key, value in meta.items() if key not in {"token", "ip"}
    }
    audit_meta["from_state"] = from_state.value
    audit_meta["to_state"] = target.value
    ip = meta.get("ip")

    # (4) Approval authorisation. Prepared here (before the write) so verification
    # fully precedes any mutation. ``used_row`` is inserted inside the atomic block.
    used_row: UsedToken | None = None
    if target is DraftState.APPROVED:
        token_str = meta.get("token")
        if not token_str or not isinstance(token_str, str):
            raise TokenRequired("approval requires a valid single-use token")

        settings = get_settings()
        # verify_token fails closed on bad signature / expiry / prior use; those
        # typed errors (ExpiredToken/InvalidToken/BadAction/UsedToken) propagate.
        verified = verify_token(
            token_str,
            settings.secret_hmac_key,
            now,
            _make_is_used(session),
        )
        # Defence in depth: even a valid token must be bound to THIS draft and the
        # approve action (threat model §1 — bind draft_id + action into the MAC).
        if verified.draft_id != str(draft.id):
            raise TokenBindingError("token is not bound to this draft")
        if verified.action != "approve":
            raise TokenBindingError("token action is not 'approve'")

        # The single-use key is the nonce-derived hash (stable across any token
        # re-encoding, §14.2). Storing it — never the raw token — consumes the
        # nonce; the UNIQUE constraint on used_tokens.nonce is the atomic backstop.
        token_hash = _single_use_key(verified.draft_id, verified.nonce)
        audit_meta["token_hash"] = token_hash  # nonce hash for repudiation audit
        used_row = UsedToken(
            nonce=token_hash, draft_id=draft.id, action="approve", used_at=now
        )

    # (5) The atomic unit: compare-and-set + (approval) nonce consumption + audit,
    # all in one transaction. Any failure rolls the whole thing back (fail-closed).
    try:
        # Compare-and-set: the row's state changes ONLY if it is still the state
        # we validated against. rowcount 0 => a concurrent actor moved it first.
        result = session.execute(
            update(Draft)
            .where(Draft.id == draft.id, Draft.state == from_state.value)
            .values(state=target.value),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount != 1:
            raise TransitionConflict(
                f"draft {draft.id} was not in expected state {from_state.value!r}"
            )

        if used_row is not None:
            # Flush now so the UNIQUE(nonce) constraint fires here (a concurrent
            # replay that slipped past the is-used check trips IntegrityError).
            session.add(used_row)
            session.flush()

        # Append-only audit row — one per real transition (threat model §1/§2).
        session.add(
            AuditLog(
                entity="draft",
                entity_id=str(draft.id),
                action=target.value,
                actor=actor,
                ip=ip if isinstance(ip, str) else None,
                meta=audit_meta,
                at=now,
            )
        )
        session.commit()
    except IntegrityError as exc:
        # Duplicate nonce => the token was already consumed in a racing request.
        session.rollback()
        raise TokenAlreadyUsed("approval token has already been consumed") from exc
    except (StateMachineError, Exception):
        # Any other failure: roll back so state, nonce, and audit stay consistent
        # (nothing half-applied), then re-raise for the caller to handle.
        session.rollback()
        raise

    # Sync the ORM object with the committed row (the CAS used a core UPDATE).
    session.refresh(draft)
    _logger.info(
        "draft transition committed",
        extra={"draft_id": str(draft.id), "from_state": from_state.value,
               "to_state": target.value, "actor": actor},
    )
    return draft
