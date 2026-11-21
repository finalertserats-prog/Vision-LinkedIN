"""One LinkedIn re-auth email per incident, carrying a click-to-fix link.

WHY: on 2026-11-21 an expired token made the 5-minute publisher email the owner
on every tick (dozens of identical emails). An incident is now announced once:
the first alert stamps the ``alert_state`` ledger and later ticks see the stamp
and stay quiet. Saving a new token clears the stamps (``clear_reauth_incidents``),
so the NEXT expiry is a new incident and gets its own single email.

The stamp is read and written through the CALLER's session, inside its
transaction. Production runs on SQLite, where a second connection writing while
the caller holds an uncommitted write fails with "database is locked"; the
fail-open dedup store would then let every tick email again.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from vision.config import Settings
from vision.db.models import AlertState
from vision.logging_setup import get_logger
from vision.mailer.sender import EmailSender
from vision.ops.alerts import AlertKind, Alerter
from vision.publish.reauth_link import REAUTH_LINK_TTL, build_reauth_url

_log = get_logger("vision.publish.reauth_alert")

SUBJECT_REAUTH_REQUIRED = "LinkedIn re-authorisation required"
SUBJECT_EXPIRES_SOON = "LinkedIn access expires soon"

# "Once per incident": far longer than a token's ~60-day life, so only a
# successful reconnect (which clears the stamp) re-arms the email.
REAUTH_INCIDENT_WINDOW = timedelta(days=365)

_KEY_PREFIX = f"{AlertKind.TOKEN_REAUTH_NEEDED.value}::"


class _SessionDedupStore:
    """Dedup ledger bound to the caller's session (see module docstring)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_last_fired(self, key: str) -> datetime | None:
        row = self._session.execute(
            select(AlertState).where(AlertState.dedup_key == key)
        ).scalar_one_or_none()
        if row is None:
            return None
        fired = row.last_fired_at
        return fired if fired.tzinfo else fired.replace(tzinfo=timezone.utc)

    def record_fired(self, key: str, at: datetime) -> None:
        row = self._session.execute(
            select(AlertState).where(AlertState.dedup_key == key)
        ).scalar_one_or_none()
        # Savepoint: if a concurrent run inserted the same key first, only this
        # write rolls back and the caller's session stays usable.
        with self._session.begin_nested():
            if row is None:
                self._session.add(AlertState(dedup_key=key, last_fired_at=at))
            else:
                row.last_fired_at = at


class _ReauthEmailChannel:
    """Email with a real button, since the whole point is one click to fix."""

    name = "email"

    def __init__(self, sender: EmailSender, link: str) -> None:
        self._sender = sender
        self._link = link

    def deliver(self, kind: AlertKind, subject: str, detail: str) -> bool:
        days = REAUTH_LINK_TTL.days
        text = (
            f"{detail}\n\nReconnect LinkedIn: {self._link}\n"
            f"(Click, then Allow on LinkedIn. The link works for {days} days.)\n\n"
            "Approved posts are kept and publish automatically once you reconnect. "
            "You will not get repeats of this email."
        )
        link = html.escape(self._link, quote=True)
        body = (
            f"<p>{html.escape(detail)}</p>"
            f'<p><a href="{link}" style="display:inline-block;padding:10px 18px;'
            "background:#0a66c2;color:#ffffff;border-radius:6px;text-decoration:none;"
            'font-weight:600">Reconnect LinkedIn</a></p>'
            f"<p>Click, then Allow on LinkedIn. The link works for {days} days.</p>"
            "<p>Approved posts are kept and publish automatically once you reconnect. "
            "You will not get repeats of this email.</p>"
        )
        return self._sender.send(f"VISION: {subject}", text, body)


def send_reauth_alert_once(
    session: Session,
    *,
    sender: EmailSender,
    settings: Settings,
    now: datetime,
    subject: str,
    reason: str,
) -> bool:
    """Email the owner about a LinkedIn re-auth incident unless already told.

    Returns True if the email was dispatched, False if this incident was already
    announced. Never raises: a broken mailer or ledger must not crash the job
    that is reporting the problem.
    """
    try:
        alerter = Alerter(
            channels=[_ReauthEmailChannel(sender, build_reauth_url(settings, now))],
            dedup_window=REAUTH_INCIDENT_WINDOW,
            now=lambda: now,
            store=_SessionDedupStore(session),
        )
        return alerter.alert(AlertKind.TOKEN_REAUTH_NEEDED, subject, reason)
    except Exception as exc:  # noqa: BLE001 - alerting must never crash its caller
        _log.error("reauth alert failed: %s", exc.__class__.__name__)
        return False


def clear_reauth_incidents(session: Session) -> None:
    """Forget announced re-auth incidents; call when a new token is stored."""
    session.execute(delete(AlertState).where(AlertState.dedup_key.startswith(_KEY_PREFIX)))
    session.flush()
