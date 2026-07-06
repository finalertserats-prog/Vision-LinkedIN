"""Typed exceptions for the LinkedIn publishing layer (BRD §15.4).

WHY this module exists: the error matrix in BRD §15.4 demands *distinct*
handling per failure class — 401 must trigger a token refresh / re-auth alert,
403 is a hard config problem, 429 and 5xx are transient and must be retried with
backoff. Encoding each case as its own exception type lets callers (the publisher
worker) branch on the class instead of re-parsing HTTP status codes, and lets the
retry loop simply ask ``exc.retryable`` rather than duplicating the matrix.
"""

from __future__ import annotations


class LinkedInError(Exception):
    """Base error for every failure raised by ``LinkedInClient``.

    WHY a common base: callers that only care "did LinkedIn fail?" can catch this
    one type, while callers that need the §15.4 matrix catch the specific
    subclasses. ``status_code`` and ``retryable`` are carried on the base so the
    retry loop can inspect any subclass uniformly.
    """

    # Default: an unclassified LinkedIn error is NOT safe to blindly retry, since
    # retrying a non-idempotent publish could double-post (§15.4 duplicate guard).
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        # Keep the human-readable message on the exception for logs/alerts, plus
        # the raw status so observability can group failures without re-parsing.
        super().__init__(message)
        self.status_code = status_code
        # ``retry_after`` mirrors the HTTP Retry-After header when present so the
        # backoff scheduler can honour LinkedIn's requested cool-down (§15.4 429).
        self.retry_after = retry_after


class NeedsReauth(LinkedInError):
    """Raised on 401 — the access token is invalid/expired (BRD §15.4 401).

    WHY not retryable: a bare retry with the same dead token will fail again. The
    caller must first refresh the access token (``LinkedInClient.refresh``) and,
    if that also fails, alert the owner to re-authorise. ``needs_refresh`` is the
    explicit signal the publisher worker keys off to attempt exactly one refresh
    before giving up, so the approved draft is never lost.
    """

    retryable = False

    def __init__(
        self, message: str = "LinkedIn access token rejected (401)", **kwargs: object
    ) -> None:
        # ``status_code`` is forced to 401 so the type and the code always agree.
        super().__init__(message, status_code=401, **kwargs)  # type: ignore[arg-type]
        # Distinct from ``retryable``: this tells the caller *how* to recover
        # (refresh the token) rather than *whether* to blindly re-issue the call.
        self.needs_refresh = True


class RateLimited(LinkedInError):
    """Raised on 429 — too many calls; defensive since we run far under the cap.

    WHY retryable: the request was well-formed and simply throttled, so retrying
    later (after ``retry_after``) is safe and expected (BRD §15.4 429).
    """

    retryable = True

    def __init__(
        self, message: str = "LinkedIn rate limit hit (429)", **kwargs: object
    ) -> None:
        super().__init__(message, status_code=429, **kwargs)  # type: ignore[arg-type]


class TransientLinkedInError(LinkedInError):
    """Raised on 5xx — LinkedIn-side failure that should be retried (§15.4 5xx).

    WHY retryable: a 5xx means the request never reached a stable outcome on
    LinkedIn's side, so exponential-backoff retry is correct; only after capped
    retries does the caller dead-letter + alert.
    """

    retryable = True
