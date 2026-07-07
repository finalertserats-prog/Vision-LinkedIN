"""External health canary for Project VISION (BRD §17, NFR-08).

WHY this module exists: a service is only as observable as the thing watching it.
:mod:`vision.ops.health` lets VISION report its own readiness, but a self-report
is worthless if the process is wedged or the port is dead. This module is the
*external* watcher: a tiny prober that pings ``/healthz`` from outside and alerts
the owner when the answer is anything other than "healthy".

Reuse-the-FinalAlert-pattern: this mirrors finalert's oneshot ``*_canary.py`` +
systemd-timer design — a stateless one-shot that a timer runs on a fixed interval,
which *alerts but never fixes* (it does not attempt a restart; that is the
service manager's job via ``Restart=always``). It follows the same failure
posture: read a signal, and on breach send an email and exit non-zero so the
timer/monitoring surface records the failure.

SECURITY (§22 / threat model): the canary makes an unauthenticated GET to a local
health URL only; it sends no credentials and logs no secrets. Alert emails carry
only a non-secret status line and HTTP code.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from vision.config import Settings, VisionEnv, get_settings
from vision.logging_setup import configure_logging, get_logger
from vision.mailer.sender import get_sender

_log = get_logger("vision.ops.canary")

# Where the canary looks for the health endpoint. Env-overridable (config over
# code) so the same code probes localhost in a self-hosted deploy or a private
# address behind a reverse proxy without a change.
_DEFAULT_HEALTHZ_URL = "http://127.0.0.1:8000/healthz"
_HEALTHZ_URL_ENV = "VISION_HEALTHZ_URL"

# A short timeout: a health probe that hangs is itself a failure signal, so we
# bound it tightly rather than let the canary block a timer slot.
_DEFAULT_TIMEOUT_SECS = 10.0

# The HTTP getter is injectable so tests can supply a fake and never touch the
# network; production uses ``httpx.get``.
HttpGet = Callable[..., httpx.Response]


@dataclass(frozen=True)
class CanaryResult:
    """The verdict of one canary probe (immutable).

    ``ok`` is the pass/fail the caller branches on; ``status_code`` is the HTTP
    code seen (``None`` on a transport failure); ``detail`` is a short, non-secret
    human string for logs and the alert body.
    """

    ok: bool
    status_code: int | None
    detail: str


def canary(
    url: str,
    *,
    http_get: HttpGet = httpx.get,
    timeout: float = _DEFAULT_TIMEOUT_SECS,
) -> CanaryResult:
    """Ping ``/healthz`` and return a pass/fail :class:`CanaryResult`.

    A pass is strictly HTTP 200 (the readiness contract of
    :func:`vision.ops.health.build_health_router` — 200 ready, 503 not-ready). A
    non-200 response is a fail carrying the code; a transport error (connection
    refused, DNS, timeout) is a fail with ``status_code=None`` — the service being
    unreachable is precisely what the canary exists to catch. It never raises, so
    a probe failure is data, not a crash (fail-closed).
    """
    try:
        response = http_get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        # Transport-level failure: the service is down/unreachable. Log the class
        # only (never the URL args, which are non-secret but noisy), report fail.
        _log.error("canary transport error reaching healthz: %s", type(exc).__name__)
        return CanaryResult(
            ok=False, status_code=None, detail=f"transport error: {type(exc).__name__}"
        )

    code = response.status_code
    if code == 200:
        return CanaryResult(ok=True, status_code=code, detail="healthz reported healthy")
    _log.warning("canary healthz returned HTTP %s", code)
    return CanaryResult(ok=False, status_code=code, detail=f"healthz returned HTTP {code}")


def _send_failure_alert(settings: Settings, result: CanaryResult) -> None:
    """Email the owner that the health canary failed (non-secret content only).

    Send failures are swallowed (logged) so a mail outage cannot crash the canary
    — the non-zero exit code still surfaces the failure to the timer/monitoring.
    """
    subject = "VISION: health canary FAILED"
    body = (
        "The VISION health canary could not confirm a healthy service.\n\n"
        f"Detail: {result.detail}\n"
        f"HTTP status: {result.status_code}\n\n"
        "The service manager should restart the process automatically "
        "(Restart=always); investigate if this alert repeats."
    )
    html = (
        "<p>The VISION health canary could not confirm a healthy service.</p>"
        f"<p><b>Detail:</b> {result.detail}<br><b>HTTP status:</b> {result.status_code}</p>"
        "<p>The service manager should restart the process automatically "
        "(<code>Restart=always</code>); investigate if this alert repeats.</p>"
    )
    try:
        delivered = get_sender(settings).send(subject, body, html, to=settings.email_to)
    except Exception:  # noqa: BLE001 — last-resort guard around a 3rd-party sender
        _log.exception("canary alert send raised")
        return
    if not delivered:
        _log.error("canary alert could not be delivered")


def main() -> int:
    """``vision-canary`` console entry point (systemd-timer/cron oneshot, §17).

    Probes the configured health URL; on a healthy result logs and exits ``0``.
    On failure it logs, sends a re-auth-style ops alert (unless in ``dry_run``,
    which must stay side-effect free — no email — matching every other VISION
    job), and exits ``1`` so the timer/monitoring records the breach.
    """
    configure_logging()
    settings = get_settings()
    url = os.environ.get(_HEALTHZ_URL_ENV, _DEFAULT_HEALTHZ_URL)
    _log.info("vision-canary probing health endpoint")

    result = canary(url)
    if result.ok:
        _log.info("canary pass: %s", result.detail)
        return 0

    _log.error("canary FAIL: %s", result.detail)
    # dry_run stays fully side-effect free (no email), consistent with the token
    # and daily jobs; the non-zero exit still records the failure locally.
    if settings.vision_env is VisionEnv.DRY_RUN:
        _log.info("dry_run: canary alert suppressed")
        return 1

    _send_failure_alert(settings, result)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
