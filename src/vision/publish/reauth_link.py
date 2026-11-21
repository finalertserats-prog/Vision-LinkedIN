"""Signed, expiring link that lets the owner reconnect LinkedIn from an email.

WHY: LinkedIn issues this app no refresh token, so every ~60 days the owner must
consent again. Before this link existed that meant a terminal session on the VPS.
The link opens ``/oauth/linkedin/start`` on vision-web, which only starts the
LinkedIn consent flow when the link's signature and expiry check out.

The link is not the only guard: the callback also refuses any LinkedIn member
other than the one already connected, so a forwarded or leaked link cannot attach
a stranger's account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta

from vision.config import Settings

REAUTH_START_PATH = "/oauth/linkedin/start"
REAUTH_CALLBACK_PATH = "/oauth/linkedin/callback"
# Long enough to cover a heads-up email sent days before expiry and read late.
REAUTH_LINK_TTL = timedelta(days=14)

_DEFAULT_BASE_URL = "http://localhost:8000"
# Domain separation: a key derived for this purpose only, so a re-auth link can
# never verify as an approval token (or the reverse) even though both use the
# same configured HMAC secret.
_PURPOSE = b"vision-linkedin-reauth-link-v1"


def _sign(expiry: str, secret: str) -> str:
    purpose_key = hmac.new(secret.encode("utf-8"), _PURPOSE, hashlib.sha256).digest()
    mac = hmac.new(purpose_key, expiry.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def issue_reauth_link_token(
    secret: str, now: datetime, ttl: timedelta = REAUTH_LINK_TTL
) -> str:
    """Return ``"<expiry-epoch>.<signature>"`` valid until ``now + ttl``."""
    expiry = str(int((now + ttl).timestamp()))
    return f"{expiry}.{_sign(expiry, secret)}"


def is_valid_reauth_link_token(token: str, secret: str, now: datetime) -> bool:
    """True only for an unexpired token signed with ``secret``. Never raises."""
    expiry, sep, signature = token.partition(".")
    # ASCII checks first: str.isdigit accepts non-ASCII digits, and
    # hmac.compare_digest raises on non-ASCII str input.
    # Bounded length: an epoch fits in 12 digits; refuse giant inputs outright.
    if not (
        sep
        and 0 < len(expiry) <= 12
        and expiry.isascii()
        and expiry.isdigit()
        and signature.isascii()
    ):
        return False
    if not hmac.compare_digest(signature, _sign(expiry, secret)):
        return False
    return int(expiry) > now.timestamp()


def public_base_url() -> str:
    """The public origin of vision-web (same env var the approval links use)."""
    return os.environ.get("VISION_APPROVAL_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def build_reauth_url(settings: Settings, now: datetime) -> str:
    """Absolute click-to-fix URL for an email."""
    token = issue_reauth_link_token(settings.secret_hmac_key, now)
    return f"{public_base_url()}{REAUTH_START_PATH}?t={token}"
