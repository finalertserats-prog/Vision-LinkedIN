"""Unit tests for ``LinkedInClient`` — all HTTP is mocked with respx (BRD §18.1).

WHY respx: BRD §18 mandates mocking external deps (never internal logic) and
forbids real network in the suite. respx intercepts httpx at the transport layer,
so the client's *real* request-building code runs (headers, URLs, payloads) while
LinkedIn is faked — letting us assert the §6/§15.2 contract exactly.

All tests follow AAA (Arrange → Act → Assert) with one behavioural assertion each.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest
import respx

from vision.config import Settings, get_settings
from vision.publish import (
    LinkedInClient,
    LinkedInError,
    NeedsReauth,
    RateLimited,
    TransientLinkedInError,
)

# --- Test constants ---------------------------------------------------------
# Fixed, obviously-fake values so nothing here could match a real credential and
# so assertions can compare against known strings.
_TOKEN = "fake-access-token"  # noqa: S105 - test placeholder, not a real secret
_VERSION = "202506"
_CLIENT_ID = "test-client-id"
_AUTHOR = "urn:li:person:ABC123"
_POSTS_URL = "https://api.linkedin.com/rest/posts"
_IMAGES_URL = "https://api.linkedin.com/rest/images"
_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings with a pinned version + known client id.

    model_copy(update=...) sets fields by name (bypassing env/aliases), giving a
    hermetic config without touching the real environment.
    """
    get_settings.cache_clear()
    return get_settings().model_copy(
        update={
            "li_version": _VERSION,
            "li_client_id": _CLIENT_ID,
            "li_client_secret": "test-secret",  # noqa: S106 - test placeholder
            "li_redirect_uri": "https://vps.example/oauth/linkedin/callback",
        }
    )


@pytest.fixture
def client(settings: Settings) -> LinkedInClient:
    """A client wired to the deterministic settings and a real (mocked) httpx."""
    return LinkedInClient(settings=settings, http_client=httpx.Client())


# --- OAuth: authorize URL (no HTTP) ----------------------------------------


def test_build_authorize_url_includes_scopes_state_and_client_id(
    client: LinkedInClient,
) -> None:
    # Arrange: a caller-supplied anti-CSRF state nonce.
    state = "csrf-nonce-xyz"

    # Act: build the consent URL.
    url = client.build_authorize_url(state)

    # Assert: the query carries the exact OAuth params VISION relies on.
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [_CLIENT_ID]
    assert query["state"] == [state]
    assert query["scope"] == ["openid profile email w_member_social"]


# --- OAuth: token exchange / refresh ---------------------------------------


@respx.mock
def test_exchange_code_returns_parsed_tokens(client: LinkedInClient) -> None:
    # Arrange: LinkedIn returns a standard token JSON for the code grant.
    route = respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 5184000,
            },
        )
    )

    # Act.
    tokens = client.exchange_code("auth-code-123")

    # Assert: the client parsed and returned LinkedIn's token payload.
    assert route.called
    assert tokens["access_token"] == "new-access"


@respx.mock
def test_refresh_sends_refresh_grant(client: LinkedInClient) -> None:
    # Arrange.
    route = respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "refreshed"})
    )

    # Act.
    tokens = client.refresh("a-refresh-token")

    # Assert: a refresh_token grant was issued and the new access token returned.
    body = parse_qs(route.calls.last.request.content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert tokens["access_token"] == "refreshed"


# --- OpenID: member URN -----------------------------------------------------


@respx.mock
def test_get_member_urn_builds_person_urn_from_sub(client: LinkedInClient) -> None:
    # Arrange: userinfo returns the OpenID ``sub`` claim (§6).
    respx.get(_USERINFO_URL).mock(
        return_value=httpx.Response(200, json={"sub": "ABC123"})
    )

    # Act.
    urn = client.get_member_urn(_TOKEN)

    # Assert: the sub is formatted into the author URN shape the Posts API needs.
    assert urn == "urn:li:person:ABC123"


# --- Posts: publish_text ----------------------------------------------------


@respx.mock
def test_publish_text_sends_mandatory_headers(client: LinkedInClient) -> None:
    # Arrange: a successful create returns the URN in the x-restli-id header.
    route = respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:999"})
    )

    # Act.
    client.publish_text(_TOKEN, _AUTHOR, "hello world")

    # Assert: all three BRD §6 headers are present with the configured version.
    sent = route.calls.last.request.headers
    assert sent["Authorization"] == f"Bearer {_TOKEN}"
    assert sent["LinkedIn-Version"] == _VERSION
    assert sent["X-Restli-Protocol-Version"] == "2.0.0"


@respx.mock
def test_publish_text_returns_parsed_urn_from_header(client: LinkedInClient) -> None:
    # Arrange.
    respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:42"})
    )

    # Act.
    urn = client.publish_text(_TOKEN, _AUTHOR, "text")

    # Assert: the created post URN is extracted from the create response.
    assert urn == "urn:li:share:42"


@respx.mock
def test_publish_text_targets_correct_endpoint_with_commentary(
    client: LinkedInClient,
) -> None:
    # Arrange.
    route = respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})
    )

    # Act.
    client.publish_text(_TOKEN, _AUTHOR, "the post body")

    # Assert: correct endpoint + the post text lands in ``commentary`` (§15.2).
    request = route.calls.last.request
    assert str(request.url) == _POSTS_URL
    assert json.loads(request.content)["commentary"] == "the post body"


# --- Images: upload flow ----------------------------------------------------


@respx.mock
def test_upload_image_registers_then_puts_bytes(client: LinkedInClient) -> None:
    # Arrange: step 1 initializeUpload returns an upload URL + image URN; step 2
    # is a raw PUT of the bytes to that URL.
    upload_url = "https://upload.linkedin.example/blob/xyz"
    init = respx.post(f"{_IMAGES_URL}").mock(
        return_value=httpx.Response(
            200,
            json={"value": {"uploadUrl": upload_url, "image": "urn:li:image:IMG1"}},
        )
    )
    put = respx.put(upload_url).mock(return_value=httpx.Response(201))

    # Act: owner_urn provided so no userinfo round-trip is needed.
    image_urn = client.upload_image(_TOKEN, b"\x89PNG-bytes", owner_urn=_AUTHOR)

    # Assert: both steps ran and the durable image URN is returned.
    assert init.called
    assert put.called
    assert image_urn == "urn:li:image:IMG1"


@respx.mock
def test_upload_image_resolves_owner_via_userinfo_when_absent(
    client: LinkedInClient,
) -> None:
    # Arrange: no owner_urn passed → the client must call userinfo first.
    userinfo = respx.get(_USERINFO_URL).mock(
        return_value=httpx.Response(200, json={"sub": "OWNER1"})
    )
    respx.post(_IMAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={"value": {"uploadUrl": "https://up.example/x", "image": "urn:li:image:I2"}},
        )
    )
    respx.put("https://up.example/x").mock(return_value=httpx.Response(201))

    # Act.
    client.upload_image(_TOKEN, b"bytes")

    # Assert: the owner was resolved through the OpenID userinfo endpoint.
    assert userinfo.called


# --- Posts: publish_with_image ---------------------------------------------


@respx.mock
def test_publish_with_image_attaches_media_urn(client: LinkedInClient) -> None:
    # Arrange.
    route = respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:img"})
    )

    # Act.
    urn = client.publish_with_image(_TOKEN, _AUTHOR, "caption", "urn:li:image:IMG9")

    # Assert: the image URN is referenced under content.media.id and URN returned.
    body = json.loads(route.calls.last.request.content)
    assert body["content"]["media"]["id"] == "urn:li:image:IMG9"
    assert urn == "urn:li:share:img"


# --- Error matrix (§15.4) ---------------------------------------------------


@respx.mock
def test_publish_text_401_raises_needs_reauth(client: LinkedInClient) -> None:
    # Arrange: an expired/invalid token → 401.
    respx.post(_POSTS_URL).mock(return_value=httpx.Response(401))

    # Act / Assert: signals the caller to refresh (not a blind retry).
    with pytest.raises(NeedsReauth) as exc:
        client.publish_text(_TOKEN, _AUTHOR, "text")
    assert exc.value.needs_refresh is True
    assert exc.value.retryable is False


@respx.mock
def test_publish_text_429_is_retryable_rate_limited(client: LinkedInClient) -> None:
    # Arrange: throttled with a Retry-After hint.
    respx.post(_POSTS_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "12"})
    )

    # Act / Assert: raised as a retryable RateLimited carrying the backoff hint.
    with pytest.raises(RateLimited) as exc:
        client.publish_text(_TOKEN, _AUTHOR, "text")
    assert exc.value.retryable is True
    assert exc.value.retry_after == 12.0


@respx.mock
def test_publish_text_5xx_is_retryable_transient(client: LinkedInClient) -> None:
    # Arrange: LinkedIn-side failure.
    respx.post(_POSTS_URL).mock(return_value=httpx.Response(503))

    # Act / Assert: raised as a retryable transient error (§15.4 5xx).
    with pytest.raises(TransientLinkedInError) as exc:
        client.publish_text(_TOKEN, _AUTHOR, "text")
    assert exc.value.retryable is True
    assert exc.value.status_code == 503


@respx.mock
def test_publish_text_403_is_non_retryable_error(client: LinkedInClient) -> None:
    # Arrange: scope/product misconfiguration → 403.
    respx.post(_POSTS_URL).mock(return_value=httpx.Response(403))

    # Act / Assert: a hard, non-retryable LinkedInError (alert, don't retry).
    with pytest.raises(LinkedInError) as exc:
        client.publish_text(_TOKEN, _AUTHOR, "text")
    assert exc.value.retryable is False
    assert exc.value.status_code == 403


# --- Posts: delete ----------------------------------------------------------


@respx.mock
def test_delete_url_encodes_urn_and_succeeds(client: LinkedInClient) -> None:
    # Arrange: the URN's colons must be percent-encoded into the path.
    post_urn = "urn:li:share:12345"
    encoded_url = f"{_POSTS_URL}/{quote(post_urn, safe='')}"
    route = respx.delete(encoded_url).mock(return_value=httpx.Response(204))

    # Act.
    client.delete(_TOKEN, post_urn)

    # Assert: the DELETE hit the correctly-encoded resource path.
    assert route.called


@respx.mock
def test_delete_treats_404_as_success(client: LinkedInClient) -> None:
    # Arrange: post already gone.
    post_urn = "urn:li:share:gone"
    respx.delete(f"{_POSTS_URL}/{quote(post_urn, safe='')}").mock(
        return_value=httpx.Response(404)
    )

    # Act / Assert: goal state (absent) reached → no exception raised.
    client.delete(_TOKEN, post_urn)
