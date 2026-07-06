"""Typed exceptions for the signed approval-token module (BRD §14.2 / NFR-04).

WHY a dedicated module: the token verifier must *fail closed* (NFR-04) and the
FastAPI approval endpoints need to distinguish *why* a link was rejected so they
can render the right friendly page and write an accurate ``audit_log`` reason.
Separating the exception types (rather than raising a bare ``ValueError``) makes
each failure mode explicit, catchable, and testable — and keeps the verifier's
control flow readable.

All exceptions share a common ``ApprovalTokenError`` base so a caller that only
cares "was this link valid at all?" can catch the base, while a caller that
wants to branch (expired vs. replayed vs. tampered) can catch the specific type.
"""

from __future__ import annotations


class ApprovalTokenError(Exception):
    """Base class for every approval-token failure.

    Endpoints can catch this to render a single generic "link no longer valid"
    page (§14.2) without leaking which specific check failed to an attacker,
    while internal logging still records the concrete subclass for audit.
    """


class InvalidToken(ApprovalTokenError):
    """Token is malformed or its HMAC signature does not verify.

    Raised for tampering, truncation, wrong secret, or any structural defect.
    Deliberately generic so a signature mismatch and a garbled payload look the
    same to the caller — an attacker probing the endpoint learns nothing.
    """


class ExpiredToken(ApprovalTokenError):
    """Token's embedded expiry (``exp``) is at or before the verification time.

    Distinct from ``InvalidToken`` so the endpoint can tell the owner the link
    simply timed out (default cutoff 20:00 IST, §14.2) rather than implying
    something malicious happened.
    """


class UsedToken(ApprovalTokenError):
    """Token has already been consumed (single-use guarantee, §14.2).

    Raised when the ``is_used_callback`` reports the token's hash is already in
    ``used_tokens``. Enforces the single-use property that stops a captured
    Approve link from being replayed to post twice.
    """


class BadAction(ApprovalTokenError):
    """Action is not one of the allowed, action-scoped values.

    The token is scoped to exactly one of ``approve|reject|edit|post_now``.
    Anything else (at issue time or decoded at verify time) is rejected so a
    token minted for one action can never be coerced into performing another.
    """
