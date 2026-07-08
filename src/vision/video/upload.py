"""LinkedInVideoUploader — chunked /rest/videos upload flow (BRD §15, video lane).

WHY this module exists: LinkedIn videos are NOT a single blob PUT like images —
they use a four-phase, multipart protocol (initialize → upload-parts → finalize →
poll) that VISION must drive to completion before a reel can be attached to a
post. This module is the isolated boundary for that protocol, mirroring the design
of :mod:`vision.publish.linkedin` (injected ``httpx.Client``, centralised
``LinkedIn-Version`` + ``X-Restli-Protocol-Version`` headers, a ``_request``
transport guard, the §15.4 typed-error matrix, and the "never log tokens/signed
URLs" rule).

The upload is modelled as a small RESUMABLE state machine: each part carries a
first/last byte range and its own ``uploadUrl``, and each successful PUT yields an
ETag. The ETags are collected **in order** and replayed to ``finalizeUpload`` in
the SAME order — LinkedIn reassembles the file from that ordered part list, so a
reorder corrupts the video. A per-part failure retries ONLY that part (never the
whole file), because each ``uploadUrl`` is independently addressable.

GUARDRAILS: the opaque ``uploadToken``, the bearer access token and the signed
per-part ``uploadUrl``s are secrets — they are NEVER logged. Processing is
fail-closed: a ``PROCESSING_FAILED`` status raises :class:`VideoUploadError`
rather than returning a half-baked URN.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from vision.config import Settings, get_settings

from .schema import UploadResult

_log = logging.getLogger(__name__)

# --- Endpoint constants -----------------------------------------------------
# Pinned in one place so the LinkedIn video surface is auditable and a host change
# is a single edit (mirrors linkedin.py's approach).
_API_BASE = "https://api.linkedin.com"
_VIDEOS_URL = f"{_API_BASE}/rest/videos"

# Fixed Rest.li protocol contract required on every REST call (§6); never changes.
_RESTLI_VERSION = "2.0.0"

# Conservative network timeout so a hung LinkedIn socket cannot stall the video
# worker indefinitely (NFR-07). Applied only to the default client we build.
_TIMEOUT = httpx.Timeout(60.0)

# Retry/backoff policy for a single part PUT and for the availability poll. Kept
# as named constants (not magic numbers) so the cadence is tunable in one place.
_MAX_PART_ATTEMPTS = 5
_PART_BACKOFF_BASE_SECONDS = 1.0
_POLL_INTERVAL_SECONDS = 5.0
_POLL_DEADLINE_SECONDS = 600.0

# LinkedIn processing status strings we branch on (§ video lifecycle).
_STATUS_AVAILABLE = "AVAILABLE"
_STATUS_FAILED = "PROCESSING_FAILED"


class VideoUploadError(Exception):
    """Terminal failure of the video upload flow (fail-closed, BRD §15.4).

    WHY a dedicated type: the video lane must distinguish "LinkedIn permanently
    rejected/failed to process this asset" from the transient/retryable HTTP
    errors already modelled in :mod:`vision.publish.errors`. Callers catch this to
    dead-letter the reel + alert, never to blind-retry.
    """


@dataclass(frozen=True)
class _UploadPart:
    """One resumable unit of the chunked upload: an inclusive byte range + its URL.

    Frozen so a part descriptor cannot be mutated mid-flight (immutability §22).
    ``first_byte``/``last_byte`` are INCLUSIVE (LinkedIn's convention), so the
    slice is ``file[first_byte:last_byte + 1]``.
    """

    first_byte: int
    last_byte: int
    upload_url: str


@dataclass
class _UploadState:
    """Mutable progress of the state machine — the resume point on a part retry.

    WHY a state object: it makes the "retry one part, never restart" invariant
    explicit — ``etags`` grows one entry per completed part, and its length is the
    index of the next part still to upload. Nothing here is logged (the parts hold
    signed URLs).
    """

    video_urn: str
    upload_token: str
    parts: Sequence[_UploadPart]
    etags: list[str] = field(default_factory=list)


class LinkedInVideoUploader:
    """Drives LinkedIn's chunked ``/rest/videos`` upload to an AVAILABLE URN.

    Construction is cheap and side-effect free. Both the ``httpx.Client`` and the
    ``sleep`` function are injectable purely so tests are hermetic — no real
    network, no real wall-clock waits. Production passes nothing and gets a real
    client + ``time.sleep``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        # One validated settings source (§22) for ``li_version``; tests inject their
        # own so the header value is deterministic.
        self._settings = settings or get_settings()
        # Reuse one HTTP client for keep-alive; tests inject a mock-backed one.
        self._http = http_client or httpx.Client(timeout=_TIMEOUT)
        # Injected clock: tests pass a fake to skip real backoff/poll waits.
        self._sleep = sleep or time.sleep

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release the underlying HTTP connection pool (mirrors LinkedInClient)."""
        self._http.close()

    def __enter__(self) -> LinkedInVideoUploader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- header assembly ----------------------------------------------------

    def _auth_headers(
        self, access_token: str, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        """Build the three mandatory LinkedIn headers (§6) plus any extras.

        Centralised so every authenticated call is guaranteed the version +
        protocol headers — a missing ``LinkedIn-Version`` silently 400s/426s. The
        version comes from ``settings.li_version`` so API drift is one config edit.
        """
        headers = {
            # Bearer token; the logging redaction filter masks it — never logged here.
            "Authorization": f"Bearer {access_token}",
            "LinkedIn-Version": self._settings.li_version,
            "X-Restli-Protocol-Version": _RESTLI_VERSION,
        }
        if extra:
            # New dict (immutability §22) — never mutate a shared base.
            headers = {**headers, **extra}
        return headers

    # -- network guard ------------------------------------------------------

    def _request(
        self, method: str, url: str, *, context: str, **kwargs: Any
    ) -> httpx.Response:
        """Issue one HTTP request, mapping raw transport failures to a typed error.

        WHY: a hung socket / dropped connection is an UNKNOWN outcome on a video
        write; surfaced as :class:`VideoUploadError` so the worker dead-letters
        rather than crashing on a raw httpx exception. Never logs ``url`` (it may be
        a signed per-part upload URL).
        """
        try:
            return self._http.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Only the error class + context travel into the message — no URL/token.
            raise VideoUploadError(
                f"{context}: network error ({exc.__class__.__name__})"
            ) from exc

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """A part PUT is safe to retry on throttling (429) or LinkedIn-side 5xx.

        4xx (except 429) is a request-shape bug on our side — retrying can't fix it,
        so it is NOT retryable and fails fast.
        """
        return status_code == 429 or status_code >= 500

    # -- public entry point -------------------------------------------------

    def upload(
        self, access_token: str, member_urn: str, video_bytes: bytes
    ) -> UploadResult:
        """Upload ``video_bytes`` and return the URN once LinkedIn says AVAILABLE.

        Runs the full four-phase machine: initialize → upload every part (each
        retried independently) → finalize with the ORDERED ETags → poll to a
        terminal status. Fail-closed: ``PROCESSING_FAILED`` raises, never returns.
        """
        state = self._initialize(access_token, member_urn, len(video_bytes))
        self._upload_all_parts(access_token, state, video_bytes)
        self._finalize(access_token, state)
        return self._poll_until_available(access_token, state.video_urn)

    # -- phase 1: initialize ------------------------------------------------

    def _initialize(
        self, access_token: str, member_urn: str, file_size_bytes: int
    ) -> _UploadState:
        """POST ?action=initializeUpload → the video URN, upload token + part plan.

        LinkedIn returns the durable video URN, an opaque ``uploadToken`` (SECRET —
        never logged) and an ordered ``uploadInstructions`` list, each with an
        inclusive byte range and a signed per-part ``uploadUrl``. We freeze that
        into the state machine's part plan.
        """
        response = self._request(
            "POST",
            _VIDEOS_URL,
            context="video_upload.initialize",
            params={"action": "initializeUpload"},
            headers=self._auth_headers(
                access_token, {"Content-Type": "application/json"}
            ),
            json={
                "initializeUploadRequest": {
                    "owner": member_urn,
                    "fileSizeBytes": file_size_bytes,
                    # v1 uploads the raw reel only — captions/thumbnail are later.
                    "uploadCaptions": False,
                    "uploadThumbnail": False,
                }
            },
        )
        self._raise_for_status(response, "video_upload.initialize")

        # LinkedIn nests the handshake under ``value``.
        value = response.json()["value"]
        parts = _parse_upload_instructions(value["uploadInstructions"])
        return _UploadState(
            video_urn=value["video"],
            upload_token=value["uploadToken"],
            parts=parts,
        )

    # -- phase 2: upload parts ----------------------------------------------

    def _upload_all_parts(
        self, access_token: str, state: _UploadState, video_bytes: bytes
    ) -> None:
        """PUT every part in order, collecting one ETag per part into ``state``.

        The resume invariant: ``len(state.etags)`` is the index of the next part to
        send, so a caller could restart mid-list without re-sending completed parts.
        ETag order is preserved because parts are walked in list order and appended.
        """
        # Only walk parts not yet done — keeps the "never restart the whole file"
        # guarantee even if this is re-entered after a partial run.
        for index in range(len(state.etags), len(state.parts)):
            part = state.parts[index]
            # Inclusive range → +1 on the upper bound for Python's half-open slice.
            chunk = video_bytes[part.first_byte : part.last_byte + 1]
            etag = self._upload_one_part(access_token, part, chunk, index)
            # Append preserves order — the finalize contract depends on this.
            state.etags.append(etag)

    def _upload_one_part(
        self, access_token: str, part: _UploadPart, chunk: bytes, index: int
    ) -> str:
        """PUT a single part with exponential backoff on 429/5xx; return its ETag.

        WHY retry HERE and not restart: each part has its own addressable
        ``uploadUrl``, so a transient failure on part N is recovered by re-PUTting
        ONLY part N — the already-uploaded parts stay valid. The ETag header is the
        proof-of-part LinkedIn wants back at finalize.
        """
        last_error: Exception | None = None
        for attempt in range(_MAX_PART_ATTEMPTS):
            response = self._request(
                "PUT",
                part.upload_url,
                context=f"video_upload.part[{index}]",
                content=chunk,
                # The blob sink takes only the bearer token, not version/protocol.
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code < 400:
                etag = _extract_etag(response, index)
                # Log progress by INDEX only — never the signed part URL or token.
                _log.debug("video part %d uploaded", index)
                return etag

            if not self._is_retryable_status(response.status_code):
                # Non-retryable (e.g. 4xx request bug) — fail fast, no backoff loop.
                raise VideoUploadError(
                    f"video_upload.part[{index}]: non-retryable status "
                    f"{response.status_code}"
                )

            # Retryable: remember why, back off, and try the SAME part again.
            last_error = VideoUploadError(
                f"video_upload.part[{index}]: status {response.status_code}"
            )
            self._backoff(attempt)

        # Exhausted attempts on a retryable error → terminal for this upload.
        raise VideoUploadError(
            f"video_upload.part[{index}]: exhausted {_MAX_PART_ATTEMPTS} attempts"
        ) from last_error

    def _backoff(self, attempt: int) -> None:
        """Sleep an exponentially growing interval before the next part attempt.

        Uses the injected sleep so tests advance a fake clock instantly. Attempt 0
        waits the base, attempt 1 double, etc. — standard capped-attempt backoff.
        """
        self._sleep(_PART_BACKOFF_BASE_SECONDS * (2**attempt))

    # -- phase 3: finalize --------------------------------------------------

    def _finalize(self, access_token: str, state: _UploadState) -> None:
        """POST ?action=finalizeUpload with the ORDERED ETags to reassemble the file.

        The ``uploadedPartIds`` list MUST be in the same order the parts were sent —
        LinkedIn stitches the file from that sequence, so a reorder corrupts the
        video. ``state.etags`` is already ordered by construction; we pass it as-is.
        """
        response = self._request(
            "POST",
            _VIDEOS_URL,
            context="video_upload.finalize",
            params={"action": "finalizeUpload"},
            headers=self._auth_headers(
                access_token, {"Content-Type": "application/json"}
            ),
            json={
                "finalizeUploadRequest": {
                    "video": state.video_urn,
                    # Opaque token echoed back to bind the finalize to this upload.
                    "uploadToken": state.upload_token,
                    # ORDERED part ids — never sorted/reordered (see docstring).
                    "uploadedPartIds": list(state.etags),
                }
            },
        )
        self._raise_for_status(response, "video_upload.finalize")

    # -- phase 4: poll ------------------------------------------------------

    def _poll_until_available(self, access_token: str, video_urn: str) -> UploadResult:
        """GET the video until AVAILABLE (return) or PROCESSING_FAILED (raise).

        WHY a bounded poll: LinkedIn transcodes asynchronously after finalize, so
        the URN is not immediately usable. We poll with a fixed interval up to an
        overall deadline; a still-PROCESSING video that never terminates hits the
        deadline and fails-closed rather than looping forever.
        """
        # Track elapsed against a monotonic-ish budget driven by our own sleeps, so
        # the deadline is deterministic under the injected fake clock.
        elapsed = 0.0
        while True:
            status = self._fetch_status(access_token, video_urn)
            if status == _STATUS_AVAILABLE:
                # Terminal success — the URN can now be attached to a post.
                return UploadResult(video_urn=video_urn, status=_STATUS_AVAILABLE)
            if status == _STATUS_FAILED:
                # Fail-closed: never return a half-processed asset (§15.4).
                raise VideoUploadError(
                    f"video_upload.poll: LinkedIn reported {_STATUS_FAILED}"
                )

            if elapsed >= _POLL_DEADLINE_SECONDS:
                # Stuck in PROCESSING past the budget — treat as terminal failure.
                raise VideoUploadError(
                    "video_upload.poll: deadline exceeded while PROCESSING"
                )
            self._sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS

    def _fetch_status(self, access_token: str, video_urn: str) -> str:
        """GET one video status snapshot; return its ``status`` string.

        The URN contains ``:`` characters that must be URL-encoded so the path
        segment is well-formed (mirrors LinkedInClient.delete's ``quote``).
        """
        encoded = quote(video_urn, safe="")
        response = self._request(
            "GET",
            f"{_VIDEOS_URL}/{encoded}",
            context="video_upload.poll",
            headers=self._auth_headers(access_token),
        )
        self._raise_for_status(response, "video_upload.poll")
        # LinkedIn nests the resource under ``value``; ``status`` is the lifecycle.
        return response.json()["value"]["status"]

    # -- error mapping ------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        """Raise :class:`VideoUploadError` on any non-2xx JSON-API response.

        WHY simpler than linkedin.py's matrix: the JSON-API phases here
        (initialize/finalize/poll) are all safe to fail-closed on any error — the
        publisher does not branch per-status on them. The per-PART PUT path handles
        its own 429/5xx retry logic before reaching a terminal error.
        """
        if response.status_code < 400:
            return
        # Include a bounded body excerpt for debugging; the JSON-API body carries no
        # token/URL secrets (those live only on the part-PUT path we don't log).
        raise VideoUploadError(
            f"{context}: request failed ({response.status_code}): "
            f"{response.text[:500]}"
        )


# --- Module-level helpers ---------------------------------------------------
# Pure free functions (no ``self``) so they are trivially unit-testable and carry
# no client state (mirrors linkedin.py's helper style).


def _parse_upload_instructions(raw: Sequence[dict[str, Any]]) -> list[_UploadPart]:
    """Turn LinkedIn's ``uploadInstructions`` into ordered frozen part descriptors.

    Preserves list order (the reassembly order) and coerces the byte bounds to int
    so a JSON string (LinkedIn sometimes stringifies large longs) never breaks the
    slice arithmetic.
    """
    return [
        _UploadPart(
            first_byte=int(instruction["firstByte"]),
            last_byte=int(instruction["lastByte"]),
            upload_url=instruction["uploadUrl"],
        )
        for instruction in raw
    ]


def _extract_etag(response: httpx.Response, index: int) -> str:
    """Pull the ``ETag`` header proving a part was stored, or fail loudly.

    A part PUT that returns 2xx with no ETag is unusable — we could not name the
    part at finalize — so we raise rather than append an empty id that would
    silently corrupt the ordered ``uploadedPartIds`` list (fail-closed §22).
    """
    etag = response.headers.get("ETag")
    if not etag:
        raise VideoUploadError(
            f"video_upload.part[{index}]: 2xx response carried no ETag"
        )
    return etag
