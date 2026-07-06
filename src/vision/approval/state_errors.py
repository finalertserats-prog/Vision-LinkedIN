"""Typed exceptions for the draft state machine (BRD §10.4 / §22, threat model §2).

WHY a dedicated module (mirrors ``approval/errors.py`` for tokens): the state
machine must *fail closed* on any ambiguity, and the FastAPI approval endpoints
need to tell *why* a transition was refused so they can render the right generic
page and write an accurate ``audit_log`` reason — WITHOUT leaking internals to an
attacker. Distinct exception types (never a bare ``ValueError``) make every
refusal explicit, catchable, and testable.

All exceptions share a common :class:`StateMachineError` base so a caller that
only cares "did the transition happen?" can catch the base, while a caller that
must branch (illegal edge vs. missing token vs. lost race) can catch the precise
type.
"""

from __future__ import annotations


class StateMachineError(Exception):
    """Base class for every draft state-machine failure.

    Endpoints can catch this to render a single generic "action could not be
    completed" page (threat model §2 — generic errors) without disclosing which
    specific guard tripped, while internal logging still records the concrete
    subclass for the audit trail.
    """


class IllegalTransition(StateMachineError):
    """The requested edge is not present in the ``ALLOWED`` transition graph.

    Raised when moving from the draft's current state to the target state is not
    a permitted edge (e.g. ``new -> approved``), or when the source state is
    terminal (``published`` / ``rejected`` / ``expired`` / ``dead_letter``) and
    has no outgoing edges. This is the state-machine analogue of the threat
    model's "invalid state transition" elevation guard (§2).
    """


class TokenRequired(StateMachineError):
    """Reaching ``approved`` was attempted without a valid approval token.

    Approval is the one transition that mutates the owner's real profile, so it
    MUST be authorised by a signed, unexpired, single-use token (BRD §10.4 /
    §14.2). Raised when no token is supplied for an ``-> approved`` transition,
    before any state change happens (fail-closed).
    """


class TokenBindingError(StateMachineError):
    """A supplied token is valid but is not bound to *this* draft/action.

    Defence in depth (threat model §1 — "bind ``draft_id`` and normalized action
    into the MAC"): even a cryptographically valid token is rejected here if its
    signed ``draft_id`` does not match the draft being transitioned, or its
    signed action is not ``approve``. Stops a token minted for one draft/action
    from being coerced into approving another.
    """


class TransitionConflict(StateMachineError):
    """The atomic compare-and-set lost a race — the row changed underneath us.

    Raised when the guarded ``UPDATE ... WHERE state = <expected>`` affects zero
    rows, meaning a concurrent actor already moved the draft out of the expected
    source state. Prevents double-approval / double-publish (threat model §2 —
    "transactional compare-and-set"); the whole unit of work is rolled back so
    nothing is half-applied.
    """
