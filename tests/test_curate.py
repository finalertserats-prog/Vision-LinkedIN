"""CURATE layer tests — dedup, scoring, and lane-balanced selection.

AAA throughout (Arrange → Act → Assert), one behaviour per test, no network and
no real models (BRD §18/§22). Pure item-level tests build transient ORM ``Item``
objects in memory; the cross-day suppression test uses the hermetic in-memory
SQLite ``db_session`` fixture from ``conftest.py``.

Coverage map (per task spec):
  * exact + near-duplicate removal            → test_dedup_*
  * recency exponential-decay ordering        → test_recency_*
  * authority weighting                       → test_authority_weighting_*
  * cross-cut (HC×AI) bonus                    → test_crosscut_*
  * top-K lane balance                        → test_select_top_lane_balance*
  * cross-day suppression                     → test_suppress_recently_used*
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from vision.curate.dedup import (
    compute_content_hash,
    deduplicate,
    normalise_url,
    suppress_recently_used,
    title_similarity,
)
from vision.curate.score import (
    ScoringConfig,
    ScoringWeights,
    crosscut_bonus,
    recency_score,
    score_item,
    semantic_relevance,
)
from vision.curate.select import select_top
from vision.db.models import Draft, Item, Run, Source

# A fixed reference "now" so recency + suppression are fully deterministic.
NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def make_item(
    *,
    title: str,
    url: str,
    lane: str = "hc",
    summary: str = "",
    published_at: datetime | None = None,
    source: Source | None = None,
) -> Item:
    """Build a transient ORM ``Item`` for pure (non-persisted) scoring/dedup tests."""
    return Item(
        title=title,
        url=url,
        lane=lane,
        summary=summary,
        published_at=published_at if published_at is not None else NOW,
        source=source,
    )


# ---------------------------------------------------------------------------
# Normalisation primitives
# ---------------------------------------------------------------------------


def test_normalise_url_collapses_scheme_www_and_tracking_params() -> None:
    # Arrange: two cosmetically-different URLs for the same resource.
    a = "https://www.example.com/story/?utm_source=twitter&id=7"
    b = "http://example.com/story?id=7"

    # Act
    norm_a = normalise_url(a)
    norm_b = normalise_url(b)

    # Assert: scheme dropped, www stripped, trailing slash + utm removed → identical.
    assert norm_a == norm_b


def test_title_similarity_high_for_reworded_headline() -> None:
    # Arrange: same story, punctuation + case differences only.
    a = "FDA Clears First AI Diagnostic Tool"
    b = "fda clears first ai diagnostic tool."

    # Act
    ratio = title_similarity(a, b)

    # Assert
    assert ratio >= 0.95


# ---------------------------------------------------------------------------
# Item-level dedup (pure)
# ---------------------------------------------------------------------------


def test_dedup_removes_exact_url_duplicate() -> None:
    # Arrange: two items whose URLs normalise to the same identity.
    first = make_item(title="Story one", url="https://www.site.com/a?utm_source=rss")
    dup = make_item(title="Different headline", url="http://site.com/a/")
    unique = make_item(title="Another story", url="https://site.com/b")

    # Act
    result = deduplicate([first, dup, unique])

    # Assert: the exact-URL duplicate is dropped with the right reason.
    assert first in result.kept
    assert unique in result.kept
    assert (dup, "duplicate-url") in result.removed
    assert len(result.kept) == 2


def test_dedup_removes_near_duplicate_title() -> None:
    # Arrange: two outlets reword one headline (distinct URLs + content).
    # Distinct summaries → distinct content hashes, forcing the fuzzy-TITLE path
    # rather than the earlier content-hash check.
    original = make_item(
        title="FDA clears first AI diagnostic tool for sepsis",
        url="https://a.com/1",
        summary="Regulators approved the device on Monday.",
    )
    reworded = make_item(
        title="FDA clears first AI diagnostic tool for sepsis!",
        url="https://b.com/2",
        summary="The agency granted clearance this week.",
    )

    # Act (high threshold so only genuine near-dups collapse)
    result = deduplicate([original, reworded], sim_threshold=0.85)

    # Assert
    assert original in result.kept
    assert (reworded, "near-duplicate-title") in result.removed


def test_dedup_removes_content_hash_duplicate_under_different_url() -> None:
    # Arrange: identical title+summary republished at a different URL.
    shared_title = "Health system deploys ambient AI scribe"
    shared_summary = "A large hospital network rolled out an ambient documentation tool."
    a = make_item(title=shared_title, url="https://a.com/x", summary=shared_summary)
    b = make_item(title=shared_title, url="https://b.com/y", summary=shared_summary)

    # Act
    result = deduplicate([a, b])

    # Assert: same content hash → the second copy is dropped.
    assert compute_content_hash(shared_title, shared_summary)  # sanity: deterministic
    assert a in result.kept
    assert (b, "duplicate-content-hash") in result.removed


def test_dedup_keeps_distinct_empty_payload_items() -> None:
    # Arrange: two malformed ingest rows — DIFFERENT URLs, both empty title AND
    # empty summary. Their content hashes are the constant md5 of "|", but empty
    # content is NOT an identity, so neither may be dropped as a content-hash dup.
    a = make_item(title="", url="https://a.com/x", summary="")
    b = make_item(title="", url="https://b.com/y", summary="")

    # Act
    result = deduplicate([a, b])

    # Assert: distinct signal is preserved — both survive, nothing removed.
    assert a in result.kept
    assert b in result.kept
    assert len(result.kept) == 2
    assert result.removed == []


def test_dedup_keeps_distinct_items() -> None:
    # Arrange: three genuinely different stories.
    items = [
        make_item(title="Alpha", url="https://a.com/1"),
        make_item(title="Beta", url="https://a.com/2"),
        make_item(title="Gamma", url="https://a.com/3"),
    ]

    # Act
    result = deduplicate(items)

    # Assert: nothing removed.
    assert len(result.kept) == 3
    assert result.removed == []


# ---------------------------------------------------------------------------
# Recency scoring
# ---------------------------------------------------------------------------


def test_recency_score_decays_exponentially() -> None:
    # Arrange: fresh vs one-window-old vs missing timestamp.
    fresh = recency_score(NOW, NOW, recency_hours=48.0)
    one_window_old = recency_score(NOW - timedelta(hours=48), NOW, recency_hours=48.0)
    missing = recency_score(None, NOW, recency_hours=48.0)

    # Assert: 1.0 at age 0, ≈e⁻¹ at age == window, 0.0 when unknown.
    assert fresh == 1.0
    assert abs(one_window_old - 0.3678794) < 1e-4
    assert missing == 0.0


def test_recency_orders_newer_before_older() -> None:
    # Arrange: identical items differing only in publish time.
    config = ScoringConfig(recency_hours=48.0)
    newer = make_item(title="X", url="https://a.com/new", published_at=NOW - timedelta(hours=2))
    older = make_item(title="X2", url="https://a.com/old", published_at=NOW - timedelta(hours=40))

    # Act
    newer_score = score_item(newer, config, now=NOW)
    older_score = score_item(older, config, now=NOW)

    # Assert: recency component and total both favour the newer item.
    assert newer_score.recency > older_score.recency
    assert newer_score.total > older_score.total


def test_recency_clamps_future_timestamp() -> None:
    # Arrange: a feed with clock skew reports a future publish time.
    future = recency_score(NOW + timedelta(hours=5), NOW, recency_hours=48.0)

    # Assert: clamped to age 0 → maximum freshness, never > 1.0.
    assert future == 1.0


# ---------------------------------------------------------------------------
# Authority weighting
# ---------------------------------------------------------------------------


def test_authority_weighting_favours_trusted_source() -> None:
    # Arrange: two identical items from a high- vs low-authority source.
    config = ScoringConfig(recency_hours=48.0)
    trusted_src = Source(name="Nature Medicine", lane="hc", kind="rss", url="u1", authority_weight=0.95)
    weak_src = Source(name="Random Blog", lane="hc", kind="rss", url="u2", authority_weight=0.30)
    trusted = make_item(title="Same story", url="https://a.com/1", source=trusted_src)
    weak = make_item(title="Same story two", url="https://a.com/2", source=weak_src)

    # Act
    trusted_score = score_item(trusted, config, now=NOW)
    weak_score = score_item(weak, config, now=NOW)

    # Assert: authority component + total reward the trusted source.
    assert trusted_score.authority == 0.95
    assert weak_score.authority == 0.30
    assert trusted_score.total > weak_score.total


def test_authority_defaults_to_neutral_when_no_source() -> None:
    # Arrange: an item with no source relationship.
    config = ScoringConfig(recency_hours=48.0)
    item = make_item(title="Orphan", url="https://a.com/o")

    # Act
    breakdown = score_item(item, config, now=NOW)

    # Assert: neutral 0.5 midpoint, not a penalty.
    assert breakdown.authority == 0.5


# ---------------------------------------------------------------------------
# Semantic relevance + cross-cut bonus
# ---------------------------------------------------------------------------


def test_semantic_relevance_prefers_on_topic_text() -> None:
    # Arrange: a profile and two items — one dense with themes, one off-topic.
    profile = ("hospital", "revenue cycle", "automation", "clinical")
    on_topic = make_item(
        title="Hospital automates revenue cycle",
        url="https://a.com/1",
        summary="Clinical teams cut manual work with revenue cycle automation.",
    )
    off_topic = make_item(
        title="Local bakery wins award",
        url="https://a.com/2",
        summary="A neighbourhood bakery celebrates.",
    )

    # Act
    on_score = semantic_relevance(on_topic, profile)
    off_score = semantic_relevance(off_topic, profile)

    # Assert
    assert on_score > off_score
    assert off_score == 0.0


def test_semantic_relevance_uses_word_boundaries() -> None:
    # Arrange: 'ai' as a substring of 'email'/'maintain' must NOT count.
    profile = ("ai",)
    trap = make_item(title="Email maintains uptime", url="https://a.com/e")

    # Act
    score = semantic_relevance(trap, profile)

    # Assert: no whole-token 'ai' present → zero relevance.
    assert score == 0.0


def test_crosscut_bonus_only_when_both_lanes_present() -> None:
    # Arrange
    hc_keywords = ("hospital", "clinical")
    ai_keywords = ("machine learning", "ai")
    bridges = make_item(title="Hospital deploys machine learning triage", url="https://a.com/1")
    ai_only = make_item(title="New machine learning benchmark", url="https://a.com/2")
    hc_only = make_item(title="Hospital opens new clinical wing", url="https://a.com/3")

    # Act / Assert
    assert crosscut_bonus(bridges, hc_keywords, ai_keywords) == 1.0
    assert crosscut_bonus(ai_only, hc_keywords, ai_keywords) == 0.0
    assert crosscut_bonus(hc_only, hc_keywords, ai_keywords) == 0.0


def test_crosscut_bonus_lifts_total_score() -> None:
    # Arrange: two items equal on recency/authority; only one bridges lanes.
    config = ScoringConfig(
        weights=ScoringWeights(w_recency=0.25, w_authority=0.25, w_relevance=0.25, w_crosscut=0.25),
        recency_hours=48.0,
        owner_topic_profile=("machine learning",),
        hc_keywords=("hospital",),
        ai_keywords=("machine learning",),
    )
    bridging = make_item(title="Hospital adopts machine learning", url="https://a.com/1")
    single = make_item(title="Startup ships machine learning app", url="https://a.com/2")

    # Act
    bridging_score = score_item(bridging, config, now=NOW)
    single_score = score_item(single, config, now=NOW)

    # Assert: the cross-cutting item wins purely on the bonus.
    assert bridging_score.crosscut == 1.0
    assert single_score.crosscut == 0.0
    assert bridging_score.total > single_score.total


# ---------------------------------------------------------------------------
# Top-K lane-balanced selection
# ---------------------------------------------------------------------------


def _lane_balance_items() -> list[Item]:
    """Four HC items (denser in owner themes → higher-scoring) + four distinct AI
    items. Titles are deliberately dissimilar so item-level dedup keeps all eight.
    """
    hc = [
        make_item(
            title="Hospital automates its revenue cycle billing",
            url="https://hc.com/1",
            lane="hc",
            summary="A health system cut manual work in claims and revenue cycle.",
        ),
        make_item(
            title="Clinical documentation workflow gets faster",
            url="https://hc.com/2",
            lane="hc",
            summary="Care teams save time on clinical notes with automation.",
        ),
        make_item(
            title="Health network reduces claims denials",
            url="https://hc.com/3",
            lane="hc",
            summary="Automation and analytics trimmed denials for the hospital.",
        ),
        make_item(
            title="New analytics dashboard for care operations",
            url="https://hc.com/4",
            lane="hc",
            summary="Data and analytics give clinical operations fresh insight.",
        ),
    ]
    ai = [
        make_item(
            title="Researchers publish a new benchmark suite",
            url="https://ai.com/1",
            lane="ai",
            summary="A group released an evaluation benchmark.",
        ),
        make_item(
            title="Open weights release lands this week",
            url="https://ai.com/2",
            lane="ai",
            summary="A lab shipped downloadable weights.",
        ),
        make_item(
            title="Compiler speeds up training loops",
            url="https://ai.com/3",
            lane="ai",
            summary="An engineering post about faster kernels.",
        ),
        make_item(
            title="Startup demos a coding assistant",
            url="https://ai.com/4",
            lane="ai",
            summary="A demo of a developer productivity tool.",
        ),
    ]
    return hc + ai


def test_select_top_lane_balance_represents_both_lanes() -> None:
    # Arrange: HC items score higher, but we want a blended pick.
    items = _lane_balance_items()

    # Act: no session → cross-day suppression skipped; deterministic NOW.
    result = select_top(items, k=4, per_lane_balance=True, now=NOW, config=ScoringConfig())

    # Assert: 4 selected, split evenly across both lanes.
    lanes = [i.lane for i in result.selected]
    assert len(result.selected) == 4
    assert lanes.count("hc") == 2
    assert lanes.count("ai") == 2
    assert result.rationale["lane_counts"] == {"hc": 2, "ai": 2}


def test_select_top_without_balance_can_be_single_lane() -> None:
    # Arrange: same items; balance OFF should let the stronger lane dominate.
    items = _lane_balance_items()

    # Act
    result = select_top(items, k=4, per_lane_balance=False, now=NOW, config=ScoringConfig())

    # Assert: HC items outscore AI, so the top 4 are all HC.
    assert {i.lane for i in result.selected} == {"hc"}


def test_select_top_marks_selected_items() -> None:
    # Arrange
    items = _lane_balance_items()

    # Act
    result = select_top(items, k=2, per_lane_balance=True, now=NOW, config=ScoringConfig())

    # Assert: chosen items flagged, others left False (persistence side effect).
    for item in result.selected:
        assert item.selected is True
    unselected = [i for i in items if i not in result.selected]
    assert all(i.selected is not True for i in unselected)


# ---------------------------------------------------------------------------
# Cross-day suppression (session-using)
# ---------------------------------------------------------------------------


def _seed_used_item(session: Session, *, url: str, draft_age_days: int) -> Item:
    """Persist an Item + a Draft that referenced it ``draft_age_days`` ago."""
    run = Run(status="ok")
    session.add(run)
    session.flush()
    used = Item(
        run_id=run.id,
        lane="hc",
        title="Previously covered story",
        url=url,
        published_at=NOW - timedelta(days=draft_age_days),
    )
    session.add(used)
    session.flush()  # assign the UUID we reference below
    draft = Draft(
        run_id=run.id,
        post_text="An earlier post.",
        source_item_ids=[str(used.id)],
        state="published",
        created_at=NOW - timedelta(days=draft_age_days),
    )
    session.add(draft)
    session.commit()
    return used


def test_suppress_recently_used_drops_item_from_recent_draft(db_session: Session) -> None:
    # Arrange: a story used in a draft 2 days ago; a fresh re-fetch of it today
    # arrives under the same URL but as a brand-new (unpersisted) Item.
    _seed_used_item(db_session, url="https://news.com/story-x", draft_age_days=2)
    refetch = make_item(
        title="Today's rewrite of the same story",
        url="https://news.com/story-x",
        summary="Same underlying story, new wording.",
    )
    novel = make_item(title="A fresh story", url="https://news.com/story-y")

    # Act
    result = suppress_recently_used([refetch, novel], db_session, days=14, now=NOW)

    # Assert: the re-fetch is suppressed by URL identity; the novel item survives.
    assert novel in result.kept
    assert refetch not in result.kept
    assert any(reason.startswith("recently-used") for _, reason in result.removed)


def test_suppress_recently_used_ignores_old_drafts(db_session: Session) -> None:
    # Arrange: the same story, but the draft that used it is 20 days old (> window).
    _seed_used_item(db_session, url="https://news.com/story-z", draft_age_days=20)
    refetch = make_item(title="Same story again", url="https://news.com/story-z")

    # Act
    result = suppress_recently_used([refetch], db_session, days=14, now=NOW)

    # Assert: outside the 14-day window → not suppressed.
    assert refetch in result.kept
    assert result.removed == []


def test_select_top_applies_cross_day_suppression(db_session: Session) -> None:
    # Arrange: one candidate was used recently; two are fresh.
    _seed_used_item(db_session, url="https://news.com/dup", draft_age_days=1)
    used_again = make_item(title="Recycled", url="https://news.com/dup", lane="hc")
    fresh_hc = make_item(title="Fresh HC hospital story", url="https://news.com/hc", lane="hc")
    fresh_ai = make_item(title="Fresh AI model story", url="https://news.com/ai", lane="ai")

    # Act: session provided → suppression runs inside the funnel.
    result = select_top(
        [used_again, fresh_hc, fresh_ai],
        k=3,
        per_lane_balance=True,
        session=db_session,
        now=NOW,
        config=ScoringConfig(),
    )

    # Assert: the recycled item is suppressed and never selected.
    assert used_again not in result.selected
    assert result.rationale["suppressed_cross_day"] >= 1
    assert fresh_hc in result.selected
    assert fresh_ai in result.selected
