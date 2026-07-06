"""Tests for own-post dedup memory (BRD §11.5, FR-18).

All tests follow AAA (Arrange -> Act -> Assert), use the hermetic in-memory
SQLite ``db_session`` fixture from conftest, and touch no network or external
model — the whole feature is local-only by design, so nothing needs mocking.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vision.curate.own_dedup import check_against_own, record_own_post, similarity

# A realistic owner post about the same topic re-used across near-duplicate tests.
_ORIGINAL = (
    "New study shows AI models can predict protein folding structures faster "
    "than traditional lab methods, accelerating drug discovery pipelines."
)


def _record(session, text: str, urn: str, *, days_ago: int) -> None:
    """Helper: store one own post published ``days_ago`` days before now.

    Keeps each test's Arrange step to a single intention-revealing line rather
    than repeating the timestamp arithmetic inline.
    """
    published_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    record_own_post(
        session,
        draft_id=None,
        post_urn=urn,
        post_text=text,
        published_at=published_at,
    )


def test_near_duplicate_exceeds_threshold_and_fails(db_session) -> None:
    # Arrange: the owner already posted the original within the window.
    _record(db_session, _ORIGINAL, "urn:li:share:orig", days_ago=5)
    near_duplicate = (
        "A new study shows AI models predict protein folding structures far "
        "faster than traditional lab methods, accelerating drug discovery."
    )

    # Act: check a lightly reworded version of that same post.
    result = check_against_own(db_session, near_duplicate, threshold=0.80)

    # Assert: it is caught as a duplicate (does not pass) and points at the prior.
    assert result["max_similarity"] >= 0.80
    assert result["pass"] is False
    assert result["nearest_urn"] == "urn:li:share:orig"


def test_distinct_text_passes(db_session) -> None:
    # Arrange: the same AI/protein post sits in the window.
    _record(db_session, _ORIGINAL, "urn:li:share:orig", days_ago=10)
    unrelated = (
        "Quarterly hospital reimbursement rules are shifting as payers adopt "
        "value-based contracts, reshaping revenue-cycle operations for clinics."
    )

    # Act: check a topically unrelated candidate.
    result = check_against_own(db_session, unrelated, threshold=0.80)

    # Assert: low similarity -> the candidate is novel enough to publish.
    assert result["max_similarity"] < 0.80
    assert result["pass"] is True


def test_post_older_than_window_is_excluded(db_session) -> None:
    # Arrange: an all-but-identical post, but published 120 days ago (> 90-day window).
    _record(db_session, _ORIGINAL, "urn:li:share:stale", days_ago=120)

    # Act: check the exact original text with the default 90-day window.
    result = check_against_own(db_session, _ORIGINAL, days=90, threshold=0.80)

    # Assert: the stale post is outside the window, so nothing matches and it passes.
    assert result["max_similarity"] == 0.0
    assert result["pass"] is True
    assert result["nearest_urn"] is None


def test_empty_history_passes(db_session) -> None:
    # Arrange: no own posts recorded at all.

    # Act: check any candidate against an empty memory.
    result = check_against_own(db_session, _ORIGINAL, threshold=0.80)

    # Assert: empty history is always novel.
    assert result["max_similarity"] == 0.0
    assert result["pass"] is True
    assert result["nearest_urn"] is None


def test_similarity_symmetry_and_bounds(db_session) -> None:
    # Arrange: two unrelated texts and one identical pair.

    # Act: compute pairwise similarities directly (no DB needed).
    identical = similarity(_ORIGINAL, _ORIGINAL)
    reversed_order = similarity("alpha beta gamma", "gamma beta alpha")
    unrelated = similarity("protein folding ai models", "hospital payer contracts")

    # Assert: self-similarity is 1.0, order-independent, and unrelated text is low.
    assert identical == 1.0
    assert reversed_order == 1.0
    assert unrelated < 0.2
