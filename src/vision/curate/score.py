"""Relevance/recency/authority/cross-cut scoring for CURATE (BRD §12.3, FR-04).

The scoring formula (verbatim from BRD §12.3)
---------------------------------------------
    score = w_recency   * recency(published_at)
          + w_authority * source.authority_weight
          + w_relevance * semantic_relevance(item, owner_topic_profile)
          + w_crosscut  * bonus_if_bridges_HC_and_AI

Every component is normalised to 0..1 and the weights sum to 1.0 by default, so
the total is also 0..1 and directly comparable across items and days.

Design choices, mapped to the BRD + task spec
---------------------------------------------
* **recency** — exponential decay over ``RECENCY_HOURS`` (config). At age == the
  half-life-ish constant the score is e⁻¹ ≈ 0.368; brand-new items score ~1.0,
  stale items tend to 0. Exponential (not linear) matches the intuition that
  "yesterday vs today" matters far more than "day 9 vs day 10".
* **authority** — the source's ``authority_weight`` (0..1) straight from the
  ``sources`` table; config-over-code (owner tunes trust without code, §22.6).
* **relevance** — a *keyword / TF-overlap* proxy (no API, per the task): how much
  of the ``owner_topic_profile`` the item's text covers, modulated by term
  frequency. Deliberately dependency-free so scoring stays deterministic and
  offline (adapts the finalert ``sentiment_agent`` keyword-scorer idea).
* **cross-cut bonus** — the owner's niche is the *intersection* of HC and AI
  (§12.3), so an item whose text bridges BOTH lanes earns a flat bonus.

The pure ``score_item``/``score_items`` functions do NOT mutate inputs; a
separate ``apply_scores`` step writes ``Item.relevance_score`` when the caller
wants persistence (keeps scoring testable in isolation, §22.8).
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vision.config import Settings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default topic vocabularies (config-over-code: overridable via env / prep).
# Sourced from BRD §12.3 owner_topic_profile + the voice_profile.yaml niche.
# ---------------------------------------------------------------------------

# The owner's themes — used for the keyword/TF relevance overlap. Kept as plain
# lower-case phrases so multi-word themes ("revenue cycle") match via substring.
_DEFAULT_OWNER_TOPIC_PROFILE: tuple[str, ...] = (
    "healthcare operations",
    "hospital",
    "clinical",
    "revenue cycle",
    "claims",
    "digital health",
    "applied ai",
    "data",
    "analytics",
    "business intelligence",
    "interoperability",
    "patient",
    "medtech",
    "biotech",
    "pharma",
    "automation",
    "machine learning",
    "electronic health record",
    "telehealth",
    "diagnostics",
)

# HC-lane marker terms — presence signals the item touches healthcare.
_DEFAULT_HC_KEYWORDS: tuple[str, ...] = (
    "health",
    "healthcare",
    "clinical",
    "hospital",
    "patient",
    "medical",
    "medicine",
    "pharma",
    "biotech",
    "care",
    "ehr",
    "fda",
    "nurse",
    "physician",
    "diagnosis",
    "drug",
    "therapy",
    "clinic",
    "telehealth",
    "medicaid",
    "medicare",
)

# AI-lane marker terms — presence signals the item touches AI/technology.
_DEFAULT_AI_KEYWORDS: tuple[str, ...] = (
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "neural",
    "algorithm",
    "model",
    "llm",
    "gpt",
    "generative",
    "automation",
    "data science",
    "agent",
    "transformer",
    "foundation model",
)


@dataclass(frozen=True)
class ScoringWeights:
    """The four BRD §12.3 weights. Frozen so a config is safe to share/log.

    Defaults sum to 1.0 so the total score lands in 0..1. Weights are the primary
    tuning knob and are config-driven (see ``ScoringConfig.load``).
    """

    w_recency: float = 0.30
    w_authority: float = 0.25
    w_relevance: float = 0.30
    w_crosscut: float = 0.15


@dataclass(frozen=True)
class ScoringConfig:
    """All inputs the scorer needs, in one immutable, config-driven object.

    Grouping weights + recency window + vocabularies here (rather than passing a
    fistful of args) keeps ``score_item`` a clean pure function and gives one
    place to load overrides from env/prep (config-over-code, §22.6).
    """

    weights: ScoringWeights = field(default_factory=ScoringWeights)
    recency_hours: float = 48.0
    owner_topic_profile: tuple[str, ...] = _DEFAULT_OWNER_TOPIC_PROFILE
    hc_keywords: tuple[str, ...] = _DEFAULT_HC_KEYWORDS
    ai_keywords: tuple[str, ...] = _DEFAULT_AI_KEYWORDS

    @classmethod
    def load(cls, settings: Settings | None = None) -> "ScoringConfig":
        """Build a config from ``Settings`` + optional env-var weight overrides.

        ``recency_hours`` comes straight from the validated ``Settings`` (env/.env).
        Each weight may be overridden with a ``W_RECENCY`` / ``W_AUTHORITY`` /
        ``W_RELEVANCE`` / ``W_CROSSCUT`` env var so the owner can retune scoring
        without touching code. Owner-topic keywords may be supplied as a
        comma-separated ``OWNER_TOPIC_PROFILE`` env var; otherwise the BRD default
        vocabulary is used.
        """
        resolved = settings or get_settings()

        # Each weight: env override if present + parseable, else the class default.
        def _weight(env_key: str, default: float) -> float:
            raw = os.environ.get(env_key)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError:
                # A malformed override must not silently corrupt scoring; log and
                # fall back to the safe default (fail-visible, not fail-loud here
                # because a bad tuning knob shouldn't crash the daily run).
                logger.warning("ignoring non-numeric weight override", extra={"env": env_key})
                return default

        weights = ScoringWeights(
            w_recency=_weight("W_RECENCY", ScoringWeights.w_recency),
            w_authority=_weight("W_AUTHORITY", ScoringWeights.w_authority),
            w_relevance=_weight("W_RELEVANCE", ScoringWeights.w_relevance),
            w_crosscut=_weight("W_CROSSCUT", ScoringWeights.w_crosscut),
        )

        # Owner topic profile: optional comma-separated env override.
        profile_env = os.environ.get("OWNER_TOPIC_PROFILE")
        owner_profile = (
            tuple(term.strip().lower() for term in profile_env.split(",") if term.strip())
            if profile_env
            else _DEFAULT_OWNER_TOPIC_PROFILE
        )

        return cls(
            weights=weights,
            recency_hours=float(resolved.recency_hours),
            owner_topic_profile=owner_profile,
        )


@dataclass(frozen=True)
class ScoreBreakdown:
    """Per-item score components + weighted total, with the item for reference.

    Returning the breakdown (not just a number) makes the selection rationale
    honest (§14.1) and makes each component independently assertable in tests.
    """

    item: Any
    recency: float
    authority: float
    relevance: float
    crosscut: float
    total: float


# ---------------------------------------------------------------------------
# Component scorers — each returns a value in 0..1.
# ---------------------------------------------------------------------------


def _ensure_aware(moment: datetime) -> datetime:
    """Coerce a naive datetime to UTC-aware (ingest stores UTC); avoids TypeError."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def recency_score(
    published_at: datetime | None,
    now: datetime,
    recency_hours: float,
) -> float:
    """Exponential-decay recency in 0..1 (BRD §12.3 ``recency(published_at)``).

    ``score = exp(-age_hours / recency_hours)``:
      * age 0h            → 1.0 (fresh)
      * age recency_hours → e⁻¹ ≈ 0.368 (the configured "old" reference point)
      * age → ∞           → 0.0 (stale)

    Guards:
      * a missing ``published_at`` scores 0.0 — we cannot claim freshness we can't
        prove (fail-safe, precision principle NFR-02);
      * a future timestamp (clock skew / bad feed) is clamped to age 0 → 1.0.
    """
    if published_at is None:
        return 0.0
    # Guard against a non-positive window that would divide-by-zero / invert sign.
    safe_hours = recency_hours if recency_hours > 0 else 1.0
    age_seconds = (_ensure_aware(now) - _ensure_aware(published_at)).total_seconds()
    age_hours = max(0.0, age_seconds / 3600.0)  # clamp future → 0
    return math.exp(-age_hours / safe_hours)


def _resolve_authority(item: Any) -> float:
    """Return the item's source authority weight (0..1), defaulting to 0.5.

    Looks first at a direct ``authority_weight`` attribute (lightweight test
    items / pre-joined rows), then at the related ``source.authority_weight`` (ORM
    ``Item``). A missing source yields the neutral 0.5 midpoint rather than
    penalising an item for a scoring gap.
    """
    direct = getattr(item, "authority_weight", None)
    if isinstance(direct, (int, float)):
        return float(direct)
    source = getattr(item, "source", None)
    source_weight = getattr(source, "authority_weight", None)
    if isinstance(source_weight, (int, float)):
        return float(source_weight)
    return 0.5


def _text_of(item: Any) -> str:
    """Concatenate an item's title + summary, lower-cased, for keyword matching."""
    title = getattr(item, "title", "") or ""
    summary = getattr(item, "summary", "") or ""
    return f"{title} {summary}".lower()


def _count_occurrences(text: str, phrase: str) -> int:
    """Count non-overlapping whole-token occurrences of ``phrase`` in ``text``.

    WHY word boundaries: a bare ``in`` check would count "ai" inside "email" or
    "maintain". We anchor on ``\\b`` so "ai" matches the token "ai" but not a
    substring — critical for the short AI-lane markers. Multi-word phrases match
    across a single space.
    """
    pattern = r"\b" + re.escape(phrase) + r"\b"
    return len(re.findall(pattern, text))


def semantic_relevance(item: Any, owner_topic_profile: tuple[str, ...] | list[str]) -> float:
    """Keyword/TF overlap of the item text with the owner's topic profile (0..1).

    Two signals, blended so coverage dominates but frequency still counts:
      * **coverage** = distinct profile terms present / total profile terms — the
        breadth of overlap with the owner's themes.
      * **freq_factor** = hits / (hits + |profile|) — a saturating term-frequency
        boost so an item repeatedly on-theme edges out a single passing mention,
        without letting keyword-stuffing run away.

    ``relevance = coverage * (0.5 + 0.5 * freq_factor)`` → 0..1, monotonic in both
    coverage and frequency, and 0.0 for an item with no thematic overlap.
    """
    if not owner_topic_profile:
        return 0.0
    text = _text_of(item)

    distinct_hits = 0
    total_hits = 0
    for term in owner_topic_profile:
        occurrences = _count_occurrences(text, term.lower())
        if occurrences:
            distinct_hits += 1
            total_hits += occurrences

    if distinct_hits == 0:
        return 0.0

    coverage = distinct_hits / len(owner_topic_profile)
    freq_factor = total_hits / (total_hits + len(owner_topic_profile))
    return coverage * (0.5 + 0.5 * freq_factor)


def crosscut_bonus(
    item: Any,
    hc_keywords: tuple[str, ...] | list[str],
    ai_keywords: tuple[str, ...] | list[str],
) -> float:
    """Return 1.0 if the item bridges BOTH lanes, else 0.0 (BRD §12.3 bonus).

    The owner's niche is the HC × AI *intersection*, so an item whose text
    contains at least one healthcare marker AND at least one AI marker is exactly
    the cross-cutting signal the pipeline should privilege.
    """
    text = _text_of(item)
    touches_hc = any(_count_occurrences(text, kw.lower()) for kw in hc_keywords)
    touches_ai = any(_count_occurrences(text, kw.lower()) for kw in ai_keywords)
    return 1.0 if (touches_hc and touches_ai) else 0.0


# ---------------------------------------------------------------------------
# Composite scoring.
# ---------------------------------------------------------------------------


def score_item(
    item: Any,
    config: ScoringConfig,
    now: datetime | None = None,
) -> ScoreBreakdown:
    """Compute the four components + the weighted BRD §12.3 total for one item.

    Pure: reads the item, returns a ``ScoreBreakdown``, mutates nothing. ``now``
    is injectable so recency is deterministic in tests; it defaults to current UTC.
    """
    reference_now = _ensure_aware(now) if now is not None else datetime.now(timezone.utc)
    weights = config.weights

    recency = recency_score(getattr(item, "published_at", None), reference_now, config.recency_hours)
    authority = _resolve_authority(item)
    relevance = semantic_relevance(item, config.owner_topic_profile)
    crosscut = crosscut_bonus(item, config.hc_keywords, config.ai_keywords)

    # Weighted sum — the verbatim BRD §12.3 formula.
    total = (
        weights.w_recency * recency
        + weights.w_authority * authority
        + weights.w_relevance * relevance
        + weights.w_crosscut * crosscut
    )
    return ScoreBreakdown(
        item=item,
        recency=recency,
        authority=authority,
        relevance=relevance,
        crosscut=crosscut,
        total=total,
    )


def score_items(
    items: list[Any],
    config: ScoringConfig,
    now: datetime | None = None,
) -> list[ScoreBreakdown]:
    """Score a batch (pure), returning breakdowns in the SAME order as ``items``.

    Ordering is preserved (not sorted) so the caller controls ranking/selection;
    selection lives in ``select.py`` to keep concerns separate.
    """
    return [score_item(item, config, now=now) for item in items]


def apply_scores(breakdowns: list[ScoreBreakdown]) -> None:
    """Persist each computed total onto ``Item.relevance_score`` (§11.2 column).

    Deliberately the ONLY mutating function here: keeping the write separate from
    the pure scorers means tests can assert scores without side effects, and the
    caller opts into persistence explicitly. Items that do not expose the column
    (non-ORM test doubles) are skipped rather than erroring.
    """
    for breakdown in breakdowns:
        if hasattr(breakdown.item, "relevance_score"):
            breakdown.item.relevance_score = breakdown.total
