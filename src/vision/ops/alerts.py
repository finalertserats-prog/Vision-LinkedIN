"""Operational alerting for Project VISION (BRD §17, NFR-07/08).

WHY this module exists: the daily pipeline, the publisher, the token job and the
feed-health checker all need to *tell the owner something has gone wrong* — a
failed run, a stuck/dead-lettered post, an expired LinkedIn token, or a silent
feed — without each caller re-implementing "how do I notify?". This module is the
single, small notification seam:

  * :class:`AlertChannel`      — the ``Protocol`` every delivery channel satisfies.
  * :class:`EmailAlertChannel` — reuses :mod:`vision.mailer` (the existing
                                 ``EmailSender``) so alerts ride the SAME, already
                                 hardened email path the approval loop uses.
  * :class:`TelegramAlertChannel` — an OPTIONAL, config-gated push channel over
                                 ``httpx`` for out-of-band alerts (email itself can
                                 be the thing that is broken).
  * :class:`Alerter`           — fans an ``alert(kind, subject, detail)`` out to
                                 every configured channel, with DURABLE
                                 dedup/rate-limiting (last-fired state persisted in
                                 the ``alert_state`` table via an
                                 :class:`AlertDedupStore`) so one flapping or
                                 permanent fault does not spam the owner even though
                                 every cron tick is a fresh process.

SECURITY (threat model §4, NFR-05/§22): fail-closed everywhere — a channel that
cannot deliver returns ``False`` and NEVER raises into the caller's control flow
(an alert failure must not crash the pipeline it is trying to report on). Secrets
(the Telegram bot token, the SMTP/Resend credential) live only in config/env and
are NEVER written to a log line, an exception message, or an alert body.
"""

from __future__ import annotations

import logging
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from vision.config import Settings, get_settings
from vision.db.models import AlertState
from vision.db.session import get_session
from vision.mailer.sender import EmailSender, get_sender

_log = logging.getLogger(__name__)


class AlertKind(str, Enum):
    """The finite set of operational events VISION alerts on (BRD §17).

    A closed enum (not free strings) means a typo'd kind fails at construction
    instead of silently mis-routing, and gives every call site one stable
    vocabulary. Each value is the machine-readable dedup discriminator AND a
    human-readable tag in the alert subject.
    """

    DAILY_RUN_FAILURE = "daily_run_failure"  # the daily pipeline errored / produced nothing
    PUBLISH_FAILURE = "publish_failure"  # a publish failed (§15.4)
    TOKEN_REAUTH_NEEDED = "token_reauth_needed"  # LinkedIn re-authorisation required
    DEAD_FEED = "dead_feed"  # a source has been silent past its threshold (§17)
    DEAD_LETTER = "dead_letter"  # a draft was dead-lettered (terminal failure)


# Default dedup window: how long an identical (kind, subject) alert is suppressed
# after it first fires. Config-over-code (§22) — overridable via env without a code
# change — so a noisy incident can be throttled harder or a test can shorten it.
_DEFAULT_DEDUP_WINDOW = timedelta(hours=1)

# Telegram Bot API endpoint template. The bot token sits in the URL PATH (that is
# how Telegram's API is shaped), which is exactly why the URL is NEVER logged.
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_TIMEOUT_SECS = 10


@runtime_checkable
class AlertChannel(Protocol):
    """One delivery channel for an alert (email, Telegram, …).

    The contract mirrors the mailer's: ``deliver`` returns ``True`` on accepted
    delivery and ``False`` on a *handled* failure, and NEVER raises for an ordinary
    delivery problem — so the :class:`Alerter` can try every channel and a single
    dead channel can neither crash the caller nor stop the others.
    """

    #: A short, stable label used in logs to identify which channel acted.
    name: str

    def deliver(self, kind: AlertKind, subject: str, detail: str) -> bool:
        """Deliver one alert; return whether it was accepted."""
        ...


def _format_subject(kind: AlertKind, subject: str) -> str:
    """Prefix a subject with a ``[VISION <KIND>]`` tag for at-a-glance triage.

    Kept as a pure helper so every channel renders the SAME subject shape and the
    dedup key (which is computed from the raw kind+subject, not this rendering)
    stays independent of presentation.
    """
    return f"[VISION {kind.value}] {subject}"


class EmailAlertChannel:
    """Alert channel that reuses the existing :mod:`vision.mailer` email path.

    WHY reuse rather than a bespoke SMTP call: the mailer's ``EmailSender`` already
    encapsulates provider selection, credential handling (never logged), TLS, and
    the "return bool, never raise" contract (§14.5). An alert is just a plain-text
    email, so this channel builds a minimal text+HTML body and delegates the send.
    """

    name = "email"

    def __init__(self, sender: EmailSender) -> None:
        # The sender is injected (built from settings by :func:`build_alerter`, or a
        # mock in tests), so this channel needs no email config of its own.
        self._sender = sender

    def deliver(self, kind: AlertKind, subject: str, detail: str) -> bool:
        """Send the alert as a plain+HTML email; return the sender's success bool.

        No secret ever reaches this method — ``subject``/``detail`` are caller-built
        operational text — so the body is safe to send as-is. Any provider failure
        is already reduced to ``False`` by the sender, which we pass straight
        through (fail-closed: a dead mailer is a failed alert, not an exception).
        """
        full_subject = _format_subject(kind, subject)
        # A minimal, escaping-free body: callers pass operational strings, never
        # untrusted HTML, so no markup injection surface exists here.
        text = f"{subject}\n\n{detail}\n"
        html = f"<p><strong>{subject}</strong></p><p>{detail}</p>"
        return self._sender.send(full_subject, text, html)


class TelegramAlertChannel:
    """Optional out-of-band alert channel over the Telegram Bot API (``httpx``).

    WHY optional + config-gated: email is the primary channel, but email itself can
    be the failure being reported (SMTP down, provider outage). A Telegram push
    gives a second, independent path. It is only *enabled* when BOTH a bot token
    and a chat id are configured; otherwise :meth:`deliver` is an inert no-op that
    returns ``False`` — so an un-configured deployment silently skips it rather than
    erroring (fail-closed, and no partial config guesswork).

    SECURITY: the bot token is a secret held only in memory and placed ONLY in the
    request URL path — it is never logged, never put in an exception message, and
    never included in an alert body.
    """

    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout: int = _TELEGRAM_TIMEOUT_SECS,
    ) -> None:
        self._bot_token = bot_token  # secret — only ever placed in the URL path
        self._chat_id = chat_id
        self._timeout = timeout
        # A channel missing either half of its config is inert. Surfaced once at
        # DEBUG (no secret) so a dev checkout without Telegram config degrades safely.
        self._enabled = bool(bot_token and chat_id)
        if not self._enabled:
            _log.debug("TelegramAlertChannel not configured; alerts will skip Telegram.")

    @property
    def enabled(self) -> bool:
        """Whether this channel is configured to actually deliver (token + chat)."""
        return self._enabled

    def deliver(self, kind: AlertKind, subject: str, detail: str) -> bool:
        """POST the alert to Telegram; return whether it was accepted (2xx).

        Fail-closed: an unconfigured channel, a non-2xx response, or any transport
        error all return ``False`` without raising — an alert channel must never be
        the thing that crashes the process it is reporting on.
        """
        if not self._enabled:
            # Not configured — skip quietly (the caller's email channel still fires).
            return False

        # The token lives ONLY here, in the URL path, and this URL is never logged.
        url = _TELEGRAM_API.format(token=self._bot_token)
        payload = {"chat_id": self._chat_id, "text": f"{_format_subject(kind, subject)}\n\n{detail}"}

        try:
            response = httpx.post(url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            # Transport-level failure (DNS/connect/timeout): class only, never the
            # token or the URL, so the secret cannot leak into logs.
            _log.error("Telegram alert transport error: %s", exc.__class__.__name__)
            return False

        if response.is_success:
            _log.info("Alert delivered via Telegram: %s", kind.value)
            return True

        # Log the status but NOT the body — a Telegram error body can echo back the
        # chat id / request we would rather not persist.
        _log.error("Telegram rejected the alert with HTTP %s.", response.status_code)
        return False


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to UTC-aware, or pass ``None`` through.

    WHY: our ``DateTime(timezone=True)`` columns are stored tz-aware, but the SQLite
    dev/test backend reads them back NAIVE (Postgres returns aware). Comparing a
    naive persisted ``last_fired_at`` against a tz-aware ``now`` would raise
    ``TypeError``. Treating a naive value as UTC (the timezone everything is written
    in) keeps the dedup-window maths portable across both backends. Mirrors the
    identical helper in :mod:`vision.ops.feed_health` (duplicated, not shared, to
    avoid a feed_health -> alerts -> feed_health import cycle).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@runtime_checkable
class AlertDedupStore(Protocol):
    """Durable backing store for alert suppression state.

    Abstracts *where* last-fired timestamps live so the :class:`Alerter` can be
    unit-tested with an in-memory store and run in prod against the database. Both
    methods are fail-open toward NOTIFYING (§22.9): if the store cannot be read, the
    caller must treat the alert as not-yet-fired and send it — a duplicate alert is
    strictly safer than a silently swallowed incident.
    """

    def get_last_fired(self, key: str) -> datetime | None:
        """Return the persisted last-fired instant for ``key``, or ``None``."""
        ...

    def record_fired(self, key: str, at: datetime) -> None:
        """Persist that the alert for ``key`` fired at ``at`` (upsert)."""
        ...


@dataclass
class InMemoryAlertDedupStore:
    """Process-local dedup store — suppression lasts only for THIS process.

    The default for a bare :class:`Alerter` (and the historical behaviour). Fine for
    a long-lived process or a test, but NOT durable across cron ticks — production
    wiring in :func:`build_alerter` swaps in :class:`DbAlertDedupStore` so a
    persistent fault is suppressed across restarts.
    """

    _fired: dict[str, datetime] = field(default_factory=dict)

    def get_last_fired(self, key: str) -> datetime | None:
        return self._fired.get(key)

    def record_fired(self, key: str, at: datetime) -> None:
        self._fired[key] = at


@dataclass
class DbAlertDedupStore:
    """Database-backed dedup store — suppression survives process restarts (NFR-08).

    Reads/writes one :class:`AlertState` row per ``dedup_key`` through an injected
    session factory (the app's :func:`~vision.db.session.get_session` in prod, a test
    session in tests). The store is deliberately fail-OPEN: a read/write error is
    logged (class only, never a secret) and treated as "not fired", so a database
    hiccup can at most cause a duplicate alert — never a missed incident (§22.9).
    """

    # A context-manager factory yielding a Session that COMMITS on clean exit
    # (get_session's contract). Injected so tests can share a hermetic session.
    session_factory: Callable[[], AbstractContextManager[Session]] = get_session

    def get_last_fired(self, key: str) -> datetime | None:
        """Return the persisted last-fired instant for ``key`` (UTC-aware) or ``None``.

        Fail-open: on any database error we log the error CLASS (never row data) and
        return ``None`` so the alert is treated as not-yet-fired and still notifies.
        """
        try:
            with self.session_factory() as session:
                row = session.execute(
                    select(AlertState).where(AlertState.dedup_key == key)
                ).scalar_one_or_none()
                return _as_utc(row.last_fired_at) if row is not None else None
        except SQLAlchemyError as exc:
            _log.error(
                "alert dedup read failed (%s); treating as not-yet-fired.",
                exc.__class__.__name__,
            )
            return None

    def record_fired(self, key: str, at: datetime) -> None:
        """Upsert the last-fired instant for ``key`` to ``at``.

        Read-modify-write guarded by the UNIQUE ``dedup_key`` so it is portable
        across SQLite and Postgres (no dialect-specific ``ON CONFLICT``). A losing
        concurrent INSERT race raises ``IntegrityError``, which we fold into an
        UPDATE in a fresh transaction. A persistence failure is logged and swallowed
        — the alert has already been delivered, so failing to stamp state must not
        crash the caller (fail-open toward the safe direction: at worst a re-alert).
        """
        try:
            self._upsert(key, at)
        except IntegrityError:
            # A concurrent tick inserted the row first; fold to an update.
            try:
                self._upsert(key, at)
            except SQLAlchemyError as exc:
                _log.error(
                    "alert dedup write retry failed (%s); suppression not persisted.",
                    exc.__class__.__name__,
                )
        except SQLAlchemyError as exc:
            _log.error(
                "alert dedup write failed (%s); suppression not persisted.",
                exc.__class__.__name__,
            )

    def _upsert(self, key: str, at: datetime) -> None:
        """Insert a new last-fired row or update the existing one for ``key``."""
        with self.session_factory() as session:
            row = session.execute(
                select(AlertState).where(AlertState.dedup_key == key)
            ).scalar_one_or_none()
            if row is None:
                session.add(AlertState(dedup_key=key, last_fired_at=at))
            else:
                # A new value on the existing row (ORM attribute write, not a mutation
                # of a shared object) — updated_at bumps via the TimestampMixin.
                row.last_fired_at = at
                session.add(row)


@dataclass
class Alerter:
    """Fans an alert out to every configured channel with durable dedup/rate-limiting.

    WHY dedup lives here (not in each channel): a single persistent fault (a dead
    feed, a token that needs re-auth) would otherwise re-alert on every cron tick
    and bury the owner. The Alerter suppresses an identical ``(kind, subject)``
    alert for ``dedup_window`` after it first fires, across ALL channels, so the
    owner gets one notification per incident per window (NFR-08 "actionable, not
    noisy").

    DURABILITY (the fix for the process-local-state bug): suppression state lives in
    an injectable :class:`AlertDedupStore`, NOT only in memory. Each cron tick is a
    fresh process, so an in-memory-only map would start empty every tick and let a
    permanent fault re-alert forever. :func:`build_alerter` wires a
    :class:`DbAlertDedupStore` so the last-fired stamp is persisted and read back on
    the next tick. The in-memory ``_cache`` below is kept purely as a
    within-process OPTIMISATION in front of the store, never the source of truth.
    The clock is injectable so tests are deterministic without sleeping.
    """

    # The delivery channels to try, in order. Email first (primary), Telegram second
    # (optional out-of-band). A copy is stored so an external list can't mutate us.
    channels: list[AlertChannel]
    # How long an identical alert is suppressed after firing (config-over-code).
    dedup_window: timedelta = _DEFAULT_DEDUP_WINDOW
    # Injectable clock (UTC). Defaults to wall-clock; tests pass a fake for hermetic
    # dedup-window assertions.
    now: Callable[[], datetime] = field(default_factory=lambda: lambda: datetime.now(timezone.utc))
    # DURABLE suppression state. Defaults to in-memory (bare Alerter / tests);
    # build_alerter injects a DB-backed store so suppression survives restarts.
    store: AlertDedupStore = field(default_factory=InMemoryAlertDedupStore)
    # Within-process read cache in FRONT of the store (an optimisation, not the
    # source of truth) so repeated checks in one tick don't re-query the DB.
    _cache: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)

    def _dedup_key(self, kind: AlertKind, subject: str) -> str:
        """Compute the suppression key for an alert.

        Keyed on kind + subject (NOT the detail) so the same incident re-worded with
        fresh detail (e.g. an updated timestamp) is still recognised as a repeat and
        suppressed within the window.
        """
        return f"{kind.value}::{subject}"

    def _last_fired_at(self, key: str) -> datetime | None:
        """Return the last-fired instant for ``key``, cache-first then durable store.

        The in-memory cache short-circuits a repeat within THIS process; on a miss we
        consult the durable store (which survives restarts) and warm the cache. This
        is what makes a fresh cron process — whose cache starts empty — still see a
        prior tick's fire and stay quiet.
        """
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        stored = self.store.get_last_fired(key)
        if stored is not None:
            self._cache[key] = stored
        return stored

    def _is_suppressed(self, key: str, at: datetime) -> bool:
        """Return True if an identical alert fired within the dedup window of ``at``."""
        last = self._last_fired_at(key)
        if last is None:
            return False
        return at - last < self.dedup_window

    def alert(self, kind: AlertKind, subject: str, detail: str) -> bool:
        """Dispatch an alert to all channels unless it is a suppressed repeat.

        Returns ``True`` when the alert was dispatched (at least attempted on the
        channels), ``False`` when it was suppressed as a within-window duplicate.
        A channel that fails is logged and skipped — one dead channel never stops
        the others, and an all-channels-failed alert still counts as dispatched
        (the suppression window still starts, so a flapping fault can't spin).
        """
        now = self.now()
        key = self._dedup_key(kind, subject)

        if self._is_suppressed(key, now):
            # A duplicate inside the window — record nothing new, notify nobody.
            _log.debug("alert suppressed (deduped within window): %s", kind.value)
            return False

        # Mark BEFORE delivery, and DURABLY, so (a) a delivery that itself takes a
        # while can't let a concurrent identical alert slip through the window, and
        # (b) the very next cron tick — a fresh process — reads the persisted stamp
        # and suppresses instead of re-alerting. The cache is updated in lockstep as
        # a within-process optimisation; the store is the cross-process truth.
        self._cache[key] = now
        self.store.record_fired(key, now)

        delivered_any = False
        for channel in self.channels:
            try:
                ok = channel.deliver(kind, subject, detail)
            except Exception as exc:  # noqa: BLE001 - defensive: a channel must never crash alerting
                # Defence-in-depth: even a misbehaving channel that violates the
                # "never raise" contract cannot take down the alerter or the caller.
                _log.error(
                    "alert channel %r raised while delivering: %s",
                    getattr(channel, "name", channel.__class__.__name__),
                    exc.__class__.__name__,
                )
                continue
            delivered_any = delivered_any or ok

        if not delivered_any:
            # Every channel declined/failed — the incident is still "seen" (window
            # started) but we log that no channel actually reached the owner.
            _log.error("alert dispatched but NO channel accepted it: %s", kind.value)
        return True


def _build_telegram_channel(settings: Settings) -> TelegramAlertChannel | None:
    """Build a Telegram channel from env config, or ``None`` when not configured.

    WHY read from env directly (not from ``Settings``): the bot token is a secret
    and the Telegram lane is an optional, deployment-specific add-on, so it is kept
    out of the core typed settings surface — exactly as the mailer reads its
    non-core transport knobs from env. A missing token OR chat id means "no Telegram
    channel", which the caller simply omits (config-gated, §22).
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (bot_token and chat_id):
        return None
    return TelegramAlertChannel(bot_token=bot_token, chat_id=chat_id)


def _dedup_window_from_env(default: timedelta = _DEFAULT_DEDUP_WINDOW) -> timedelta:
    """Read the alert dedup window (minutes) from env, falling back to the default.

    A non-integer value is a config error; we fail toward the safe default rather
    than crashing alerting on a typo (an un-alerting deployment is worse than a
    mis-tuned window).
    """
    raw = os.environ.get("ALERT_DEDUP_WINDOW_MINUTES")
    if raw is None:
        return default
    try:
        minutes = int(raw)
    except ValueError:
        _log.warning("ALERT_DEDUP_WINDOW_MINUTES is not an integer; using default window.")
        return default
    # A non-positive window would disable dedup entirely; clamp to the default so a
    # fat-fingered 0 can't turn alerting into a spam firehose.
    if minutes <= 0:
        _log.warning("ALERT_DEDUP_WINDOW_MINUTES must be positive; using default window.")
        return default
    return timedelta(minutes=minutes)


def build_alerter(settings: Settings | None = None) -> Alerter:
    """Assemble the default :class:`Alerter` from configuration.

    The email channel is ALWAYS present (built from the existing mailer factory);
    the Telegram channel is appended ONLY when its env config is complete. This is
    the single entry point the daily job / publisher / feed-health checker use so
    channel wiring lives in exactly one place (config-over-code, §22).

    Crucially, the alerter is wired with a DURABLE :class:`DbAlertDedupStore` (not
    the in-memory default) so suppression state survives the process — each cron
    tick spawns a new process, and only a persisted last-fired stamp stops a
    permanent fault from re-alerting every tick (NFR-08).
    """
    settings = settings or get_settings()
    channels: list[AlertChannel] = [EmailAlertChannel(get_sender(settings))]

    telegram = _build_telegram_channel(settings)
    if telegram is not None:
        channels.append(telegram)

    return Alerter(
        channels=channels,
        dedup_window=_dedup_window_from_env(),
        store=DbAlertDedupStore(),
    )
