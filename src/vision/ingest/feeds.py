"""Feed collection for the INGEST layer (FR-01).

WHY this module exists: VISION ingests two kinds of sources — RSS/Atom feeds
(parsed with ``feedparser``) and JSON APIs (currently Hacker News' Firebase
``topstories`` endpoint) — and must do so **reliably** on a VPS where any single
source may be slow, malformed, or actively block bot traffic. This module is the
single, well-behaved fetcher for both kinds. Its design goals mirror the
finalert patterns we adapt (news_mapper/fetcher.py + world_engine/ingestion.py):

  * **Browser User-Agent.** Several feeds (e.g. Endpoints, and server-side blocks
    on Healthcare IT News / MobiHealthNews) return HTTP 403 to a default/library
    UA. We send a real browser UA so those feeds respond (BRD §12.1: "identify
    with a proper User-Agent").
  * **Per-feed timeout.** A hung source must never stall the daily run.
  * **Parallel fetch.** Sources are independent, so a ThreadPoolExecutor fans the
    I/O out — turning N × timeout of wall-clock into ~1 × timeout (finalert
    world_engine pattern). This keeps the full run under the NFR-09 budget.
  * **Graceful per-feed failure.** One dead feed must not kill the batch (SC7 /
    NFR-07). Every source is fetched in isolation; failures are logged and
    recorded in a returned *health* dict (which the caller uses to update
    ``sources.last_ok_at`` — §11.1 / §17 feed-health tracking) while good
    sources still return their items.

This module returns light, immutable :class:`RawItem` records (a consistent
shape regardless of source kind). Converting those into the ``items`` DB schema
is the job of :mod:`vision.ingest.normalise` — kept separate so this file stays
purely about *fetching* and that file stays a pure, side-effect-free normaliser.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import feedparser
import httpx

from vision.logging_setup import get_logger

# Module logger — structured/redacted via the root config (logging_setup). Never
# ``print`` (BRD §22): logs are correlated by run_id and scrubbed of secrets.
_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Browser User-Agent.
#
# A current desktop Chrome UA string. Kept as a module constant (not inlined) so
# there is exactly one place to bump it, and so tests can assert *this* value is
# what actually reaches the network layer. Some feeds 403 a library/default UA
# (BRD note on endpts / healthcareitnews / mobihealthnews), so this is required,
# not cosmetic.
# ---------------------------------------------------------------------------
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Hacker News Firebase API roots. Only the ``topstories`` + ``item`` endpoints
# are used; kept as constants so the API-source handler can recognise HN by URL
# and resolve individual story ids without hard-coding paths mid-function.
_HN_TOPSTORIES_HINT = "hacker-news.firebaseio.com"
_HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
# HN story ids with no ``url`` (Ask HN, etc.) get a permalink on the HN site so
# every item still carries a resolvable URL (the dedup key downstream).
_HN_PERMALINK = "https://news.ycombinator.com/item?id={item_id}"


@runtime_checkable
class SourceLike(Protocol):
    """Minimal shape a source must have to be fetched.

    WHY a Protocol rather than importing the ORM ``Source``: it lets the fetcher
    accept either a real ``vision.db.models.Source`` row *or* any lightweight
    stand-in (a test fixture, a seed spec) without a hard dependency on the DB
    layer. Duck-typing keeps this network module unit-testable with no database.
    """

    name: str
    lane: str
    kind: str  # 'rss' | 'api'
    url: str


class FeedFetchError(Exception):
    """Raised when a single source cannot be fetched or is malformed.

    A *specific* exception (never a bare ``except``) so the batch orchestrator
    can distinguish an expected feed failure (log + continue) from a programming
    error, per BRD §22.
    """


@dataclass(frozen=True)
class RawItem:
    """One fetched entry in a source-agnostic, immutable shape.

    Carries the raw, source-provided fields plus the lane/source context needed
    downstream. Timestamp is left *unparsed* here (as a struct_time, epoch, or
    string, whichever the source gave) and is turned into a tz-aware datetime by
    :mod:`vision.ingest.normalise` — keeping this module free of date-parsing
    concerns. Frozen so a fetched item is never mutated in place (immutability,
    BRD §22).
    """

    source_name: str  # human label of the originating source (e.g. "STAT News")
    lane: str  # 'hc' | 'ai' — carried through to the item/draft provenance
    kind: str  # 'rss' | 'api' — how it was fetched (diagnostics only)
    title: str  # raw title text (normaliser cleans/strips it)
    url: str  # canonical link (dedup key once normalised)
    summary: str  # source abstract/snippet (may contain HTML; cleaned later)
    # Exactly one of the following three timestamp representations is typically
    # populated, depending on source kind; the normaliser tries them in order.
    published_parsed: time.struct_time | None = None  # RSS: feedparser struct_time
    published_epoch: float | None = None  # API: unix seconds (HN 'time')
    published_str: str | None = None  # RSS/API: raw string fallback for dateutil
    raw: dict[str, Any] = field(default_factory=dict)  # original entry for audit


@dataclass(frozen=True)
class FeedHealth:
    """Per-source outcome of a fetch, used to drive ``sources.last_ok_at`` (§17).

    ``ok`` True means the caller should stamp ``last_ok_at = checked_at``; False
    means the source failed this run (the caller leaves ``last_ok_at`` untouched
    and may alert if a source has been silent past a threshold). Immutable — a
    health record is a factual snapshot, never edited after the fact.
    """

    name: str  # source name (matches Source.name for a clean lookup)
    ok: bool  # did the fetch succeed and yield a usable feed?
    count: int  # how many RawItems were produced
    checked_at: datetime  # tz-aware UTC instant the fetch was attempted
    error: str | None = None  # short error text when ok is False (never a secret)


@dataclass(frozen=True)
class FetchResult:
    """Aggregate of a batch fetch: all items plus a per-source health map.

    ``health`` is keyed by source name so the caller can zip results back to the
    ``sources`` rows and update ``last_ok_at`` idempotently.
    """

    items: list[RawItem]
    health: dict[str, FeedHealth]


class FeedFetcher:
    """Fetches RSS and API sources with a browser UA, timeouts, and isolation.

    Instances are cheap and stateless between calls (no shared mutable buffers),
    so the same fetcher can be reused across daily runs. Tunables (timeout,
    worker count, per-feed item cap) are constructor args so they stay
    config-over-code (BRD §22) rather than magic numbers buried in methods.
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_workers: int = 6,
        user_agent: str = BROWSER_USER_AGENT,
        item_limit: int = 25,
        hn_story_limit: int = 30,
    ) -> None:
        # Per-feed network timeout (seconds). A slow source is abandoned rather
        # than allowed to stall the whole daily run (NFR-07/NFR-09).
        self.timeout = timeout
        # Parallelism for the fan-out. Bounded so the VPS memory/socket use stays
        # modest (mind the prior finalert memory-overload incident, §21).
        self.max_workers = max_workers
        # The browser UA actually sent to every source (RSS and API alike).
        self.user_agent = user_agent
        # Cap entries taken per feed so one prolific source can't flood a run.
        self.item_limit = item_limit
        # How many top HN story ids to resolve into items (HN is noisy — §12.2).
        self.hn_story_limit = hn_story_limit

    # -- RSS ----------------------------------------------------------------
    def fetch_rss(self, source: SourceLike) -> list[RawItem]:
        """Fetch and parse a single RSS/Atom feed into RawItems.

        WHY we do the HTTP fetch ourselves (and DON'T use ``feedparser.parse(url)``):
        ``feedparser`` runs its own blocking ``urllib`` request that has NO timeout
        hook — the ``agent`` kwarg only sets the User-Agent, it cannot bound the
        wait. A hung or trickle-slow feed would therefore block its worker thread
        forever, and ``ThreadPoolExecutor.__exit__`` joins every worker on the way
        out, so one dead feed could hang the entire daily run (violating NFR-07 /
        NFR-09). Instead we fetch the raw bytes with ``httpx`` — which DOES honour
        ``self.timeout`` — while still sending the browser UA (BRD §12.1) and
        following redirects, then hand the in-memory bytes to ``feedparser.parse``
        (which parses offline, no network). A >=400 status is turned into an
        explicit failure via ``raise_for_status`` so the batch records the source
        unhealthy — exactly how the server-side-403 feeds surface (BRD note).

        Raises:
            FeedFetchError: on a timeout/transport error, an HTTP error status, or
                an unusable/empty feed.
        """
        # Fetch the feed body with a *bounded* client. httpx enforces self.timeout
        # on connect/read/write, so a slow source is abandoned rather than allowed
        # to stall the daily run. follow_redirects handles feeds that moved (301).
        headers = {"User-Agent": self.user_agent}
        try:
            with httpx.Client(
                timeout=self.timeout, headers=headers, follow_redirects=True
            ) as client:
                response = client.get(source.url)
                # A 4xx/5xx (e.g. a 403 bot-block) means no usable content — fail
                # loudly so the source is recorded unhealthy and the run continues.
                response.raise_for_status()
                content = response.content
        except httpx.HTTPError as exc:
            # httpx.HTTPError covers timeouts, connect errors, and error statuses —
            # all expected per-feed failures (never a bug), so rewrap them as the
            # specific FeedFetchError the batch orchestrator knows to isolate.
            raise FeedFetchError(f"HTTP fetch failed for {source.name}: {exc}") from exc

        # feedparser parses the already-fetched bytes entirely in memory — no
        # network here, so this step cannot hang regardless of the source.
        parsed = feedparser.parse(content)

        entries = getattr(parsed, "entries", []) or []

        # A "bozo" feed is malformed. That is only fatal if it also yielded no
        # entries — many real feeds are technically bozo yet still parse usably,
        # so we tolerate those and only reject the truly empty/broken case.
        if not entries and getattr(parsed, "bozo", 0):
            reason = getattr(parsed, "bozo_exception", "malformed feed")
            raise FeedFetchError(f"malformed feed {source.name}: {reason}")

        items: list[RawItem] = []
        for entry in entries[: self.item_limit]:
            # feedparser entries support attribute access; getattr with defaults
            # keeps us safe against feeds that omit optional fields.
            title = (getattr(entry, "title", "") or "").strip()
            if not title:
                # A title-less entry can't be normalised meaningfully; skip it.
                continue
            link = getattr(entry, "link", "") or ""
            summary = getattr(entry, "summary", "") or ""
            items.append(
                RawItem(
                    source_name=source.name,
                    lane=source.lane,
                    kind="rss",
                    title=title,
                    url=link,
                    summary=summary,
                    # Prefer the pre-parsed struct_time; fall back to the raw
                    # 'published'/'updated' string for dateutil to handle later.
                    published_parsed=(
                        getattr(entry, "published_parsed", None)
                        or getattr(entry, "updated_parsed", None)
                    ),
                    published_str=(
                        getattr(entry, "published", None)
                        or getattr(entry, "updated", None)
                    ),
                    raw=dict(entry) if hasattr(entry, "keys") else {"repr": repr(entry)},
                )
            )
        return items

    # -- API (Hacker News) --------------------------------------------------
    def fetch_api(self, source: SourceLike) -> list[RawItem]:
        """Fetch a JSON API source. Currently supports Hacker News topstories.

        HN's Firebase API returns an array of top story *ids*; each id is then
        resolved to a story object via the ``item/{id}.json`` endpoint. We take
        the first ``hn_story_limit`` ids and fetch each item, sending the browser
        UA on every request (respected/asserted in tests).

        Raises:
            FeedFetchError: for an unsupported API URL or a transport/JSON error.
        """
        # Only HN is wired today; anything else is an explicit, specific failure
        # rather than a silent empty result (fail loudly, BRD §22).
        if _HN_TOPSTORIES_HINT not in source.url:
            raise FeedFetchError(f"unsupported API source: {source.url}")

        # A single client (with the browser UA + per-request timeout) is reused
        # for the topstories call and every item call, so connection setup is
        # amortised. ``raise_for_status`` turns 4xx/5xx into a caught error.
        headers = {"User-Agent": self.user_agent}
        try:
            with httpx.Client(timeout=self.timeout, headers=headers) as client:
                top = client.get(source.url)
                top.raise_for_status()
                story_ids = top.json()

                items: list[RawItem] = []
                # ``story_ids`` is a plain list[int]; slice to the configured cap.
                for story_id in list(story_ids)[: self.hn_story_limit]:
                    item = self._fetch_hn_item(client, source, story_id)
                    if item is not None:
                        items.append(item)
                return items
        except (httpx.HTTPError, ValueError) as exc:
            # httpx.HTTPError covers timeouts/connect/status; ValueError covers a
            # non-JSON body. Both are expected feed failures, not bugs.
            raise FeedFetchError(f"HN API fetch failed: {exc}") from exc

    def _fetch_hn_item(
        self, client: httpx.Client, source: SourceLike, story_id: Any
    ) -> RawItem | None:
        """Resolve one HN story id to a RawItem (or None to skip it).

        A single dead/blank item must not abort the source — a missing title or a
        transient item error just drops that one story and continues.
        """
        try:
            resp = client.get(_HN_ITEM_URL.format(item_id=story_id))
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Non-fatal: log at debug and skip this single id.
            _log.debug("HN item %s failed: %s", story_id, exc)
            return None

        # HN returns null for deleted items and dict for live ones.
        if not isinstance(data, dict):
            return None

        # WHY a SECOND try wraps the field coercion: even a live dict can carry
        # malformed fields — a non-string ``title`` makes ``.strip()`` raise
        # AttributeError, and a non-numeric ``time`` makes ``float(...)`` raise
        # ValueError. Because this method runs inside ``fetch_api``'s loop, an
        # unguarded raise here would abort the ENTIRE HN source and break the
        # documented per-item isolation. Guarding the coercion lets one poisoned
        # story be dropped (return None) while the rest of the feed continues.
        try:
            title = (data.get("title") or "").strip()
            if not title:
                return None
            # Ask HN / Show HN posts have no external ``url`` — fall back to the HN
            # permalink so the item still has a resolvable, unique URL.
            url = data.get("url") or _HN_PERMALINK.format(item_id=story_id)
            summary = data.get("text", "") or ""
            # HN 'time' is unix seconds — carried through for the normaliser. The
            # truthy guard preserves the original "missing/zero -> None" behaviour.
            raw_time = data.get("time")
            published_epoch = float(raw_time) if raw_time else None
        except (AttributeError, TypeError, ValueError) as exc:
            # Malformed single item: log at debug and skip just this story.
            _log.debug("HN item %s has malformed fields: %s", story_id, exc)
            return None

        return RawItem(
            source_name=source.name,
            lane=source.lane,
            kind="api",
            title=title,
            url=url,
            summary=summary,
            published_epoch=published_epoch,
            raw=data,
        )

    # -- Batch orchestration ------------------------------------------------
    def fetch_all(self, sources: list[SourceLike]) -> FetchResult:
        """Fetch every source in parallel, isolating per-source failures.

        Each source is dispatched by ``kind`` to the RSS or API handler inside a
        worker. A failure in one worker is caught, logged, and recorded as an
        unhealthy :class:`FeedHealth` — the batch keeps going and returns all
        items from the sources that succeeded (SC7 / NFR-07).
        """
        items: list[RawItem] = []
        health: dict[str, FeedHealth] = {}

        # ThreadPoolExecutor is ideal here: the work is I/O-bound (network), so
        # threads overlap the waiting and collapse N timeouts into ~one.
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            # Map each future back to its source so failures name the right feed.
            future_to_source = {
                pool.submit(self._fetch_one, source): source for source in sources
            }
            for future in as_completed(future_to_source):
                source = future_to_source[future]
                checked_at = datetime.now(timezone.utc)
                try:
                    source_items = future.result()
                except FeedFetchError as exc:
                    # Expected feed failure: record unhealthy, keep the batch alive.
                    _log.warning("source '%s' failed: %s", source.name, exc)
                    health[source.name] = FeedHealth(
                        name=source.name,
                        ok=False,
                        count=0,
                        checked_at=checked_at,
                        error=str(exc),
                    )
                    continue

                # Success: extend the shared list (done on the single main thread
                # as futures complete, so no locking is needed) and stamp health.
                items.extend(source_items)
                health[source.name] = FeedHealth(
                    name=source.name,
                    ok=True,
                    count=len(source_items),
                    checked_at=checked_at,
                )
                _log.info("source '%s': %d items", source.name, len(source_items))

        return FetchResult(items=items, health=health)

    def _fetch_one(self, source: SourceLike) -> list[RawItem]:
        """Dispatch a single source to the handler for its ``kind``.

        Wraps unexpected non-FeedFetchError exceptions from a handler into a
        FeedFetchError so ``fetch_all`` only ever has to catch one type — no bare
        excepts leak, and a genuinely unforeseen error still degrades to a
        per-source failure rather than crashing the run.
        """
        kind = (source.kind or "").lower()
        try:
            if kind == "rss":
                return self.fetch_rss(source)
            if kind == "api":
                return self.fetch_api(source)
            raise FeedFetchError(f"unknown source kind '{source.kind}'")
        except FeedFetchError:
            # Already the right type — let it propagate to fetch_all.
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately narrowed+rewrapped
            # Defence-in-depth: convert any unexpected handler error into the
            # expected failure type so one weird feed can never kill the batch.
            raise FeedFetchError(f"unexpected error fetching {source.name}: {exc}") from exc
