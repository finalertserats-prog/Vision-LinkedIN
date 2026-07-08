"""Email delivery providers behind one small interface (BRD §14, D4).

WHY a provider abstraction: BRD D4 chose a *transactional provider* (Resend/
Postmark/SES) for deliverability so Approve-links never land in spam, but the
owner can also fall back to plain Gmail SMTP with an App Password. The rest of
VISION should not care which is wired — it composes ``(subject, text, html)``
(see ``composer.py``) and calls one ``send``. This module supplies:

  * :class:`EmailSender`  — the ``Protocol`` every provider satisfies.
  * :class:`SMTPSender`   — STARTTLS (587) / SSL (465) SMTP, Gmail App Password,
                            multipart plain+HTML, adapted from finalert's
                            ``EmailAlerter`` with its error-specific handling.
  * :class:`ResendSender` — the Resend HTTP API via ``httpx``.
  * :func:`get_sender`    — factory selecting a provider from ``settings``.

SECURITY (§22, threat model §4): the SMTP password and the Resend API key come
from :class:`Settings` and are NEVER written to a log line, an exception message
we emit, or the returned value. Errors are logged generically.
"""

from __future__ import annotations

import logging
import os
import smtplib
import base64
import socket
import ssl
from collections.abc import Sequence
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Protocol, runtime_checkable

import httpx

from vision.config import Settings

log = logging.getLogger(__name__)

# One inline image = (content_id, raw_bytes, mime_subtype). A plain tuple (not a
# composer type) keeps the sender decoupled from the mailer's composer module.
InlineImagePart = tuple[str, bytes, str]

# Sensible defaults for the SMTP path. Gmail is the documented owner setup
# (App Password over STARTTLS:587); host/port may be overridden via env for
# Outlook/Hostinger without a code change (config over code). These are NOT
# secrets, so an env fallback here is acceptable where Settings has no field.
_DEFAULT_SMTP_HOST = "smtp.gmail.com"
_DEFAULT_SMTP_PORT = 587
_SMTP_TIMEOUT_SECS = 20

# Resend transactional-email endpoint. Pinned so provider drift is explicit.
_RESEND_ENDPOINT = "https://api.resend.com/emails"
_RESEND_TIMEOUT_SECS = 15


@runtime_checkable
class EmailSender(Protocol):
    """The one method every provider implements.

    Returns ``True`` on accepted delivery, ``False`` on a handled failure — the
    caller (daily job) decides whether to alert; a provider never raises for an
    ordinary send failure so one bad send can't crash the pipeline (§14.5).
    """

    def send(
        self,
        subject: str,
        text: str,
        html: str,
        to: str | None = None,
        inline_images: Sequence[InlineImagePart] | None = None,
    ) -> bool:
        """Send a multipart plain+HTML email; ``to`` overrides the configured
        recipient; ``inline_images`` are attached as related cid: parts."""
        ...


def _build_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    text: str,
    html: str,
    inline_images: Sequence[InlineImagePart] | None = None,
) -> MIMEMultipart:
    """Assemble the message: ``multipart/alternative`` (plain→HTML), wrapped in a
    ``multipart/related`` when inline CID images are supplied.

    WHY the alternative order: clients render the LAST acceptable part, so HTML
    clients show the rich version and plain-text clients fall back to ``text``.
    WHY related-wrap for images: an ``<img src="cid:...">`` only resolves against a
    sibling image part inside a ``multipart/related`` — which Gmail renders (unlike
    a ``data:`` URI). Pure helper so providers + tests build identical MIME.
    """
    alternative = MIMEMultipart("alternative")
    # Attach in fallback→preferred order (RFC 2046 §5.1.4).
    alternative.attach(MIMEText(text, "plain", "utf-8"))
    alternative.attach(MIMEText(html, "html", "utf-8"))

    if not inline_images:
        alternative["Subject"] = subject
        alternative["From"] = from_addr
        alternative["To"] = to_addr
        return alternative

    root = MIMEMultipart("related")
    root["Subject"] = subject
    root["From"] = from_addr
    root["To"] = to_addr
    root.attach(alternative)
    for cid, data, subtype in inline_images:
        part = MIMEImage(data, _subtype=subtype)
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("Content-Disposition", "inline", filename=f"{cid}.{subtype}")
        root.attach(part)
    return root


class SMTPSender:
    """SMTP provider — STARTTLS:587 or SSL:465, Gmail App Password (adapted from finalert).

    The credential (``password``) is held only in memory and never logged. A
    blank ``password`` disables the sender (``send`` returns ``False`` after a
    single info log), matching finalert's "not configured → skip" behaviour so a
    dev checkout without secrets degrades safely instead of erroring.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_addr: str,
        default_to: str,
        timeout: int = _SMTP_TIMEOUT_SECS,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password  # secret — never logged
        self._from = from_addr
        self._default_to = default_to
        self._timeout = timeout
        # A sender with no credential is inert; surface it once at DEBUG (no secret).
        self._enabled = bool(password and from_addr)
        if not self._enabled:
            log.debug("SMTPSender not configured (missing password/from); sends will be skipped.")

    def send(
        self,
        subject: str,
        text: str,
        html: str,
        to: str | None = None,
        inline_images: Sequence[InlineImagePart] | None = None,
    ) -> bool:
        """Deliver ``subject``/``text``/``html`` over SMTP; return success as a bool.

        ``inline_images`` are attached as related cid: parts so an HTML preview
        renders in Gmail. Error handling is *specific* (§22): auth, protocol,
        timeout and network faults are caught separately and logged generically.
        """
        if not self._enabled:
            log.info("SMTP send skipped: sender not configured.")
            return False

        recipient = (to or self._default_to).strip()
        if not recipient:
            log.warning("SMTP send skipped: no recipient address.")
            return False

        message = _build_message(self._from, recipient, subject, text, html, inline_images)

        try:
            if self._port == 465:
                # Implicit TLS from the first byte (SSL on connect).
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self._host, self._port, context=context, timeout=self._timeout) as server:
                    server.login(self._username or self._from, self._password)
                    server.sendmail(self._from, [recipient], message.as_string())
            else:
                # Opportunistic TLS: connect plain, then upgrade with STARTTLS.
                with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
                    server.ehlo()
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                    server.login(self._username or self._from, self._password)
                    server.sendmail(self._from, [recipient], message.as_string())
        except smtplib.SMTPAuthenticationError:
            # Most common misconfig: a Gmail main password used instead of an App
            # Password. Guide the owner WITHOUT echoing any credential.
            log.error("SMTP authentication failed — check the App Password (not the account password).")
            return False
        except smtplib.SMTPException as exc:
            log.error("SMTP protocol error while sending mail: %s", exc.__class__.__name__)
            return False
        except socket.timeout:
            log.error("SMTP send timed out after %ss.", self._timeout)
            return False
        except OSError as exc:
            # Network-level failure (DNS, refused, reset). Log the class, not args
            # (args can contain host detail but never the secret).
            log.error("SMTP network error: %s", exc.__class__.__name__)
            return False

        log.info("Email sent via SMTP: %r", subject)
        return True


class ResendSender:
    """Transactional provider — the Resend HTTP API over ``httpx``.

    The API key is sent only in the ``Authorization`` header and is never logged.
    A non-2xx response is a handled failure (returns ``False``); a transport
    error is caught so a provider outage cannot crash the daily job (§14.5).
    """

    def __init__(
        self,
        *,
        api_key: str,
        from_addr: str,
        default_to: str,
        endpoint: str = _RESEND_ENDPOINT,
        timeout: int = _RESEND_TIMEOUT_SECS,
    ) -> None:
        self._api_key = api_key  # secret — only ever placed in the auth header
        self._from = from_addr
        self._default_to = default_to
        self._endpoint = endpoint
        self._timeout = timeout
        self._enabled = bool(api_key and from_addr)
        if not self._enabled:
            log.debug("ResendSender not configured (missing api key/from); sends will be skipped.")

    def send(
        self,
        subject: str,
        text: str,
        html: str,
        to: str | None = None,
        inline_images: Sequence[InlineImagePart] | None = None,
    ) -> bool:
        """POST the email to Resend; return whether it was accepted (2xx)."""
        if not self._enabled:
            log.info("Resend send skipped: sender not configured.")
            return False

        recipient = (to or self._default_to).strip()
        if not recipient:
            log.warning("Resend send skipped: no recipient address.")
            return False

        # Resend accepts both text and html; sending both preserves the plain
        # fallback exactly as the SMTP path does.
        payload: dict[str, object] = {
            "from": self._from,
            "to": [recipient],
            "subject": subject,
            "html": html,
            "text": text,
        }
        if inline_images:
            # Resend inlines an attachment when it carries a content_id matching the
            # HTML's cid: reference.
            payload["attachments"] = [
                {
                    "filename": f"{cid}.{subtype}",
                    "content": base64.b64encode(data).decode("ascii"),
                    "content_id": f"<{cid}>",
                }
                for cid, data, subtype in inline_images
            ]
        headers = {
            # The key lives here and ONLY here — never in a log line.
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(self._endpoint, json=payload, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            # Transport-level failure (DNS/connect/timeout). Class only, no key.
            log.error("Resend transport error: %s", exc.__class__.__name__)
            return False

        if response.is_success:
            log.info("Email sent via Resend: %r", subject)
            return True

        # Log the status but not the body — a provider error body could echo the
        # recipient or headers we would rather not persist.
        log.error("Resend rejected the send with HTTP %s.", response.status_code)
        return False


def get_sender(settings: Settings) -> EmailSender:
    """Return the configured :class:`EmailSender` for ``settings.email_provider``.

    Selection is by ``EMAIL_PROVIDER`` (``smtp`` | ``resend``); an unknown value
    fails loudly (``ValueError``) rather than silently defaulting, so a typo in
    config surfaces at startup instead of quietly not sending (fail-closed, §22).

    Credentials are read from ``settings`` (``email_api_key`` is the SMTP App
    Password for the SMTP path and the API key for the Resend path). SMTP host /
    port — which are not secrets and have no ``Settings`` field — may be overridden
    via ``SMTP_HOST`` / ``SMTP_PORT`` env, defaulting to Gmail's STARTTLS:587.
    """
    provider = settings.email_provider.strip().lower()

    if provider == "smtp":
        # Non-secret transport knobs from env; Gmail STARTTLS defaults otherwise.
        host = os.environ.get("SMTP_HOST", _DEFAULT_SMTP_HOST).strip()
        try:
            port = int(os.environ.get("SMTP_PORT", str(_DEFAULT_SMTP_PORT)))
        except ValueError:
            # A non-integer SMTP_PORT is a config error; fail closed rather than
            # guessing a port and mis-connecting.
            raise ValueError("SMTP_PORT must be an integer (e.g. 587 or 465)")
        return SMTPSender(
            host=host,
            port=port,
            username=settings.email_from,
            password=settings.email_api_key,
            from_addr=settings.email_from,
            default_to=settings.email_to,
        )

    if provider == "resend":
        return ResendSender(
            api_key=settings.email_api_key,
            from_addr=settings.email_from,
            default_to=settings.email_to,
        )

    raise ValueError(f"unknown EMAIL_PROVIDER {settings.email_provider!r}; expected 'smtp' or 'resend'")
