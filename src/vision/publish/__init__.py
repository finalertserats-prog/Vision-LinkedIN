"""Publish package — LinkedIn client + publisher worker (§15).

Exposes the ``LinkedInClient`` (OAuth + Posts/Images API) and the typed error
hierarchy (§15.4) so callers can ``from vision.publish import LinkedInClient,
NeedsReauth`` without reaching into submodules. The token lifecycle job and the
idempotent publisher worker build on top of this client.
"""

from .errors import (
    LinkedInError,
    NeedsReauth,
    RateLimited,
    TransientLinkedInError,
)
from .linkedin import LinkedInClient

__all__ = [
    "LinkedInClient",
    "LinkedInError",
    "NeedsReauth",
    "RateLimited",
    "TransientLinkedInError",
]
