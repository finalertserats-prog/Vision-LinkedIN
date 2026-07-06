"""Unit tests for the INGEST layer — feeds, normalisation, and source seeding.

WHY these tests: BRD §18 makes tests part of "done" and forbids real network in
the suite. Every external dependency is mocked — ``feedparser.parse`` is
monkeypatched (so no HTTP), Hacker News' API is faked with respx (transport-layer
interception, so the real httpx request-building runs), and the DB is the
in-memory SQLite ``db_session`` fixture. Each test is AAA with a focused
behavioural assertion.

Coverage of the task's required cases:
  * normalisation of an RSS entry (tz-aware published_at, cleaned summary),
  * content_hash stability across whitespace/case differences,
  * the browser User-Agent actually reaching the network (RSS + API paths),
  * one dead feed not killing the parallel batch,
  * Hacker News API id->item mapping.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy.orm import Session

from vision.db.models import Source
from vision.ingest import feeds as feeds_mod
from vision.ingest.feeds import (
    BROWSER_USER_AGENT,
    FeedFetcher,
    RawItem,
)
from vision.ingest.normalise import (
    compute_content_hash,
    normalise,
    parse_published,
)
from vision.ingest.sources import (
    SeedSource,
    get_enabled_sources,
    load_seed,
    upsert_sources,
)

# --- Test doubles -----------------------------------------------------------


def _spec(name: str, url: str, kind: str = "rss", lane: str = "hc") -> SimpleNamespace:
    """Build a lightweight SourceLike stand-in (duck-types the ORM Source)."""
    return SimpleNamespace(name=name, lane=lane, kind=kind, url=url)


def _rss_bytes(title: str, link: str, summary: str) -> bytes:
    """Build a minimal, well-formed RSS document as raw bytes.

    WHY bytes (not a fake feedparser object): the fetcher now performs the HTTP
    request itself with httpx (so it can enforce ``self.timeout`` — see the
    per-feed-timeout fix) and hands the *raw response bytes* to
    ``feedparser.parse``. Tests therefore intercept at the httpx layer (respx)
    and return a real RSS body, exercising the true fetch + parse path offline.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Feed</title>'
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>{summary}</description>"
        "<pubDate>Sat, 05 Jul 2026 08:30:00 GMT</pubDate></item>"
        "</channel></rss>"
    ).encode("utf-8")


# --- normalisation ----------------------------------------------------------


def test_normalise_rss_entry_yields_tz_aware_item() -> None:
    # Arrange: a raw RSS item with an HTML summary and a struct_time timestamp.
    raw = RawItem(
        source_name="STAT News",
        lane="hc",
        kind="rss",
        title="  AI triage  cuts  ED wait times  ",
        url="https://example.com/story",
        summary="<p>Hospitals <b>report</b> gains.</p>",
        published_parsed=time.struct_time((2026, 7, 5, 8, 30, 0, 0, 0, 0)),
    )

    # Act
    item = normalise(raw)

    # Assert: whitespace-collapsed title, HTML-stripped summary, tz-aware date.
    assert item is not None
    assert item.title == "AI triage cuts ED wait times"
    assert item.summary == "Hospitals report gains."
    assert item.published_at is not None
    assert item.published_at.tzinfo is not None  # tz-aware, never naive


def test_normalise_drops_item_without_url() -> None:
    # Arrange: a titled entry with no URL cannot be deduped, so it must be dropped.
    raw = RawItem(
        source_name="X", lane="ai", kind="rss", title="headline", url="", summary=""
    )

    # Act
    item = normalise(raw)

    # Assert
    assert item is None


def test_content_hash_is_stable_across_whitespace_and_case() -> None:
    # Arrange: two titles that differ only by whitespace/case, same URL.
    hash_a = compute_content_hash("Hello   World", "https://Example.com/A")
    hash_b = compute_content_hash("  hello world ", "https://example.com/a")

    # Act / Assert: canonicalisation makes them identical (dedup relies on this).
    assert hash_a == hash_b


def test_content_hash_differs_for_different_urls() -> None:
    # Arrange / Act: same title, different URL -> different identity.
    hash_a = compute_content_hash("Same title", "https://example.com/1")
    hash_b = compute_content_hash("Same title", "https://example.com/2")

    # Assert
    assert hash_a != hash_b


def test_parse_published_from_epoch_is_utc() -> None:
    # Arrange: a Hacker-News-style epoch timestamp.
    raw = RawItem(
        source_name="HN", lane="ai", kind="api", title="t", url="u",
        summary="", published_epoch=1_751_700_000.0,
    )

    # Act
    parsed = parse_published(raw)

    # Assert: resolved to a tz-aware UTC datetime.
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2025  # 1_751_700_000s -> 2025 (sanity on the mapping)


def test_parse_published_naive_string_is_assumed_utc() -> None:
    # Arrange: a bare date string with no timezone.
    raw = RawItem(
        source_name="S", lane="hc", kind="rss", title="t", url="u",
        summary="", published_str="2026-07-05 08:30:00",
    )

    # Act
    parsed = parse_published(raw)

    # Assert: naive input is stamped UTC (never left tz-naive).
    assert parsed is not None
    assert parsed.utcoffset().total_seconds() == 0


# --- RSS fetch + browser UA -------------------------------------------------


@respx.mock
def test_fetch_rss_sends_browser_user_agent() -> None:
    # Arrange: intercept the HTTP fetch and return a real RSS body.
    feed_url = "https://e.com/feed"
    route = respx.get(feed_url).mock(
        return_value=Response(200, content=_rss_bytes("T", "https://e.com/1", "s"))
    )

    # Act
    items = FeedFetcher().fetch_rss(_spec("STAT", feed_url))

    # Assert: the browser UA (not a library default) reached the network layer,
    # and the fetched-then-parsed entry came through.
    assert route.calls.last.request.headers["user-agent"] == BROWSER_USER_AGENT
    assert len(items) == 1
    assert items[0].title == "T"


@respx.mock
def test_fetch_rss_raises_on_http_error_status() -> None:
    # Arrange: a 403 (bot-block) feed — raise_for_status must surface as failure.
    feed_url = "https://e.com/feed"
    respx.get(feed_url).mock(return_value=Response(403))

    # Act / Assert
    with pytest.raises(feeds_mod.FeedFetchError):
        FeedFetcher().fetch_rss(_spec("Blocked", feed_url))


@respx.mock
def test_fetch_rss_follows_redirects() -> None:
    # Arrange: a feed that 301-redirects to its canonical URL (common for feeds
    # that moved to HTTPS or a new path). The fetch must follow it, not fail.
    respx.get("https://e.com/old").mock(
        return_value=Response(301, headers={"Location": "https://e.com/new"})
    )
    respx.get("https://e.com/new").mock(
        return_value=Response(200, content=_rss_bytes("Moved", "https://e.com/1", "s"))
    )

    # Act
    items = FeedFetcher().fetch_rss(_spec("Redirected", "https://e.com/old"))

    # Assert
    assert [i.title for i in items] == ["Moved"]


# --- batch isolation: one dead feed must not kill the run -------------------


@respx.mock
def test_fetch_all_isolates_a_dead_feed() -> None:
    # Arrange: two RSS sources — one parses fine, one 403s.
    respx.get("https://good/feed").mock(
        return_value=Response(200, content=_rss_bytes("Good", "https://good/1", "s"))
    )
    respx.get("https://bad/feed").mock(return_value=Response(403))
    sources = [
        _spec("GoodFeed", "https://good/feed"),
        _spec("DeadFeed", "https://bad/feed"),
    ]

    # Act
    result = FeedFetcher(max_workers=2).fetch_all(sources)

    # Assert: the good feed's item survives; the dead feed is marked unhealthy.
    assert len(result.items) == 1
    assert result.items[0].source_name == "GoodFeed"
    assert result.health["GoodFeed"].ok is True
    assert result.health["DeadFeed"].ok is False
    assert result.health["DeadFeed"].error is not None


def test_fetch_all_bounds_a_hanging_rss_feed_by_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-1 regression: a hung RSS feed must not stall the whole batch.

    A slow/hanging feed used to block indefinitely because ``feedparser.parse``
    ran its own timeout-less urllib fetch; ``ThreadPoolExecutor.__exit__`` then
    joined that never-finishing worker and hung ``fetch_all`` (NFR-07/NFR-09).
    Here we simulate the hang as an httpx read timeout (what a bounded client
    raises once ``self.timeout`` elapses) and assert the batch still returns
    promptly, marks the slow feed failed, and keeps the healthy feed's items.
    """
    good_rss = _rss_bytes("Good", "https://good/1", "s")

    def fake_get(self: httpx.Client, url: str, *args: object, **kwargs: object):
        # A hanging feed: a bounded httpx client would raise ReadTimeout rather
        # than block forever. A well-behaved feed returns its body immediately.
        if "slow" in url:
            raise httpx.ReadTimeout("simulated hang", request=httpx.Request("GET", url))
        return Response(200, content=good_rss, request=httpx.Request("GET", url))

    monkeypatch.setattr(feeds_mod.httpx.Client, "get", fake_get)
    sources = [
        _spec("GoodFeed", "https://good/feed"),
        _spec("SlowFeed", "https://slow/feed"),
    ]

    # Act: time the batch — it must finish well within a small budget, never hang.
    start = time.monotonic()
    result = FeedFetcher(timeout=1.0, max_workers=2).fetch_all(sources)
    elapsed = time.monotonic() - start

    # Assert: prompt return, slow feed isolated as failed, good feed intact.
    assert elapsed < 5.0
    assert len(result.items) == 1
    assert result.items[0].source_name == "GoodFeed"
    assert result.health["GoodFeed"].ok is True
    assert result.health["SlowFeed"].ok is False
    assert result.health["SlowFeed"].error is not None


# --- Hacker News API mapping ------------------------------------------------


@respx.mock
def test_fetch_api_maps_hacker_news_items() -> None:
    # Arrange: fake the topstories list and the two item lookups it triggers.
    top_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    respx.get(top_url).mock(return_value=Response(200, json=[101, 102]))
    respx.get("https://hacker-news.firebaseio.com/v0/item/101.json").mock(
        return_value=Response(
            200,
            json={"title": "AI model for sepsis", "url": "https://ex.com/a", "time": 1_751_700_000},
        )
    )
    # Item 102 is an Ask HN with no url -> must fall back to an HN permalink.
    respx.get("https://hacker-news.firebaseio.com/v0/item/102.json").mock(
        return_value=Response(200, json={"title": "Ask HN: health data?", "time": 1_751_700_100})
    )

    # Act
    items = FeedFetcher().fetch_api(_spec("Hacker News", top_url, kind="api", lane="ai"))

    # Assert: both stories mapped; the url-less one got the HN permalink.
    assert [i.title for i in items] == ["AI model for sepsis", "Ask HN: health data?"]
    assert items[0].url == "https://ex.com/a"
    assert items[1].url == "https://news.ycombinator.com/item?id=102"
    assert items[0].published_epoch == 1_751_700_000


@respx.mock
def test_fetch_api_skips_malformed_item_without_aborting_source() -> None:
    """BUG-2 regression: a malformed HN item must be skipped, not fatal.

    Per-item field coercion (``title.strip()``, ``float(time)``) used to run
    OUTSIDE the item's try/except: a non-string title raised AttributeError and a
    non-numeric ``time`` raised ValueError, either of which aborted the ENTIRE HN
    source and violated the documented per-item isolation. The good stories must
    still come through while only the poisoned one is dropped.
    """
    top_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    respx.get(top_url).mock(return_value=Response(200, json=[201, 202, 203]))
    respx.get("https://hacker-news.firebaseio.com/v0/item/201.json").mock(
        return_value=Response(
            200, json={"title": "Good one", "url": "https://ex.com/1", "time": 1_751_700_000}
        )
    )
    # Poisoned item: a non-string title AND a non-numeric time — the exact shapes
    # that previously raised AttributeError / ValueError outside the guard.
    respx.get("https://hacker-news.firebaseio.com/v0/item/202.json").mock(
        return_value=Response(
            200, json={"title": 12345, "url": "https://ex.com/2", "time": "not-a-number"}
        )
    )
    respx.get("https://hacker-news.firebaseio.com/v0/item/203.json").mock(
        return_value=Response(
            200, json={"title": "Good two", "url": "https://ex.com/3", "time": 1_751_700_200}
        )
    )

    # Act
    items = FeedFetcher().fetch_api(_spec("Hacker News", top_url, kind="api", lane="ai"))

    # Assert: both good stories survive; the malformed one is silently skipped.
    assert [i.title for i in items] == ["Good one", "Good two"]


@respx.mock
def test_fetch_api_sends_browser_user_agent() -> None:
    # Arrange: capture request headers on the topstories call.
    top_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    route = respx.get(top_url).mock(return_value=Response(200, json=[]))

    # Act
    FeedFetcher().fetch_api(_spec("Hacker News", top_url, kind="api", lane="ai"))

    # Assert: the browser UA header was sent on the API request too.
    assert route.calls.last.request.headers["user-agent"] == BROWSER_USER_AGENT


@respx.mock
def test_fetch_api_rejects_unsupported_source() -> None:
    # Arrange: a non-HN API URL is explicitly unsupported.
    # Act / Assert
    with pytest.raises(feeds_mod.FeedFetchError):
        FeedFetcher().fetch_api(_spec("Other", "https://api.example.com/x", kind="api"))


# --- source seeding (DB) ----------------------------------------------------


def _seed(name: str, **overrides: object) -> SeedSource:
    """Factory for a SeedSource with sensible defaults (testing rule: factories)."""
    base: dict[str, object] = {
        "name": name,
        "lane": "hc",
        "kind": "rss",
        "url": f"https://example.com/{name}",
        "authority_weight": 0.5,
        "enabled": True,
    }
    base.update(overrides)
    return SeedSource(**base)  # type: ignore[arg-type]


def test_upsert_sources_is_idempotent_by_name(db_session: Session) -> None:
    # Arrange: seed once, then re-seed the same names with a changed weight.
    first = [_seed("STAT News", authority_weight=0.9), _seed("Endpoints")]
    upsert_sources(db_session, first)

    # Act: re-run with an updated weight for an existing source.
    second = [_seed("STAT News", authority_weight=0.42)]
    summary = upsert_sources(db_session, second)

    # Assert: no duplicate rows; the existing row was updated, not inserted.
    rows = db_session.query(Source).filter(Source.name == "STAT News").all()
    assert len(rows) == 1
    assert rows[0].authority_weight == 0.42
    assert summary == {"inserted": 0, "updated": 1, "total": 1}


def test_upsert_preserves_last_ok_at(db_session: Session) -> None:
    # Arrange: an existing source with runtime feed-health already stamped.
    from datetime import datetime, timezone

    stamped = datetime(2026, 7, 1, 6, 30, tzinfo=timezone.utc)
    db_session.add(
        Source(name="STAT News", lane="hc", kind="rss", url="https://old", last_ok_at=stamped)
    )
    db_session.flush()

    # Act: re-seed the same source (config changed).
    upsert_sources(db_session, [_seed("STAT News", url="https://new")])

    # Assert: config updated, but runtime last_ok_at is left intact.
    # SQLite stores DateTimes tz-naive, so re-stamp UTC before comparing the
    # wall-clock instant — the point is that upsert did NOT touch this value.
    row = db_session.query(Source).filter(Source.name == "STAT News").one()
    assert row.url == "https://new"
    assert row.last_ok_at.replace(tzinfo=timezone.utc) == stamped


def test_get_enabled_sources_filters_and_orders(db_session: Session) -> None:
    # Arrange: mix of enabled/disabled across lanes with distinct weights.
    upsert_sources(
        db_session,
        [
            _seed("HC-high", lane="hc", authority_weight=0.9),
            _seed("HC-low", lane="hc", authority_weight=0.3),
            _seed("HC-off", lane="hc", enabled=False),
            _seed("AI-one", lane="ai", authority_weight=0.8),
        ],
    )

    # Act: enabled HC sources only.
    hc = get_enabled_sources(db_session, lane="hc")

    # Assert: disabled excluded; ordered by authority weight desc.
    assert [s.name for s in hc] == ["HC-high", "HC-low"]


def test_load_seed_flattens_lane_groups_from_real_file() -> None:
    # Arrange / Act: load the shipped prep/sources_seed.yaml (config over code).
    seeds = load_seed()

    # Assert: it flattens across lane groups and includes the HN API source.
    names = {s.name for s in seeds}
    assert "STAT News" in names
    assert "Hacker News (front page)" in names
    hn = next(s for s in seeds if s.name == "Hacker News (front page)")
    assert hn.kind == "api"
