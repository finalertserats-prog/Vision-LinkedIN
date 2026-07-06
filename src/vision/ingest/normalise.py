"""Normalisation of fetched raw items to the common ``items`` schema (FR-02).

WHY this module exists: the INGEST layer pulls from heterogeneous sources (RSS
feeds via feedparser, JSON APIs like Hacker News) whose entries have different
field names, HTML-laden summaries, and wildly inconsistent timestamp formats.
Everything downstream (dedup, scoring, synthesis) needs one predictable shape.
This module is that boundary: a set of **pure functions** (no I/O, no DB, no
globals) that turn a :class:`vision.ingest.feeds.RawItem` into a
:class:`NormalisedItem` matching the ``items`` table columns (§11.2):

    title, url, source, published_at (tz-aware), summary, lane, content_hash, raw

Being pure makes each rule trivially unit-testable and deterministic — the same
raw entry always yields the same ``content_hash`` (BRD §22: deterministic
contracts), which is what makes cross-run deduplication (§12.4) reliable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as date_parser

from vision.ingest.feeds import RawItem
from vision.logging_setup import get_logger

# Pure module, but we still log (never print) when a timestamp can't be parsed so
# feed-quality issues are observable (§17) without failing the whole item.
_log = get_logger(__name__)

# Strips HTML tags from source summaries. Feeds routinely wrap snippets in markup
# (finalert fetcher stripped these too); we render plain text so the summary is
# safe to show in emails and to hash consistently.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Collapses any run of whitespace (incl. newlines) to a single space so that
# cosmetic whitespace differences never change the content hash.
_WHITESPACE_RE = re.compile(r"\s+")
# Hard cap on stored summary length — keeps rows bounded and mirrors the
# finalert 500-char snippet convention.
_SUMMARY_MAX_CHARS = 500


@dataclass(frozen=True)
class NormalisedItem:
    """A fetched signal normalised to the ``items`` schema (§11.2).

    Field names line up 1:1 with the DB columns so persisting is a direct map.
    Immutable (frozen) — normalisation produces a new value and never mutates the
    input RawItem, honouring the immutability convention (BRD §22).
    """

    title: str  # cleaned, whitespace-collapsed title
    url: str  # canonical link (dedup key)
    source: str  # originating source name (Source.name)
    lane: str  # 'hc' | 'ai'
    published_at: datetime | None  # tz-aware UTC publish time (None if unknown)
    summary: str  # HTML-stripped, length-capped snippet
    content_hash: str  # sha256 of normalised title+url (dedup, §12.4)
    raw: dict[str, Any] = field(default_factory=dict)  # original entry, for audit


def _collapse_whitespace(text: str) -> str:
    """Trim and collapse internal whitespace to single spaces.

    Used both for display cleanup and, crucially, for hash canonicalisation so
    that ``"Hello   World"`` and ``" hello world "`` do not produce different
    hashes for what is really the same title.
    """
    return _WHITESPACE_RE.sub(" ", text.strip())


def strip_html(text: str) -> str:
    """Remove HTML tags from a snippet and normalise its whitespace.

    Feeds embed markup in summaries; we want plain text for emails and hashing.
    """
    without_tags = _HTML_TAG_RE.sub(" ", text)
    return _collapse_whitespace(without_tags)


def clean_summary(summary: str) -> str:
    """Produce a display-ready, length-bounded plain-text summary."""
    return strip_html(summary)[:_SUMMARY_MAX_CHARS]


def compute_content_hash(title: str, url: str) -> str:
    """Return a stable sha256 hex digest of the normalised title + url.

    WHY normalise first: dedup (§12.4) must treat entries that differ only by
    casing or whitespace as identical. We lower-case and collapse whitespace on
    both fields, join them with a separator that can't appear after collapsing
    (a newline), and hash the UTF-8 bytes. Deterministic and collision-safe
    enough for near-duplicate detection.
    """
    canonical = (
        f"{_collapse_whitespace(title).lower()}\n{_collapse_whitespace(url).lower()}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_published(raw: RawItem) -> datetime | None:
    """Resolve a RawItem's timestamp to a tz-aware UTC datetime, or None.

    Sources express time three different ways; we try them in decreasing order of
    reliability:
      1. ``published_parsed`` — feedparser's pre-parsed struct_time (UTC-based).
      2. ``published_epoch``  — unix seconds (e.g. Hacker News 'time').
      3. ``published_str``    — a raw string, handed to dateutil as a last resort.
    Any value that comes back naive (no tzinfo) is assumed UTC and stamped so —
    guaranteeing every ``published_at`` is tz-aware and comparable across the
    SQLite→Postgres move (BRD §22). Unparseable timestamps return None (the item
    is still usable; the scorer treats missing dates conservatively).
    """
    # 1) feedparser struct_time: its first 6 fields are UTC (Y,M,D,h,m,s).
    if raw.published_parsed is not None:
        try:
            return datetime(*raw.published_parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError) as exc:
            _log.debug("bad struct_time for '%s': %s", raw.title, exc)

    # 2) Unix epoch seconds -> aware UTC datetime.
    if raw.published_epoch is not None:
        try:
            return datetime.fromtimestamp(raw.published_epoch, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            _log.debug("bad epoch for '%s': %s", raw.title, exc)

    # 3) Free-form string -> dateutil, then force tz-awareness.
    if raw.published_str:
        try:
            parsed = date_parser.parse(raw.published_str)
        except (ValueError, OverflowError, TypeError) as exc:
            # dateutil raises ValueError-family on unrecognisable input; a bad
            # date must not sink the item, so we log and fall through to None.
            _log.debug("unparseable date '%s' for '%s': %s", raw.published_str, raw.title, exc)
            return None
        # A naive datetime (no tzinfo) is assumed UTC; an aware one is converted.
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    # No usable timestamp on this entry.
    return None


def normalise(raw: RawItem) -> NormalisedItem | None:
    """Convert a RawItem into a NormalisedItem, or None if it is unusable.

    Returns None (rather than raising) when the entry lacks the minimum viable
    fields — a title and a URL — since those two are required to identify and
    dedup an item. Callers can therefore filter Nones without try/except noise.
    """
    title = _collapse_whitespace(raw.title or "")
    url = (raw.url or "").strip()
    # Both identity fields are mandatory; without them the item can't be hashed
    # or deduplicated, so we drop it (fail-safe, not fail-loud — a single junk
    # entry is expected, not exceptional).
    if not title or not url:
        _log.debug("dropping item with missing title/url from '%s'", raw.source_name)
        return None

    return NormalisedItem(
        title=title,
        url=url,
        source=raw.source_name,
        lane=raw.lane,
        published_at=parse_published(raw),
        summary=clean_summary(raw.summary or ""),
        content_hash=compute_content_hash(title, url),
        raw=raw.raw,
    )


def normalise_many(raws: list[RawItem]) -> list[NormalisedItem]:
    """Normalise a batch, dropping unusable entries.

    Thin convenience over :func:`normalise` for the common "normalise everything
    the fetcher returned" call; keeps the None-filtering in one place.
    """
    normalised: list[NormalisedItem] = []
    for raw in raws:
        item = normalise(raw)
        if item is not None:
            normalised.append(item)
    return normalised
