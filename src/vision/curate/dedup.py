"""Item-level and cross-day deduplication for the CURATE layer (BRD §12.4, FR-03).

WHY this module exists
----------------------
Ingestion pulls the same story from overlapping feeds (e.g. a wire item echoed by
three outlets) and re-pulls yesterday's stories on the next day. Surfacing the
same signal twice — within one run OR across days — wastes the single daily post
and reads as repetitive. BRD §12.4 mandates three item-level checks and one
cross-day check:

  * exact URL          — the strongest identity signal (after light normalisation)
  * normalised-title   — fuzzy match via ``difflib`` ratio ≥ threshold (near-dups
                         where two outlets reword the same headline)
  * content_hash       — an MD5 fingerprint of the normalised title+summary, so a
                         different URL for identical content still collapses
  * cross-day (14d)    — "don't resurface an item used in a draft in the last N
                         days" — matched by identity (id / url / hash / title),
                         because a re-fetched story is a *new* ``Item`` row.

Design notes
------------
* Two flavours per the task spec: a **pure** function (``deduplicate``) that needs
  nothing but the in-memory items — trivially unit-testable, no I/O — and a
  **session-using** variant (``suppress_recently_used``) for the cross-day check
  that must query ``drafts``/``items``.
* MD5 content-hashing reuses the finalert ``news_mapper/fetcher.py`` pattern
  (adapted, not imported).
* Inputs are never mutated; every function returns NEW lists (immutability, §22).
* Duck-typed over anything exposing ``title``/``url``/``summary`` (+ ``id`` and
  ``content_hash`` when present), so ORM ``Item`` rows and lightweight test
  stand-ins both work without a shared base class.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from vision.db.models import Draft, Item

logger = logging.getLogger(__name__)

# Default fuzzy-title threshold. Mirrors ``Settings.dedup_sim_threshold`` (0.80)
# so callers that do not thread config through still get the BRD-configured value
# — but any caller may override per-call (config-over-code, §22.6).
DEFAULT_SIM_THRESHOLD: float = 0.80

# Default cross-day suppression window (BRD §12.4: "last 14 days").
DEFAULT_SUPPRESS_DAYS: int = 14

# Query-string keys that carry only tracking noise, never identity. Dropped
# during URL normalisation so ``?utm_source=x`` variants collapse to one item.
_TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_", "fbclid", "gclid", "mc_", "ref")


@runtime_checkable
class DedupItem(Protocol):
    """Structural contract for anything this module can deduplicate.

    ORM ``Item`` satisfies it, and so does any test double exposing these
    attributes. ``content_hash`` may be ``None`` — it is computed on demand when
    absent (``id`` likewise, for pure in-memory items that were never persisted).
    """

    title: str
    url: str
    summary: str | None


@dataclass(frozen=True)
class DedupResult:
    """Outcome of a dedup pass — kept items plus removed items with reasons.

    Frozen so a result can be safely shared/logged without a caller mutating it.
    ``removed`` pairs each dropped item with a human-readable reason string, which
    feeds the selection rationale (transparency, §14.1 "sources shown").
    """

    kept: list[Any]
    removed: list[tuple[Any, str]] = field(default_factory=list)

    @property
    def removed_count(self) -> int:
        """Number of items dropped — convenient for run stats/logging (§17)."""
        return len(self.removed)


# ---------------------------------------------------------------------------
# Normalisation helpers — the primitives every check is built on.
# ---------------------------------------------------------------------------


def normalise_title(title: str | None) -> str:
    """Return a canonical form of ``title`` for fuzzy comparison + hashing.

    WHY: raw headlines differ in case, punctuation and whitespace even when the
    story is identical ("FDA Clears AI Tool" vs "FDA clears AI tool."). We
    lower-case, strip everything that is not a word character or space, and
    collapse runs of whitespace so ``difflib`` compares *content*, not typography.
    """
    if not title:
        return ""
    lowered = title.lower()
    # Replace any non-alphanumeric/space run with a single space, then squeeze.
    cleaned = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalise_url(url: str | None) -> str:
    """Return a canonical URL used as the exact-match dedup key.

    Normalisation folds cosmetically-different URLs onto one identity:
      * scheme is DROPPED entirely so ``http`` and ``https`` variants match;
      * host is lower-cased and a leading ``www.`` is removed;
      * a trailing slash on the path is stripped;
      * tracking query params (``utm_*``/``fbclid``/...) are removed and the
        survivors sorted so param order never splits an identity.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    # Host: lower-case and drop a leading www. (purely cosmetic subdomain).
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    # Path: drop a single trailing slash so /a/ and /a are one identity.
    path = parts.path.rstrip("/")
    # Query: keep only non-tracking params, sorted for order-independence.
    kept_params = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not any(key.lower().startswith(prefix) for prefix in _TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(sorted(kept_params))
    # Scheme intentionally omitted; fragment (#...) intentionally dropped.
    canonical = f"{host}{path}"
    return f"{canonical}?{query}" if query else canonical


def compute_content_hash(title: str | None, summary: str | None) -> str:
    """Return an MD5 fingerprint of the normalised title + summary.

    Reuses finalert's MD5 approach: a stable hex digest lets identical content
    published at different URLs collapse to one item. Title and summary are joined
    with a separator that cannot appear in the normalised text, so distinct
    (title, summary) pairs cannot accidentally hash-collide via concatenation.
    """
    normalised = f"{normalise_title(title)}|{normalise_title(summary)}"
    # MD5 is used for a *dedup fingerprint*, never for security — collision risk
    # here is cosmetic, and MD5 keeps parity with the finalert ingestion layer.
    return hashlib.md5(normalised.encode("utf-8")).hexdigest()  # noqa: S324


def _hash_of(item: Any) -> str:
    """Return an item's ``content_hash``, computing + caching it when absent.

    Persisted ORM items usually already carry ``content_hash`` from ingest; pure
    in-memory test items may not, so we derive it. When the attribute exists but
    is ``None`` we also backfill it on the object so downstream stages (and the DB
    row) share one consistent fingerprint.
    """
    existing = getattr(item, "content_hash", None)
    if existing:
        return str(existing)
    computed = compute_content_hash(getattr(item, "title", ""), getattr(item, "summary", ""))
    # Backfill only if the object declares the attribute (ORM Item does); never
    # invent attributes on foreign objects.
    if hasattr(item, "content_hash"):
        item.content_hash = computed
    return computed


def title_similarity(a: str | None, b: str | None) -> float:
    """Return the ``difflib`` ratio (0..1) of two titles after normalisation.

    ``SequenceMatcher.ratio`` is a cheap, dependency-free near-duplicate signal:
    1.0 for identical normalised titles, degrading as wording diverges. Used with
    a threshold to catch reworded headlines that share no URL/hash.
    """
    return SequenceMatcher(None, normalise_title(a), normalise_title(b)).ratio()


# ---------------------------------------------------------------------------
# Pure item-level dedup (no I/O).
# ---------------------------------------------------------------------------


def deduplicate(
    items: list[Any],
    *,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
) -> DedupResult:
    """Collapse duplicates within one batch: exact URL, content hash, fuzzy title.

    Order of checks per item (first hit wins, cheapest/strongest first):
      1. exact normalised-URL already seen        → drop ("duplicate-url")
      2. content-hash already seen                → drop ("duplicate-content-hash")
      3. normalised-title ≥ ``sim_threshold`` vs  → drop ("near-duplicate-title")
         any kept survivor

    The FIRST occurrence of a cluster is kept (feeds are iterated in caller order,
    typically authority-sorted upstream), and inputs are never mutated — a new
    ``kept`` list is built.

    Args:
        items: batch to dedup; each exposes ``title``/``url``/``summary``.
        sim_threshold: minimum ``difflib`` ratio to treat two titles as the same
            story. Defaults to the BRD-configured 0.80.

    Returns:
        A ``DedupResult`` with survivors and (item, reason) removals.
    """
    kept: list[Any] = []
    removed: list[tuple[Any, str]] = []

    # Identity indexes for O(1) exact checks; survivor titles for the fuzzy scan.
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    kept_titles: list[str] = []

    for item in items:
        norm_url = normalise_url(getattr(item, "url", None))
        content_hash = _hash_of(item)
        # Empty title+summary hashes to a constant (md5 of "|"); treat that empty
        # payload as NON-identity so malformed rows sharing no real content are
        # never collapsed. Only a non-empty payload may seed/consult seen_hashes.
        has_content = bool(
            normalise_title(getattr(item, "title", None))
            or normalise_title(getattr(item, "summary", None))
        )

        # 1. Exact URL — strongest, cheapest identity signal.
        if norm_url and norm_url in seen_urls:
            removed.append((item, "duplicate-url"))
            continue

        # 2. Identical content under a different URL (only when content exists).
        if has_content and content_hash in seen_hashes:
            removed.append((item, "duplicate-content-hash"))
            continue

        # 3. Reworded headline — fuzzy-match the normalised title against every
        #    survivor. First survivor over threshold wins.
        norm_title = normalise_title(getattr(item, "title", None))
        near_dup = next(
            (
                kept_title
                for kept_title in kept_titles
                if norm_title
                and SequenceMatcher(None, norm_title, kept_title).ratio() >= sim_threshold
            ),
            None,
        )
        if near_dup is not None:
            removed.append((item, "near-duplicate-title"))
            continue

        # Survivor: record its identity for subsequent comparisons.
        kept.append(item)
        if norm_url:
            seen_urls.add(norm_url)
        if has_content:
            seen_hashes.add(content_hash)
        kept_titles.append(norm_title)

    logger.debug(
        "item-level dedup complete", extra={"kept": len(kept), "removed": len(removed)}
    )
    return DedupResult(kept=kept, removed=removed)


# ---------------------------------------------------------------------------
# Cross-day suppression (session-using variant).
# ---------------------------------------------------------------------------


def _ensure_aware(moment: datetime | None) -> datetime | None:
    """Coerce a naive datetime to UTC-aware so comparisons never raise.

    SQLite may hand back naive datetimes; mixing naive/aware raises ``TypeError``.
    We assume UTC for any naive value (the ingest layer stores UTC).
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _recent_used_identities(
    session: Session,
    *,
    cutoff: datetime,
) -> tuple[set[str], set[str], set[str], list[str]]:
    """Gather identities of items referenced by drafts created since ``cutoff``.

    "Used" means an item id appears in some ``draft.source_item_ids`` for a draft
    created within the window. Because a re-fetched story is a *new* ``Item`` row
    with a new id, we resolve those referenced ids back to ``Item`` rows and index
    them by every stable identity signal — id, normalised URL, content hash and
    normalised title — so today's fresh copy of a used story is still caught.

    Returns four indexes: (id-strings, normalised-urls, content-hashes, titles).
    """
    # 1. Pull recent drafts and union their referenced item-id strings.
    recent_drafts = (
        session.execute(select(Draft).where(Draft.created_at >= cutoff)).scalars().all()
    )
    used_id_strings: set[str] = set()
    for draft in recent_drafts:
        # source_item_ids is a portable JSON list[str]; guard against None.
        for raw_id in draft.source_item_ids or []:
            used_id_strings.add(str(raw_id))

    # Short-circuit: no references → empty indexes, skip the items query.
    if not used_id_strings:
        return set(), set(), set(), []

    # 2. Resolve those ids to Item rows to recover url/hash/title identities.
    #    Only ids that parse as UUIDs are queryable; others still suppress by id.
    used_items = (
        session.execute(select(Item)).scalars().all()  # small daily volumes
    )
    used_urls: set[str] = set()
    used_hashes: set[str] = set()
    used_titles: list[str] = []
    for item in used_items:
        if str(item.id) not in used_id_strings:
            continue
        norm_url = normalise_url(item.url)
        if norm_url:
            used_urls.add(norm_url)
        used_hashes.add(_hash_of(item))
        used_titles.append(normalise_title(item.title))

    return used_id_strings, used_urls, used_hashes, used_titles


def suppress_recently_used(
    items: list[Any],
    session: Session,
    *,
    days: int = DEFAULT_SUPPRESS_DAYS,
    now: datetime | None = None,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
) -> DedupResult:
    """Drop items already used in a draft within the last ``days`` (BRD §12.4).

    An incoming item is suppressed when ANY of these match a recently-used item:
    its own id-string, its normalised URL, its content hash, or a fuzzy-title
    match ≥ ``sim_threshold`` (a re-fetched story reworded slightly).

    Args:
        items: today's candidates (already item-level deduped, ideally).
        session: DB session to query ``drafts``/``items``.
        days: suppression window; default 14 per the BRD.
        now: reference "now" (injectable for deterministic tests); defaults to
            the current UTC time.
        sim_threshold: fuzzy-title threshold for the cross-day title check.

    Returns:
        ``DedupResult`` with survivors and (item, "recently-used-*") removals.
    """
    reference_now = _ensure_aware(now) or datetime.now(timezone.utc)
    cutoff = reference_now - timedelta(days=days)

    used_ids, used_urls, used_hashes, used_titles = _recent_used_identities(
        session, cutoff=cutoff
    )

    kept: list[Any] = []
    removed: list[tuple[Any, str]] = []

    for item in items:
        item_id = str(getattr(item, "id", "")) if getattr(item, "id", None) else ""
        norm_url = normalise_url(getattr(item, "url", None))
        content_hash = _hash_of(item)
        norm_title = normalise_title(getattr(item, "title", None))

        # Check strongest identity signals first, then the fuzzy fallback.
        if item_id and item_id in used_ids:
            removed.append((item, "recently-used-id"))
        elif norm_url and norm_url in used_urls:
            removed.append((item, "recently-used-url"))
        elif content_hash in used_hashes:
            removed.append((item, "recently-used-content-hash"))
        elif norm_title and any(
            SequenceMatcher(None, norm_title, used_title).ratio() >= sim_threshold
            for used_title in used_titles
        ):
            removed.append((item, "recently-used-title"))
        else:
            kept.append(item)

    logger.info(
        "cross-day suppression complete",
        extra={"window_days": days, "kept": len(kept), "suppressed": len(removed)},
    )
    return DedupResult(kept=kept, removed=removed)
