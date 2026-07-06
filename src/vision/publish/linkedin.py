"""LinkedInClient — official Posts API publishing over httpx (BRD §6, §15).

WHY this module exists: VISION publishes approved drafts to the owner's *personal*
LinkedIn profile through LinkedIn's self-serve ``w_member_social`` product. This
client is the single, isolated boundary between VISION and LinkedIn's HTTP API
(BRD risk table: "abstraction isolates the client" — a LinkedIn API change is a
one-file update here). It implements the full lifecycle:

    * 3-legged OAuth (authorize URL -> code exchange -> refresh),
    * member-URN discovery via OpenID Connect ``userinfo`` (§6 author URN),
    * image registration + binary upload via ``/rest/images`` (§15.2),
    * text and image posts via ``POST /rest/posts`` (§15.2),
    * post deletion (§6: editing is unsupported, so we delete + recreate).

Every outbound call carries the three mandatory headers from BRD §6:
``Authorization: Bearer <token>``, ``LinkedIn-Version: YYYYMM`` (from
``settings.li_version``) and ``X-Restli-Protocol-Version: 2.0.0``. Failures are
mapped onto the typed exceptions of ``errors.py`` per the §15.4 matrix.

IDEMPOTENCY (BRD §13/§15.4 duplicate guard): the Posts API is **not** natively
idempotent — a repeated ``publish_text`` creates a second post. VISION therefore
enforces at-most-once publishing *above* this client: a draft that already has a
stored ``post_urn`` is never re-published (a second Approve is a no-op). Callers
MUST guard on the persisted URN before invoking ``publish_*``; this client
deliberately performs no hidden retries on 2xx-capable, non-idempotent writes.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from vision.config import Settings, get_settings

from .errors import (
    LinkedInError,
    NeedsReauth,
    RateLimited,
    TransientLinkedInError,
)

_log = logging.getLogger(__name__)

# --- Endpoint constants -----------------------------------------------------
# Pinned as module constants (not scattered literals) so the LinkedIn surface is
# auditable in one place and a host change is a single edit. OAuth lives on
# www.linkedin.com; the REST + userinfo surface lives on api.linkedin.com (§6).
_OAUTH_BASE = "https://www.linkedin.com/oauth/v2"
_AUTHORIZE_URL = f"{_OAUTH_BASE}/authorization"
_TOKEN_URL = f"{_OAUTH_BASE}/accessToken"
_API_BASE = "https://api.linkedin.com"
_USERINFO_URL = f"{_API_BASE}/v2/userinfo"
_POSTS_URL = f"{_API_BASE}/rest/posts"
_IMAGES_URL = f"{_API_BASE}/rest/images"

# Least-privilege scopes (BRD §16): OpenID trio to read the member id + the one
# write scope needed to post. Space-delimited per the OAuth spec.
_SCOPES = "openid profile email w_member_social"

# The Restli protocol version is a fixed contract value required on every REST
# call (§6); it is not configurable and never changes for this API generation.
_RESTLI_VERSION = "2.0.0"

# Conservative network timeout so a hung LinkedIn socket cannot stall the
# publisher worker indefinitely (NFR-07 reliability). Connect/read/write/pool.
_TIMEOUT = httpx.Timeout(30.0)


class LinkedInClient:
    """Thin, fully-typed wrapper over LinkedIn's OAuth + Posts + Images APIs.

    Construction is cheap and side-effect free: the client owns one persistent
    ``httpx.Client`` for connection reuse. An ``httpx.Client`` may be injected
    (``transport``/``http_client``) purely to make tests hermetic — production
    code passes nothing and gets a real client.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        # Pull the validated settings singleton unless a test supplies its own,
        # so ``li_client_id`` / ``li_version`` etc. come from one source (§22
        # config-over-code) rather than being re-read from the environment here.
        self._settings = settings or get_settings()
        # Reuse one HTTP client for keep-alive; tests may inject a mock-backed one.
        self._http = http_client or httpx.Client(timeout=_TIMEOUT)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release the underlying HTTP connection pool.

        Explicit close keeps sockets from leaking when the publisher worker
        creates a client per run; also invoked by the context-manager exit.
        """
        self._http.close()

    def __enter__(self) -> LinkedInClient:
        # Support ``with LinkedInClient() as li:`` so callers get deterministic
        # cleanup without remembering to call ``close``.
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- header assembly ----------------------------------------------------

    def _auth_headers(self, access_token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Build the three mandatory LinkedIn headers (§6) plus any extras.

        Centralised so *every* authenticated call is guaranteed to send the
        version + protocol headers — a missing ``LinkedIn-Version`` is a common,
        silent cause of 426/400s, so we never hand-roll headers at call sites.
        The version comes from ``settings.li_version`` so API drift is one config
        change, not a code edit (§22).
        """
        headers = {
            # Bearer auth is the OAuth access token; redaction filter masks it in logs.
            "Authorization": f"Bearer {access_token}",
            # Pins the API generation VISION was built against (§6).
            "LinkedIn-Version": self._settings.li_version,
            # Fixed Rest.li protocol contract required on the REST surface (§6).
            "X-Restli-Protocol-Version": _RESTLI_VERSION,
        }
        if extra:
            # Merge into a NEW dict (immutability §22) — never mutate a shared base.
            headers = {**headers, **extra}
        return headers

    # -- error mapping ------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        """Map a non-2xx response onto the §15.4 typed-exception matrix.

        WHY here and not ``response.raise_for_status()``: httpx raises one generic
        error, but the publisher needs to branch — refresh on 401, alert on 403,
        back off on 429/5xx. We translate once, at the boundary, so no status
        codes leak into business logic.
        """
        code = response.status_code
        # 2xx is success — nothing to raise.
        if code < 400:
            return

        # Parse Retry-After defensively; LinkedIn may send seconds as a string.
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))

        if code == 401:
            # Token dead/expired: signal the caller to refresh, then re-auth if
            # that fails. Never retried blindly with the same token (§15.4 401).
            raise NeedsReauth(f"{context}: unauthorized (401)")
        if code == 403:
            # Scope/role/product misconfiguration — not fixable by retrying;
            # surfaced as a hard error so ops gets alerted (§15.4 403).
            raise LinkedInError(f"{context}: forbidden (403)", status_code=403)
        if code == 429:
            # Throttled; safe to retry after the requested cool-down (§15.4 429).
            raise RateLimited(f"{context}: rate limited (429)", retry_after=retry_after)
        if code >= 500:
            # LinkedIn-side failure; retry with backoff, then dead-letter (§15.4 5xx).
            raise TransientLinkedInError(
                f"{context}: server error ({code})",
                status_code=code,
                retry_after=retry_after,
            )
        # Any other 4xx (400/404/409/422 …) is a request-shape bug on our side —
        # not retryable, surfaced with the body excerpt for debugging.
        raise LinkedInError(
            f"{context}: request failed ({code}): {response.text[:500]}",
            status_code=code,
        )

    # -- OAuth: authorize ---------------------------------------------------

    def build_authorize_url(self, state: str) -> str:
        """Return the LinkedIn consent URL to send the owner to (§15.1 step 4).

        ``state`` is an opaque anti-CSRF nonce the caller generates and later
        verifies on the callback — it is echoed back unchanged by LinkedIn, so a
        mismatch means a forged/replayed callback and MUST be rejected. Scopes are
        least-privilege (§16). No secret is included: the authorization endpoint
        only takes the *public* client id.
        """
        params = {
            "response_type": "code",  # 3-legged authorization-code flow (§6)
            "client_id": self._settings.li_client_id,
            "redirect_uri": self._settings.li_redirect_uri,
            "state": state,
            "scope": _SCOPES,
        }
        # urlencode guarantees every value (redirect_uri, scope spaces) is escaped.
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    # -- OAuth: token exchange / refresh ------------------------------------

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Trade an authorization ``code`` for access + refresh tokens (§6).

        Returns LinkedIn's raw token JSON (``access_token``, ``refresh_token``,
        ``expires_in``, ``refresh_token_expires_in``, …). The caller encrypts and
        persists these (§15.3) — this client is stateless and holds no tokens.
        """
        # Standard OAuth authorization-code grant; the client secret is sent in
        # the POST body over TLS (never in a URL/log).
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.li_redirect_uri,
            "client_id": self._settings.li_client_id,
            "client_secret": self._settings.li_client_secret,
        }
        return self._token_request(form, context="exchange_code")

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        """Exchange a refresh token for a fresh access token (§15.3).

        Called proactively by the token job before the ~60-day access token
        expires, and reactively by the publisher after a ``NeedsReauth``. Returns
        the same token-JSON shape as ``exchange_code``.
        """
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._settings.li_client_id,
            "client_secret": self._settings.li_client_secret,
        }
        return self._token_request(form, context="refresh")

    def _token_request(self, form: dict[str, str], *, context: str) -> dict[str, Any]:
        """POST a form-encoded OAuth grant and return the parsed token JSON.

        Shared by ``exchange_code`` and ``refresh`` so the content-type, error
        mapping and JSON parsing live in exactly one place.
        """
        # OAuth token endpoint requires ``application/x-www-form-urlencoded`` —
        # httpx sets that automatically when we pass ``data=``.
        response = self._http.post(_TOKEN_URL, data=form)
        # A failed refresh (invalid/expired refresh token) commonly returns 400;
        # the matrix turns that into a non-retryable LinkedInError so the token
        # job alerts the owner to re-authorise rather than looping.
        self._raise_for_status(response, context)
        return response.json()

    # -- OpenID: member URN -------------------------------------------------

    def get_member_urn(self, access_token: str) -> str:
        """Resolve the author URN ``urn:li:person:{sub}`` via userinfo (§6).

        The Posts API needs the member's URN as ``author``. Per §6 the id is the
        OpenID Connect ``sub`` claim from the ``userinfo`` endpoint (enabled by
        the ``openid`` scope), NOT anything parsed from the token itself.
        """
        response = self._http.get(_USERINFO_URL, headers=self._auth_headers(access_token))
        self._raise_for_status(response, "get_member_urn")
        # ``sub`` is the stable member id; format it into the URN shape the Posts
        # API expects as ``author``.
        sub = response.json()["sub"]
        return f"urn:li:person:{sub}"

    # -- Images: register + upload ------------------------------------------

    def upload_image(
        self,
        access_token: str,
        image_bytes: bytes,
        *,
        owner_urn: str | None = None,
    ) -> str:
        """Register + upload an image, returning its ``urn:li:image:...`` (§15.2).

        Two-step LinkedIn protocol (a single "upload" verb does not exist):
          1. ``POST /rest/images?action=initializeUpload`` with the owner URN →
             LinkedIn returns a one-time ``uploadUrl`` and the final image URN.
          2. ``PUT`` the raw bytes to that ``uploadUrl``.
        The returned URN is then referenced by ``publish_with_image``.

        ``owner_urn`` defaults to the member URN resolved from the token, so
        callers that already know it (they usually do, from the draft) can skip a
        redundant userinfo round-trip.
        """
        # Determine the owner: initializeUpload requires it, so resolve lazily
        # only when the caller didn't already provide it (saves an HTTP call).
        owner = owner_urn or self.get_member_urn(access_token)

        # Step 1: initialize — the ?action= query param is how Rest.li models an
        # RPC-style action on a collection.
        init_response = self._http.post(
            _IMAGES_URL,
            params={"action": "initializeUpload"},
            headers=self._auth_headers(
                access_token, {"Content-Type": "application/json"}
            ),
            json={"initializeUploadRequest": {"owner": owner}},
        )
        self._raise_for_status(init_response, "upload_image.initialize")
        # LinkedIn nests the handshake under ``value``: the short-lived upload URL
        # and the durable image URN we ultimately attach to the post.
        value = init_response.json()["value"]
        upload_url = value["uploadUrl"]
        image_urn = value["image"]

        # Step 2: PUT the binary. The upload endpoint still requires the bearer
        # token; it does NOT take the version/protocol headers (it's a raw blob
        # sink), so we send auth only.
        put_response = self._http.put(
            upload_url,
            content=image_bytes,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self._raise_for_status(put_response, "upload_image.put")
        return image_urn

    # -- Posts: publish -----------------------------------------------------

    def publish_text(
        self,
        access_token: str,
        author_urn: str,
        text: str,
        visibility: str = "PUBLIC",
    ) -> str:
        """Publish a text-only post; return the created post URN (§15.2).

        Body shape follows the Posts API contract: ``author`` (member URN),
        ``commentary`` (the post text), ``visibility``, ``lifecycleState`` =
        ``PUBLISHED`` and explicit MAIN_FEED distribution defaults. NOT idempotent
        — see the module docstring; the caller guards on the stored URN.
        """
        # Build the canonical text-post payload; distribution is spelled out so
        # behaviour is explicit rather than relying on implicit API defaults.
        payload = _base_post_payload(author_urn, text, visibility)
        return self._create_post(access_token, payload, context="publish_text")

    def publish_with_image(
        self,
        access_token: str,
        author_urn: str,
        text: str,
        image_urn: str,
        visibility: str = "PUBLIC",
    ) -> str:
        """Publish a post with one attached image; return the post URN (§15.2).

        Identical to ``publish_text`` plus a ``content.media`` block referencing
        the ``image_urn`` previously obtained from ``upload_image``. v1 attaches a
        single image (§13.6); carousels are a later enhancement.
        """
        payload = _base_post_payload(author_urn, text, visibility)
        # Attach the pre-uploaded image by URN. ``altText`` aids accessibility;
        # the image itself is text-free per the precision-first image policy.
        payload = {
            **payload,
            "content": {"media": {"id": image_urn, "altText": "Post image"}},
        }
        return self._create_post(access_token, payload, context="publish_with_image")

    def _create_post(
        self, access_token: str, payload: dict[str, Any], *, context: str
    ) -> str:
        """POST a fully-built payload to ``/rest/posts`` and extract the URN.

        Shared by both publish methods. The created post's URN is returned in the
        ``x-restli-id`` response header (LinkedIn's convention for create); we
        fall back to the body ``id`` for resilience against header casing.
        """
        response = self._http.post(
            _POSTS_URL,
            headers=self._auth_headers(
                access_token, {"Content-Type": "application/json"}
            ),
            json=payload,
        )
        self._raise_for_status(response, context)
        return _extract_post_urn(response)

    # -- Posts: delete ------------------------------------------------------

    def delete(self, access_token: str, post_urn: str) -> None:
        """Delete a published post (§6: no edit API → delete + recreate).

        Used by the STAGING E2E "post-then-delete" test (§18.1) and by any
        recovery flow. The URN must be URL-encoded because it contains ``:``
        characters that would otherwise break the path.
        """
        # quote(..., safe="") escapes the colons in ``urn:li:share:123`` so the
        # path segment is well-formed.
        encoded = quote(post_urn, safe="")
        response = self._http.delete(
            f"{_POSTS_URL}/{encoded}",
            headers=self._auth_headers(access_token),
        )
        # A 404 here means the post is already gone — treat as success (the goal
        # state is "not present"); everything else goes through the matrix.
        if response.status_code == 404:
            return
        self._raise_for_status(response, "delete")


# --- Module-level helpers ---------------------------------------------------
# Kept as free functions (pure, no ``self``) so they are trivially unit-testable
# and carry no client state.


def _base_post_payload(author_urn: str, text: str, visibility: str) -> dict[str, Any]:
    """Return the shared ``/rest/posts`` body for text and image posts (§15.2)."""
    return {
        "author": author_urn,  # inherently the owner (personal profile, §15.6)
        "commentary": text,  # the post body exactly as approved
        "visibility": visibility,  # PUBLIC by default
        # Explicit distribution: main feed, no external channels, no targeting.
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",  # publish immediately (no native drafts)
        "isReshareDisabledByAuthor": False,
    }


def _extract_post_urn(response: httpx.Response) -> str:
    """Pull the created post URN from the create response.

    LinkedIn returns the new resource id in the ``x-restli-id`` header on a 201.
    We prefer the header (authoritative for creates) and fall back to a body
    ``id`` field so a header-casing or API-shape change still yields the URN.
    """
    header_urn = response.headers.get("x-restli-id")
    if header_urn:
        return header_urn
    # Fallback: some responses echo the id in the JSON body.
    body = response.json() if response.content else {}
    urn = body.get("id")
    if not urn:
        # Fail loudly (§22): a publish with no URN means we cannot record/idempotency-
        # guard the post, which is worse than an error.
        raise LinkedInError("publish succeeded but no post URN was returned")
    return urn


def _parse_retry_after(raw: str | None) -> float | None:
    """Best-effort parse of a ``Retry-After`` header into seconds.

    LinkedIn sends a delta-seconds integer; we tolerate junk by returning None so
    a malformed header degrades to the caller's default backoff rather than
    crashing the error path.
    """
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        # Non-numeric (or HTTP-date) Retry-After: let the backoff scheduler decide.
        return None
