"""Own-post dedup memory (BRD §11.5, FR-18).

WHY this module exists: FR-18 forbids publishing a post that *semantically
duplicates one of the owner's own posts from the last 90 days*. Repeating
yourself on LinkedIn erodes credibility, so before a draft is approved it is
checked against a rolling 90-day memory of what the owner already said.

Design constraints (BRD §22):
  * **No API keys / no heavy deps.** We deliberately avoid remote embedding
    APIs and large local models. Instead each post is reduced to a *portable
    local vector* — an L2-normalised token-frequency dict — and similarity is a
    plain cosine over those vectors. This runs identically on a laptop and in
    CI with only the standard library.
  * **DB-agnostic storage.** The vector is persisted in the existing
    ``own_posts.embedding`` JSON column (see ``db/models.py``), which round-trips
    a Python ``dict`` on both SQLite (dev) and Postgres (prod).
  * **Prod upgrade path.** The production upgrade is real semantic embeddings
    stored in a ``pgvector`` column with indexed ANN search. Because the model
    attribute stays JSON-shaped and this module is the only place that builds or
    reads vectors, that swap is localised here — callers never change.

The token-frequency + cosine approach is adapted from finalert's keyword scorer
(``agents/sentiment_agent.py``) — same "cheap, deterministic, dependency-free
text signal" philosophy, re-implemented (not imported) for VISION.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from vision.config import get_settings
from vision.db.models import OwnPost
from vision.logging_setup import get_logger

# Module logger — structured JSON logging per §17/§22 (never ``print``).
_log = get_logger("vision.curate.own_dedup")

# A ``Vector`` here is a sparse term -> weight mapping. Sparse (dict) rather than
# a dense list because posts share little vocabulary, so most positions are zero;
# a dict skips them and needs no shared vocabulary/index to align two vectors.
Vector = dict[str, float]

# Word tokenizer: runs of alphanumerics, lower-cased by the caller. Kept simple
# and deterministic so the same text always yields the same vector.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Minimal English stopword set. WHY: high-frequency function words ("the", "and")
# co-occur in *every* post and would inflate the cosine between two otherwise
# unrelated posts — dragging distinct text toward a false-positive duplicate.
# Dropping them sharpens the signal for genuine near-duplicates. Adapted from the
# frozenset keyword style in finalert's sentiment_agent (adapted, not imported).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "has", "have", "in", "into", "is", "it", "its", "of", "on", "or", "our",
        "that", "the", "their", "them", "there", "this", "to", "was", "were",
        "will", "with", "we", "you", "your", "i", "my", "me", "so", "if", "not",
        "can", "how", "what", "why", "when", "which", "who", "about", "over",
    }
)


def _tokenize(text: str) -> list[str]:
    """Split ``text`` into lower-cased, stopword-filtered word tokens.

    Returns an empty list for empty/whitespace input — callers treat that as an
    all-zero vector (cosine 0 against anything), never as an error.
    """
    # Lower-case first so tokenization and stopword lookup are case-insensitive.
    return [tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS]


def _vectorize(text: str) -> Vector:
    """Convert ``text`` into an L2-normalised token-frequency vector.

    WHY L2-normalise at build time: once each stored vector has unit length,
    cosine similarity reduces to a dot product and every stored post is compared
    on equal footing regardless of length. ``_cosine`` still divides by the norms
    defensively, so this normalisation is a documented convenience, not a
    correctness dependency.
    """
    counts: dict[str, int] = {}
    for token in _tokenize(text):
        # Immutable-friendly accumulate: dict is built fresh in this call scope,
        # never a shared/mutated argument.
        counts[token] = counts.get(token, 0) + 1

    if not counts:
        # No informative tokens -> the zero vector (empty dict).
        return {}

    # L2 norm of the raw counts, then scale each weight to unit length.
    norm = math.sqrt(sum(count * count for count in counts.values()))
    return {token: count / norm for token, count in counts.items()}


def _cosine(vec_a: Vector, vec_b: Vector) -> float:
    """Return the cosine similarity in [0.0, 1.0] between two sparse vectors.

    Iterates the smaller vector for the dot product (fewer lookups) and returns
    ``0.0`` when either vector is empty — an empty post is defined to be similar
    to nothing.
    """
    if not vec_a or not vec_b:
        return 0.0

    # Dot product over the shared keys only; missing keys contribute zero.
    smaller, larger = (vec_a, vec_b) if len(vec_a) <= len(vec_b) else (vec_b, vec_a)
    dot = sum(weight * larger.get(token, 0.0) for token, weight in smaller.items())

    norm_a = math.sqrt(sum(w * w for w in vec_a.values()))
    norm_b = math.sqrt(sum(w * w for w in vec_b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    # Clamp to guard against tiny floating-point overshoot above 1.0.
    return min(1.0, dot / (norm_a * norm_b))


def similarity(a_text: str, b_text: str) -> float:
    """Cosine similarity between two raw texts via their local vectors.

    Public helper used both by ``check_against_own`` and by callers that want a
    direct text-to-text comparison without touching the database.
    """
    return _cosine(_vectorize(a_text), _vectorize(b_text))


def record_own_post(
    session: Session,
    draft_id: uuid.UUID | None,
    post_urn: str | None,
    post_text: str,
    published_at: datetime,
) -> OwnPost:
    """Persist a published post into the 90-day dedup memory.

    Stores the post's local vector in ``own_posts.embedding`` so future
    candidates can be checked without re-vectorising history. Returns the flushed
    ``OwnPost`` (its id is available) but does NOT commit — transaction ownership
    stays with the caller / surrounding run, matching the rest of the pipeline.
    """
    own_post = OwnPost(
        draft_id=draft_id,
        post_urn=post_urn,
        post_text=post_text,
        # Persist the sparse vector as JSON; pgvector is the prod upgrade (docstring).
        embedding=_vectorize(post_text),
        published_at=published_at,
    )
    session.add(own_post)
    # Flush (not commit) so the row gets an id and is visible to subsequent
    # queries in the same transaction, while the caller controls the commit.
    session.flush()

    _log.info(
        "Recorded own post into dedup memory",
        extra={"own_post_id": str(own_post.id), "post_urn": post_urn},
    )
    return own_post


def check_against_own(
    session: Session,
    candidate_text: str,
    days: int = 90,
    threshold: float | None = None,
) -> dict[str, object]:
    """Check ``candidate_text`` against the owner's own posts from the last ``days``.

    Args:
        session: Active DB session (SQLite dev / Postgres prod — same code).
        candidate_text: The draft text about to be published.
        days: Rolling look-back window; FR-18 mandates 90.
        threshold: Similarity at/above which the candidate is a duplicate. When
            ``None`` it resolves to ``settings.dedup_sim_threshold`` (Appendix-A
            ``DEDUP_SIM_THRESHOLD``) so the gate is config-over-code (§22).

    Returns:
        A plain dict:
          * ``max_similarity`` (float): highest cosine against any in-window post.
          * ``pass`` (bool): True when ``max_similarity`` is strictly below the
            threshold, i.e. the candidate is sufficiently novel to publish.
          * ``nearest_urn`` (str | None): URN of the closest prior post (the one
            that produced ``max_similarity``), or None when history is empty.
    """
    # Resolve the threshold from settings at call time so env changes take effect
    # without re-importing the module.
    effective_threshold = (
        get_settings().dedup_sim_threshold if threshold is None else threshold
    )

    # Timezone-aware cutoff so the window is correct across the SQLite->Postgres
    # move (all timestamps are stored tz-aware, per base.TimestampMixin).
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Only fetch posts inside the window — older posts are irrelevant to FR-18 and
    # excluding them in SQL keeps the comparison set small.
    stmt = select(OwnPost).where(OwnPost.published_at >= cutoff)
    recent_posts = session.scalars(stmt).all()

    candidate_vec = _vectorize(candidate_text)

    max_similarity = 0.0
    nearest_urn: str | None = None
    for prior in recent_posts:
        # Prefer the stored vector; fall back to re-vectorising the text if an
        # older row predates the embedding column (defensive, no bare except).
        stored_vec: Vector = prior.embedding or _vectorize(prior.post_text or "")
        score = _cosine(candidate_vec, stored_vec)
        if score > max_similarity:
            max_similarity = score
            nearest_urn = prior.post_urn

    # Novel iff strictly below the threshold; equal-to-threshold counts as a dup
    # (fail-closed for the dedup gate).
    passed = max_similarity < effective_threshold

    _log.info(
        "Own-post dedup check complete",
        extra={
            "max_similarity": round(max_similarity, 4),
            "threshold": effective_threshold,
            "pass": passed,
            "window_days": days,
            "candidates_in_window": len(recent_posts),
        },
    )
    return {
        "max_similarity": max_similarity,
        "pass": passed,
        "nearest_urn": nearest_urn,
    }
