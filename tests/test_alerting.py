"""Tests for ops alerting + feed-health (BRD §17, NFR-07/08).

Strict AAA, one behaviour per test. ALL external I/O is mocked — no real SMTP, no
real Telegram/httpx call, no real network — per the testing rules and BRD §18.
Coverage:

  * every :class:`AlertKind` routes to the channel(s);
  * dedup suppresses an identical repeat within the window;
  * the Telegram channel is gated by config (inert without token+chat, active with);
  * a channel failure never crashes the alerter;
  * feed-health flags a stale source, emits ONE alert, optionally auto-disables,
    and leaves a healthy feed untouched;
  * ``record_ingest_success`` stamps ``last_ok_at``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.orm import Session

from vision.db.models import Source
from vision.ops.alerts import (
    AlertChannel,
    AlertKind,
    Alerter,
    DbAlertDedupStore,
    EmailAlertChannel,
    TelegramAlertChannel,
    build_alerter,
)
from vision.ops.feed_health import (
    FeedHealthReport,
    check_feed_health,
    record_ingest_success,
)

# A fixed reference "now" so every dedup-window / staleness assertion is
# deterministic and independent of wall-clock (testing rules: no shared mutable
# state, no time flakiness).
_NOW = datetime(2026, 7, 6, 9, 0, 0, tzinfo=timezone.utc)


def _fixed_clock(now: datetime):
    """Return a zero-arg callable yielding ``now`` — an injectable deterministic clock."""
    return lambda: now


def _mock_channel(name: str = "mock", *, accepts: bool = True) -> MagicMock:
    """Build a mock :class:`AlertChannel` whose ``deliver`` returns ``accepts``."""
    channel = MagicMock(spec=AlertChannel)
    channel.name = name
    channel.deliver.return_value = accepts
    return channel


# ---------------------------------------------------------------------------
# Alerter routing + dedup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(AlertKind))
def test_alert_routes_every_kind_to_channel(kind: AlertKind) -> None:
    # Arrange: an alerter with a single mock channel and a fixed clock.
    channel = _mock_channel()
    alerter = Alerter(channels=[channel], now=_fixed_clock(_NOW))

    # Act: fire the alert for this kind.
    dispatched = alerter.alert(kind, subject=f"{kind.value} happened", detail="context")

    # Assert: it was dispatched and the channel received exactly this kind.
    assert dispatched is True
    channel.deliver.assert_called_once()
    assert channel.deliver.call_args.args[0] is kind


def test_alert_fans_out_to_all_channels() -> None:
    # Arrange: two channels wired into one alerter.
    email = _mock_channel("email")
    telegram = _mock_channel("telegram")
    alerter = Alerter(channels=[email, telegram], now=_fixed_clock(_NOW))

    # Act.
    alerter.alert(AlertKind.PUBLISH_FAILURE, "publish failed", "draft 42")

    # Assert: both channels were asked to deliver.
    email.deliver.assert_called_once()
    telegram.deliver.assert_called_once()


def test_dedup_suppresses_identical_repeat_within_window() -> None:
    # Arrange: an alerter with a 1h window and a clock we control.
    channel = _mock_channel()
    alerter = Alerter(
        channels=[channel],
        dedup_window=timedelta(hours=1),
        now=_fixed_clock(_NOW),
    )

    # Act: fire the same (kind, subject) twice at the same instant.
    first = alerter.alert(AlertKind.DEAD_FEED, "feed dead", "STAT News")
    second = alerter.alert(AlertKind.DEAD_FEED, "feed dead", "STAT News")

    # Assert: only the first reached the channel; the repeat was suppressed.
    assert first is True
    assert second is False
    channel.deliver.assert_called_once()


def test_durable_dedup_suppresses_across_separate_alerter_instances(db_session: Session) -> None:
    # Arrange: a DURABLE (DB-backed) dedup store shared by two independent
    # alerters. Each alerter simulates a *separate cron tick* — a brand-new
    # process with its OWN empty in-memory cache — so the only thing carrying
    # suppression from the first tick to the second is the persisted store.
    from contextlib import contextmanager

    @contextmanager
    def _shared_session():
        # Hand both alerters the same in-memory test session (autoflush makes the
        # first tick's write visible to the second's read without a commit).
        yield db_session

    store = DbAlertDedupStore(session_factory=_shared_session)
    tick_one_channel = _mock_channel("tick1")
    tick_two_channel = _mock_channel("tick2")
    tick_one = Alerter(
        channels=[tick_one_channel],
        dedup_window=timedelta(hours=1),
        now=_fixed_clock(_NOW),
        store=store,
    )
    tick_two = Alerter(
        channels=[tick_two_channel],
        dedup_window=timedelta(hours=1),
        now=_fixed_clock(_NOW),
        store=store,
    )

    # Act: the SAME persistent fault is alerted on two consecutive ticks.
    first = tick_one.alert(AlertKind.DEAD_FEED, "feed dead", "STAT News")
    second = tick_two.alert(AlertKind.DEAD_FEED, "feed dead", "STAT News")

    # Assert: durable suppression — only the first tick reached a channel; the
    # second tick (a fresh process) saw the persisted last-fired stamp and stayed
    # quiet, so a permanent fault notifies ONCE per window, not every tick (NFR-08).
    assert first is True
    assert second is False
    tick_one_channel.deliver.assert_called_once()
    tick_two_channel.deliver.assert_not_called()


def test_dedup_allows_alert_again_after_window_elapses() -> None:
    # Arrange: a mutable clock so the second call lands past the window.
    clock = {"now": _NOW}
    channel = _mock_channel()
    alerter = Alerter(
        channels=[channel],
        dedup_window=timedelta(hours=1),
        now=lambda: clock["now"],
    )

    # Act: fire, advance the clock beyond the window, fire the same alert again.
    alerter.alert(AlertKind.TOKEN_REAUTH_NEEDED, "reauth", "linkedin")
    clock["now"] = _NOW + timedelta(hours=2)
    second = alerter.alert(AlertKind.TOKEN_REAUTH_NEEDED, "reauth", "linkedin")

    # Assert: the second fires because the window has expired.
    assert second is True
    assert channel.deliver.call_count == 2


def test_different_subjects_are_not_deduped() -> None:
    # Arrange.
    channel = _mock_channel()
    alerter = Alerter(channels=[channel], now=_fixed_clock(_NOW))

    # Act: same kind, different subjects — distinct incidents.
    alerter.alert(AlertKind.PUBLISH_FAILURE, "draft A failed", "x")
    alerter.alert(AlertKind.PUBLISH_FAILURE, "draft B failed", "y")

    # Assert: both delivered (dedup keys differ).
    assert channel.deliver.call_count == 2


def test_alert_dispatches_even_when_all_channels_fail() -> None:
    # Arrange: a channel that reports a handled failure.
    channel = _mock_channel(accepts=False)
    alerter = Alerter(channels=[channel], now=_fixed_clock(_NOW))

    # Act.
    dispatched = alerter.alert(AlertKind.DAILY_RUN_FAILURE, "run failed", "traceback-free")

    # Assert: still counts as dispatched (window starts) and the channel was tried.
    assert dispatched is True
    channel.deliver.assert_called_once()


def test_raising_channel_never_crashes_the_alerter() -> None:
    # Arrange: one channel that violates the contract and raises, plus a good one.
    bad = _mock_channel("bad")
    bad.deliver.side_effect = RuntimeError("boom")
    good = _mock_channel("good")
    alerter = Alerter(channels=[bad, good], now=_fixed_clock(_NOW))

    # Act: must not propagate the exception.
    dispatched = alerter.alert(AlertKind.DEAD_LETTER, "dead letter", "draft 9")

    # Assert: the good channel still delivered despite the bad one raising.
    assert dispatched is True
    good.deliver.assert_called_once()


# ---------------------------------------------------------------------------
# EmailAlertChannel — reuses the mailer's EmailSender
# ---------------------------------------------------------------------------


def test_email_channel_delegates_to_sender_with_tagged_subject() -> None:
    # Arrange: a mock EmailSender that accepts the send.
    sender = MagicMock()
    sender.send.return_value = True
    channel = EmailAlertChannel(sender)

    # Act.
    ok = channel.deliver(AlertKind.PUBLISH_FAILURE, "publish failed", "draft 7")

    # Assert: success is passed through and the subject is tagged with the kind.
    assert ok is True
    sender.send.assert_called_once()
    subject = sender.send.call_args.args[0]
    assert AlertKind.PUBLISH_FAILURE.value in subject


def test_email_channel_returns_false_when_sender_fails() -> None:
    # Arrange: a sender that reports a handled failure (never raises).
    sender = MagicMock()
    sender.send.return_value = False
    channel = EmailAlertChannel(sender)

    # Act.
    ok = channel.deliver(AlertKind.DEAD_FEED, "feed dead", "STAT News")

    # Assert: fail-closed — the channel reports the failure without raising.
    assert ok is False


# ---------------------------------------------------------------------------
# TelegramAlertChannel — config-gated, httpx mocked
# ---------------------------------------------------------------------------


def test_telegram_channel_inert_without_config() -> None:
    # Arrange: no chat id -> the channel is unconfigured.
    channel = TelegramAlertChannel(bot_token="", chat_id="")

    # Act.
    ok = channel.deliver(AlertKind.DEAD_FEED, "feed dead", "STAT News")

    # Assert: gated off — reports False and is marked disabled.
    assert channel.enabled is False
    assert ok is False


def test_telegram_channel_posts_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: a configured channel; stub httpx.post to a 200 without any network.
    posted: dict[str, object] = {}

    def fake_post(url: str, *, json: dict, timeout: int) -> httpx.Response:
        posted["url"] = url
        posted["json"] = json
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("vision.ops.alerts.httpx.post", fake_post)
    channel = TelegramAlertChannel(bot_token="secret-bot", chat_id="123456")

    # Act.
    ok = channel.deliver(AlertKind.TOKEN_REAUTH_NEEDED, "reauth", "linkedin")

    # Assert: it posted to Telegram with the configured chat id and succeeded.
    assert channel.enabled is True
    assert ok is True
    assert posted["json"]["chat_id"] == "123456"


def test_telegram_channel_returns_false_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: a configured channel; stub httpx.post to a 500.
    monkeypatch.setattr(
        "vision.ops.alerts.httpx.post",
        lambda url, *, json, timeout: httpx.Response(500, json={"ok": False}),
    )
    channel = TelegramAlertChannel(bot_token="secret-bot", chat_id="123456")

    # Act.
    ok = channel.deliver(AlertKind.DEAD_FEED, "feed dead", "x")

    # Assert: fail-closed on a rejected send.
    assert ok is False


def test_telegram_channel_returns_false_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: a configured channel; stub httpx.post to raise a transport error.
    def boom(url: str, *, json: dict, timeout: int) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("vision.ops.alerts.httpx.post", boom)
    channel = TelegramAlertChannel(bot_token="secret-bot", chat_id="123456")

    # Act: must not raise.
    ok = channel.deliver(AlertKind.PUBLISH_FAILURE, "publish failed", "x")

    # Assert: a transport failure is a handled False, never an exception.
    assert ok is False


# ---------------------------------------------------------------------------
# build_alerter — config gating of the Telegram lane
# ---------------------------------------------------------------------------


def test_build_alerter_omits_telegram_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: ensure no Telegram env config, and a stub email sender.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr("vision.ops.alerts.get_sender", lambda settings: MagicMock())

    # Act.
    alerter = build_alerter()

    # Assert: exactly one channel (email) — Telegram is gated out.
    assert [c.name for c in alerter.channels] == ["email"]


def test_build_alerter_includes_telegram_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: full Telegram env config + stub sender.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setattr("vision.ops.alerts.get_sender", lambda settings: MagicMock())

    # Act.
    alerter = build_alerter()

    # Assert: both lanes wired, email first then telegram.
    assert [c.name for c in alerter.channels] == ["email", "telegram"]


# ---------------------------------------------------------------------------
# Feed-health
# ---------------------------------------------------------------------------


def _add_source(
    session: Session,
    *,
    name: str,
    last_ok_at: datetime | None,
    created_at: datetime,
    enabled: bool = True,
) -> Source:
    """Persist a Source with explicit health timestamps for a health check.

    ``created_at`` is set explicitly (overriding the server default) so the
    "never-succeeded" branch can be exercised deterministically against ``_NOW``.
    """
    source = Source(
        name=name,
        lane="hc",
        kind="rss",
        url=f"https://example.test/{name}#feed",
        enabled=enabled,
        last_ok_at=last_ok_at,
        created_at=created_at,
    )
    session.add(source)
    session.commit()
    return source


def test_record_ingest_success_stamps_last_ok_at(db_session: Session) -> None:
    # Arrange: a source that has never been fetched.
    source = _add_source(
        db_session, name="StatNews", last_ok_at=None, created_at=_NOW - timedelta(days=1)
    )

    # Act: record a successful ingest.
    record_ingest_success(db_session, source, _NOW)
    db_session.commit()

    # Assert: last_ok_at now reflects the fetch instant.
    assert source.last_ok_at == _NOW


def test_check_feed_health_flags_stale_source_and_alerts(db_session: Session) -> None:
    # Arrange: one source silent for 3 days (well past the 48h default) + mock alerter.
    _add_source(
        db_session,
        name="StaleFeed",
        last_ok_at=_NOW - timedelta(days=3),
        created_at=_NOW - timedelta(days=10),
    )
    alerter = MagicMock(spec=Alerter)
    alerter.alert.return_value = True

    # Act.
    report = check_feed_health(db_session, _NOW, alerter)

    # Assert: the feed is flagged and exactly one dead_feed alert fired.
    assert report.stale == ("StaleFeed",)
    alerter.alert.assert_called_once()
    assert alerter.alert.call_args.args[0] is AlertKind.DEAD_FEED


def test_check_feed_health_alert_subject_distinguishes_feed_sets(db_session: Session) -> None:
    # Two DISTINCT dead-feed incidents that happen to have the SAME count must not
    # cross-suppress: the dedup key (kind::subject) has to vary by WHICH feeds are
    # dead, not merely how many — otherwise a newly-dead feed is silently swallowed
    # because an earlier, unrelated incident had the same count (NFR-08 actionable).
    captured_subjects: list[str] = []

    class _SpyChannel:
        name = "spy"

        def deliver(self, kind: AlertKind, subject: str, detail: str) -> bool:
            captured_subjects.append(subject)
            return True

    alerter = Alerter(
        channels=[_SpyChannel()], dedup_window=timedelta(hours=1), now=_fixed_clock(_NOW)
    )

    # Incident 1: exactly one feed (AlphaFeed) is dead.
    alpha = _add_source(
        db_session,
        name="AlphaFeed",
        last_ok_at=_NOW - timedelta(days=3),
        created_at=_NOW - timedelta(days=10),
    )
    check_feed_health(db_session, _NOW, alerter)

    # Incident 2 (still inside the window): AlphaFeed recovers, a DIFFERENT single
    # feed (BetaFeed) dies — same count (1), genuinely different incident.
    alpha.last_ok_at = _NOW
    db_session.add(alpha)
    _add_source(
        db_session,
        name="BetaFeed",
        last_ok_at=_NOW - timedelta(days=3),
        created_at=_NOW - timedelta(days=10),
    )
    db_session.commit()
    check_feed_health(db_session, _NOW, alerter)

    # Assert: both incidents reached the channel with DISTINCT subjects — the second
    # was not cross-deduped against the first just because the counts matched.
    assert len(captured_subjects) == 2
    assert captured_subjects[0] != captured_subjects[1]


def test_check_feed_health_leaves_healthy_feed_untouched(db_session: Session) -> None:
    # Arrange: a source fetched an hour ago (fresh) + mock alerter.
    _add_source(
        db_session,
        name="FreshFeed",
        last_ok_at=_NOW - timedelta(hours=1),
        created_at=_NOW - timedelta(days=10),
    )
    alerter = MagicMock(spec=Alerter)

    # Act.
    report = check_feed_health(db_session, _NOW, alerter)

    # Assert: healthy, not flagged, and no alert raised.
    assert report.stale == ()
    assert report.healthy == ("FreshFeed",)
    alerter.alert.assert_not_called()


def test_check_feed_health_does_not_flag_brand_new_never_ok_feed(db_session: Session) -> None:
    # Arrange: a source added an hour ago that has never succeeded yet.
    _add_source(
        db_session,
        name="NewFeed",
        last_ok_at=None,
        created_at=_NOW - timedelta(hours=1),
    )
    alerter = MagicMock(spec=Alerter)

    # Act.
    report = check_feed_health(db_session, _NOW, alerter)

    # Assert: a new feed gets a fair chance — not flagged, no alert.
    assert report.stale == ()
    alerter.alert.assert_not_called()


def test_check_feed_health_flags_old_never_ok_feed(db_session: Session) -> None:
    # Arrange: a source added 5 days ago that has NEVER produced a good fetch.
    _add_source(
        db_session,
        name="DeadOnArrival",
        last_ok_at=None,
        created_at=_NOW - timedelta(days=5),
    )
    alerter = MagicMock(spec=Alerter)
    alerter.alert.return_value = True

    # Act.
    report = check_feed_health(db_session, _NOW, alerter)

    # Assert: an old feed that never worked IS flagged.
    assert report.stale == ("DeadOnArrival",)


def test_check_feed_health_auto_disables_long_dead_feed_when_enabled(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: auto-disable ON; a feed dead for 10 days (past the 7-day threshold).
    monkeypatch.setenv("FEED_HEALTH_AUTO_DISABLE", "true")
    source = _add_source(
        db_session,
        name="LongDead",
        last_ok_at=_NOW - timedelta(days=10),
        created_at=_NOW - timedelta(days=30),
    )
    alerter = MagicMock(spec=Alerter)
    alerter.alert.return_value = True

    # Act.
    report = check_feed_health(db_session, _NOW, alerter)

    # Assert: the feed is disabled in the DB and reported as disabled.
    db_session.refresh(source)
    assert source.enabled is False
    assert report.disabled == ("LongDead",)


def test_check_feed_health_does_not_auto_disable_by_default(db_session: Session) -> None:
    # Arrange: auto-disable OFF (default); a feed dead for 10 days.
    source = _add_source(
        db_session,
        name="StillDead",
        last_ok_at=_NOW - timedelta(days=10),
        created_at=_NOW - timedelta(days=30),
    )
    alerter = MagicMock(spec=Alerter)
    alerter.alert.return_value = True

    # Act.
    report = check_feed_health(db_session, _NOW, alerter)

    # Assert: flagged but LEFT enabled — a human curates the toggle by default.
    db_session.refresh(source)
    assert source.enabled is True
    assert report.disabled == ()
    assert report.stale == ("StillDead",)


def test_check_feed_health_runs_without_an_alerter(db_session: Session) -> None:
    # Arrange: a stale feed but no alerter injected (report-only mode).
    _add_source(
        db_session,
        name="Silent",
        last_ok_at=_NOW - timedelta(days=3),
        created_at=_NOW - timedelta(days=10),
    )

    # Act: must not raise despite there being nothing to notify.
    report = check_feed_health(db_session, _NOW, alerter=None)

    # Assert: still classifies the feed; simply did not alert.
    assert report.stale == ("Silent",)
    assert report.alerted is False


def test_feed_health_report_is_immutable() -> None:
    # Arrange.
    report = FeedHealthReport(stale=("A",), healthy=("B",))

    # Act / Assert: frozen dataclass rejects mutation (§22 immutability).
    with pytest.raises(AttributeError):
        report.stale = ("C",)  # type: ignore[misc]
