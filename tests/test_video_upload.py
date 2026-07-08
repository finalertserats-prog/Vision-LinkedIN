"""Unit tests for ``LinkedInVideoUploader`` — fully hermetic, no network/clock.

WHY a fake client instead of respx: the chunked video flow drives many
signed-per-part upload URLs and we assert on ETag ORDER + per-part retry, which is
easier to introspect with an injected fake httpx-like client that records every
call and replays canned responses. A fake ``sleep`` collapses all backoff/poll
waits to nothing.

All tests follow AAA (Arrange → Act → Assert) with one behavioural assertion each.
"""

from __future__ import annotations

from typing import Any

import pytest

from vision.config import Settings, get_settings
from vision.video.schema import UploadResult
from vision.video.upload import LinkedInVideoUploader, VideoUploadError

# --- Test constants ---------------------------------------------------------
# Obviously-fake values so nothing here could match a real credential.
_TOKEN = "fake-access-token"  # noqa: S105 - test placeholder, not a real secret
_VERSION = "202506"
_MEMBER = "urn:li:person:ABC123"
_VIDEO_URN = "urn:li:video:XYZ789"
_UPLOAD_TOKEN = "opaque-upload-token"  # noqa: S105 - test placeholder
_PART_URL_0 = "https://upload.example/part0?sig=aaa"
_PART_URL_1 = "https://upload.example/part1?sig=bbb"


class _FakeResponse:
    """Minimal httpx.Response stand-in: status, JSON body and headers only."""

    def __init__(
        self,
        status_code: int,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body or {}
        self.headers = headers or {}
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._json_body


class _FakeClient:
    """Records requests and replays scripted responses by (method, url) key.

    ``script`` maps a call key to a LIST of responses consumed in order, so a part
    URL can 500 once then 200. ``calls`` records every request for assertions.
    """

    def __init__(self, script: dict[tuple[str, str], list[_FakeResponse]]) -> None:
        self._script = {key: list(responses) for key, responses in script.items()}
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        # Record the full call so tests can assert order, bodies and content.
        self.calls.append({"method": method, "url": url, **kwargs})
        # Poll GETs reuse one URL repeatedly; keep the last response once drained.
        queue = self._script[(method, url)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _init_response() -> _FakeResponse:
    """Canned initializeUpload body: two ordered parts + urn + token."""
    return _FakeResponse(
        200,
        json_body={
            "value": {
                "video": _VIDEO_URN,
                "uploadToken": _UPLOAD_TOKEN,
                "uploadInstructions": [
                    {"firstByte": 0, "lastByte": 3, "uploadUrl": _PART_URL_0},
                    {"firstByte": 4, "lastByte": 7, "uploadUrl": _PART_URL_1},
                ],
            }
        },
    )


def _part_ok(etag: str) -> _FakeResponse:
    """A successful part PUT carrying its proof-of-part ETag header."""
    return _FakeResponse(200, headers={"ETag": etag})


def _finalize_response() -> _FakeResponse:
    return _FakeResponse(200, json_body={})


def _poll_response(status: str) -> _FakeResponse:
    return _FakeResponse(200, json_body={"value": {"status": status}})


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings with a pinned LinkedIn-Version."""
    get_settings.cache_clear()
    return get_settings().model_copy(update={"li_version": _VERSION})


@pytest.fixture
def clock() -> list[float]:
    """Collector for the injected fake sleep — records every requested wait."""
    return []


def _make_uploader(
    settings: Settings, client: _FakeClient, clock: list[float]
) -> LinkedInVideoUploader:
    """Wire an uploader to the fake client + a fake sleep that records durations."""
    return LinkedInVideoUploader(
        settings=settings,
        http_client=client,  # type: ignore[arg-type]  # duck-typed httpx stand-in
        sleep=clock.append,
    )


# --- (1) happy path ---------------------------------------------------------


def test_upload_happy_path_returns_available_result_with_ordered_etags(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: init → two part PUTs (etag-0, etag-1) → finalize → AVAILABLE poll.
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [
            _init_response(),
            _finalize_response(),
        ],
        ("PUT", _PART_URL_0): [_part_ok("etag-0")],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
        (
            "GET",
            f"https://api.linkedin.com/rest/videos/{_encoded(_VIDEO_URN)}",
        ): [_poll_response("AVAILABLE")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act: run the full chunked upload over an 8-byte file (two 4-byte parts).
    result = uploader.upload(_TOKEN, _MEMBER, b"01234567")

    # Assert: AVAILABLE result carrying the video URN.
    assert result == UploadResult(video_urn=_VIDEO_URN, status="AVAILABLE")


def test_upload_sends_etags_to_finalize_in_upload_order(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: same happy-path script.
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [
            _init_response(),
            _finalize_response(),
        ],
        ("PUT", _PART_URL_0): [_part_ok("etag-0")],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
        (
            "GET",
            f"https://api.linkedin.com/rest/videos/{_encoded(_VIDEO_URN)}",
        ): [_poll_response("AVAILABLE")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act.
    uploader.upload(_TOKEN, _MEMBER, b"01234567")

    # Assert: finalize received the ETags in the SAME order the parts were sent.
    finalize_call = _finalize_call(client)
    part_ids = finalize_call["json"]["finalizeUploadRequest"]["uploadedPartIds"]
    assert part_ids == ["etag-0", "etag-1"]


def test_upload_slices_each_part_by_inclusive_byte_range(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: happy path; we inspect the exact bytes PUT per part.
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [
            _init_response(),
            _finalize_response(),
        ],
        ("PUT", _PART_URL_0): [_part_ok("etag-0")],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
        (
            "GET",
            f"https://api.linkedin.com/rest/videos/{_encoded(_VIDEO_URN)}",
        ): [_poll_response("AVAILABLE")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act.
    uploader.upload(_TOKEN, _MEMBER, b"01234567")

    # Assert: part 0 is bytes [0..3] inclusive — the first four bytes.
    put0 = next(c for c in client.calls if c["url"] == _PART_URL_0)
    assert put0["content"] == b"0123"


def test_upload_carries_version_and_protocol_headers_on_initialize(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: happy path.
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [
            _init_response(),
            _finalize_response(),
        ],
        ("PUT", _PART_URL_0): [_part_ok("etag-0")],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
        (
            "GET",
            f"https://api.linkedin.com/rest/videos/{_encoded(_VIDEO_URN)}",
        ): [_poll_response("AVAILABLE")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act.
    uploader.upload(_TOKEN, _MEMBER, b"01234567")

    # Assert: the mandatory version/protocol headers ride the initialize call.
    init_call = client.calls[0]
    assert init_call["headers"]["LinkedIn-Version"] == _VERSION
    assert init_call["headers"]["X-Restli-Protocol-Version"] == "2.0.0"


# --- (2) PROCESSING_FAILED poll --------------------------------------------


def test_upload_raises_on_processing_failed_poll(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: everything succeeds until LinkedIn reports PROCESSING_FAILED.
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [
            _init_response(),
            _finalize_response(),
        ],
        ("PUT", _PART_URL_0): [_part_ok("etag-0")],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
        (
            "GET",
            f"https://api.linkedin.com/rest/videos/{_encoded(_VIDEO_URN)}",
        ): [_poll_response("PROCESSING_FAILED")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act + Assert: fail-closed — a failed transcode raises the typed error.
    with pytest.raises(VideoUploadError):
        uploader.upload(_TOKEN, _MEMBER, b"01234567")


def test_upload_polls_until_available_after_processing(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: first poll PROCESSING, second AVAILABLE (exercises the poll loop).
    poll_key = ("GET", f"https://api.linkedin.com/rest/videos/{_encoded(_VIDEO_URN)}")
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [
            _init_response(),
            _finalize_response(),
        ],
        ("PUT", _PART_URL_0): [_part_ok("etag-0")],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
        poll_key: [_poll_response("PROCESSING"), _poll_response("AVAILABLE")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act.
    result = uploader.upload(_TOKEN, _MEMBER, b"01234567")

    # Assert: it waited once (the injected fake clock recorded a poll interval).
    assert result.status == "AVAILABLE"
    assert len(clock) == 1


# --- (3) per-part retry, no whole-file restart -----------------------------


def test_part_put_that_500s_once_is_retried_without_restarting_upload(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: part 0 500s once then 200s; part 1 succeeds immediately.
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [
            _init_response(),
            _finalize_response(),
        ],
        ("PUT", _PART_URL_0): [_FakeResponse(500), _part_ok("etag-0")],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
        (
            "GET",
            f"https://api.linkedin.com/rest/videos/{_encoded(_VIDEO_URN)}",
        ): [_poll_response("AVAILABLE")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act.
    result = uploader.upload(_TOKEN, _MEMBER, b"01234567")

    # Assert: succeeded, and part 0 was retried EXACTLY twice while part 1 ran once
    # (proving the retry re-sent only the failed part, not the whole file).
    put0_count = sum(1 for c in client.calls if c["url"] == _PART_URL_0)
    put1_count = sum(1 for c in client.calls if c["url"] == _PART_URL_1)
    assert result.status == "AVAILABLE"
    assert (put0_count, put1_count) == (2, 1)


def test_part_retry_backs_off_using_injected_sleep(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: one 500 forces exactly one backoff before the retry succeeds.
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [
            _init_response(),
            _finalize_response(),
        ],
        ("PUT", _PART_URL_0): [_FakeResponse(500), _part_ok("etag-0")],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
        (
            "GET",
            f"https://api.linkedin.com/rest/videos/{_encoded(_VIDEO_URN)}",
        ): [_poll_response("AVAILABLE")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act.
    uploader.upload(_TOKEN, _MEMBER, b"01234567")

    # Assert: the first backoff used the base interval (attempt 0 → base * 2**0).
    assert clock[0] == 1.0


def test_finalize_omitted_when_a_part_permanently_fails(
    settings: Settings, clock: list[float]
) -> None:
    # Arrange: part 0 returns a non-retryable 400 — the upload must abort.
    script = {
        ("POST", "https://api.linkedin.com/rest/videos"): [_init_response()],
        ("PUT", _PART_URL_0): [_FakeResponse(400)],
        ("PUT", _PART_URL_1): [_part_ok("etag-1")],
    }
    client = _FakeClient(script)
    uploader = _make_uploader(settings, client, clock)

    # Act + Assert: a 4xx part is not retried and aborts the whole flow.
    with pytest.raises(VideoUploadError):
        uploader.upload(_TOKEN, _MEMBER, b"01234567")
    # No finalize POST was attempted (only the single initialize POST fired).
    post_calls = [c for c in client.calls if c["method"] == "POST"]
    assert len(post_calls) == 1


# --- helpers ----------------------------------------------------------------


def _encoded(urn: str) -> str:
    """URL-encode a URN the same way the uploader does for the poll path."""
    from urllib.parse import quote

    return quote(urn, safe="")


def _finalize_call(client: _FakeClient) -> dict[str, Any]:
    """Return the recorded finalizeUpload POST (the one carrying uploadedPartIds)."""
    return next(
        c
        for c in client.calls
        if c["method"] == "POST"
        and c.get("params", {}).get("action") == "finalizeUpload"
    )
