"""Top-K candidate selection for the daily draft (BRD §12.3/§12.4, FR-04).

This is the CURATE layer's orchestrator: it runs the full funnel and returns the
handful of items the synthesis engine will actually write about.

    dedup (pure) → cross-day suppress (optional, session) → score → rank →
    lane-balanced top-K → mark ``Item.selected`` → rationale

Why lane balance (BRD §13.2/§12.2)
----------------------------------
The daily post is a *blended* HC × AI piece, so the selection must not collapse
onto whichever lane happened to score higher today. ``per_lane_balance`` picks by
alternating the lane with the fewest picks so far (ties broken by score),
guaranteeing both lanes are represented whenever both have candidates.

Side effects are explicit + minimal (§22): the only mutation is marking the
chosen ``Item`` rows ``selected=True`` and writing their ``relevance_score`` — the
persistence FR-04 requires. A ``rationale`` dict is returned for the approval
email's transparency (§14.1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from vision.curate.dedup import (
    DEFAULT_SIM_THRESHOLD,
    DEFAULT_SUPPRESS_DAYS,
    deduplicate,
    suppress_recently_used,
)
from vision.curate.score import (
    ScoreBreakdown,
    ScoringConfig,
    apply_scores,
    score_items,
)

logger = logging.getLogger(__name__)

# Lanes the balancer actively interleaves (BRD's two content lanes).
_BALANCED_LANES: tuple[str, ...] = ("hc", "ai")


@dataclass(frozen=True)
class SelectionResult:
    """Everything a caller needs after selection — chosen items + full rationale.

    Frozen so the record is a stable audit artefact. ``rationale`` is JSON-safe
    (plain types) so it can drop straight into ``runs.stats`` / the approval email.
    """

    selected: list[Any]
    rationale: dict[str, Any]
    removed: list[tuple[Any, str]] = field(default_factory=list)
    suppressed: list[tuple[Any, str]] = field(default_factory=list)


def _lane_of(item: Any) -> str:
    """Return the item's lane string ('hc'|'ai'|other), lower-cased + safe."""
    return str(getattr(item, "lane", "") or "").lower()


def _pick_lane_balanced(
    ranked: list[ScoreBreakdown],
    k: int,
) -> list[ScoreBreakdown]:
    """Select ``k`` breakdowns keeping the two lanes as balanced as possible.

    Algorithm (greedy, deterministic):
      * bucket the already-score-ranked breakdowns into hc / ai / other queues,
        each still in descending-score order;
      * repeatedly take the next item from the *balanced* lane — the one of
        hc/ai with the fewest picks so far; ties (equal counts) go to whichever
        lane's next candidate has the higher score;
      * when one lane runs dry, the loop keeps drawing from the other (min-count
        naturally selects the only non-empty lane), so we still fill to K;
      * any ``other``-lane items backfill last, purely by score, if K is unmet.

    This guarantees: if both lanes have candidates and K ≥ 2, both are represented.
    """
    # Partition preserving the incoming (descending-score) order within each lane.
    queues: dict[str, list[ScoreBreakdown]] = {"hc": [], "ai": []}
    other: list[ScoreBreakdown] = []
    for breakdown in ranked:
        lane = _lane_of(breakdown.item)
        if lane in queues:
            queues[lane].append(breakdown)
        else:
            other.append(breakdown)

    selected: list[ScoreBreakdown] = []
    cursor = {"hc": 0, "ai": 0}  # how far we've consumed each lane's queue
    counts = {"hc": 0, "ai": 0}  # how many we've picked per lane (for balance)

    while len(selected) < k:
        # Lanes that still have unconsumed candidates.
        available = [lane for lane in _BALANCED_LANES if cursor[lane] < len(queues[lane])]
        if not available:
            break  # both hc/ai exhausted → fall through to `other` backfill

        # Prefer the lane with the fewest picks; break ties by the next item's
        # score so we never pass over a clearly stronger candidate for balance.
        chosen_lane = min(
            available,
            key=lambda lane: (counts[lane], -queues[lane][cursor[lane]].total),
        )
        selected.append(queues[chosen_lane][cursor[chosen_lane]])
        cursor[chosen_lane] += 1
        counts[chosen_lane] += 1

    # Backfill any shortfall from non-hc/ai items, strongest first.
    if len(selected) < k and other:
        selected.extend(other[: k - len(selected)])

    return selected


def select_top(
    items: list[Any],
    k: int,
    *,
    config: ScoringConfig | None = None,
    per_lane_balance: bool = True,
    session: Session | None = None,
    now: datetime | None = None,
    dedup: bool = True,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
    suppress_days: int = DEFAULT_SUPPRESS_DAYS,
    mark_selected: bool = True,
) -> SelectionResult:
    """Run the CURATE funnel and return the top ``k`` items + a rationale.

    Pipeline:
      1. **dedup** (pure, in-batch) — exact URL / content-hash / fuzzy title.
      2. **cross-day suppress** — only when a ``session`` is supplied; drops items
         used in a draft within ``suppress_days`` (BRD §12.4).
      3. **score** — BRD §12.3 weighted recency/authority/relevance/cross-cut.
      4. **rank** — descending total; ``per_lane_balance`` interleaves hc/ai.
      5. **mark** — set ``selected=True`` + persist ``relevance_score`` on the
         chosen ORM items (skippable via ``mark_selected=False``).

    Args:
        items: today's ingested candidates.
        k: number of items to select for the draft.
        config: scoring config; defaults to ``ScoringConfig.load()`` (env/prep).
        per_lane_balance: interleave hc/ai so both lanes are represented.
        session: DB session enabling cross-day suppression (omit to skip it).
        now: reference time for recency + suppression (injectable for tests).
        dedup: run item-level dedup first (default True).
        sim_threshold: fuzzy-title threshold shared by dedup + suppression.
        suppress_days: cross-day suppression window.
        mark_selected: persist selection onto the item rows (default True).

    Returns:
        A ``SelectionResult`` with the chosen items, a JSON-safe rationale, and
        the removed/suppressed audit trails.
    """
    scoring_config = config or ScoringConfig.load()

    # --- 1. Item-level dedup (pure) ---------------------------------------
    removed: list[tuple[Any, str]] = []
    working = items
    if dedup:
        dedup_result = deduplicate(working, sim_threshold=sim_threshold)
        working = dedup_result.kept
        removed = dedup_result.removed

    # --- 2. Cross-day suppression (needs the DB) --------------------------
    suppressed: list[tuple[Any, str]] = []
    if session is not None:
        suppress_result = suppress_recently_used(
            working,
            session,
            days=suppress_days,
            now=now,
            sim_threshold=sim_threshold,
        )
        working = suppress_result.kept
        suppressed = suppress_result.removed

    # --- 3. Score the survivors (pure) ------------------------------------
    breakdowns = score_items(working, scoring_config, now=now)

    # Persist relevance_score for ALL scored survivors (§11.2 computed column),
    # independent of whether they make the final cut.
    if mark_selected:
        apply_scores(breakdowns)

    # --- 4. Rank + select --------------------------------------------------
    # Stable sort by descending total; equal totals keep input order for
    # determinism (important for reproducible tests + rationale).
    ranked = sorted(breakdowns, key=lambda b: b.total, reverse=True)
    chosen = (
        _pick_lane_balanced(ranked, k)
        if per_lane_balance
        else ranked[:k]
    )

    # --- 5. Mark selected items -------------------------------------------
    selected_items: list[Any] = [breakdown.item for breakdown in chosen]
    if mark_selected:
        for item in selected_items:
            if hasattr(item, "selected"):
                item.selected = True

    rationale = _build_rationale(
        chosen=chosen,
        total_in=len(items),
        removed=removed,
        suppressed=suppressed,
        per_lane_balance=per_lane_balance,
        k=k,
    )
    logger.info(
        "selection complete",
        extra={
            "in": len(items),
            "selected": len(selected_items),
            "removed": len(removed),
            "suppressed": len(suppressed),
        },
    )
    return SelectionResult(
        selected=selected_items,
        rationale=rationale,
        removed=removed,
        suppressed=suppressed,
    )


def _build_rationale(
    *,
    chosen: list[ScoreBreakdown],
    total_in: int,
    removed: list[tuple[Any, str]],
    suppressed: list[tuple[Any, str]],
    per_lane_balance: bool,
    k: int,
) -> dict[str, Any]:
    """Assemble a JSON-safe explanation of the selection for the approval email.

    Captures the funnel counts, the per-lane balance actually achieved, and a
    per-item score breakdown so the owner can see *why* each item was chosen
    (transparency, §14.1) and so ``runs.stats`` has an auditable record (§17).
    """
    lane_counts: dict[str, int] = {}
    picks: list[dict[str, Any]] = []
    for breakdown in chosen:
        lane = _lane_of(breakdown.item)
        lane_counts[lane] = lane_counts.get(lane, 0) + 1
        picks.append(
            {
                "title": getattr(breakdown.item, "title", None),
                "url": getattr(breakdown.item, "url", None),
                "lane": lane,
                "score": round(breakdown.total, 4),
                "components": {
                    "recency": round(breakdown.recency, 4),
                    "authority": round(breakdown.authority, 4),
                    "relevance": round(breakdown.relevance, 4),
                    "crosscut": round(breakdown.crosscut, 4),
                },
            }
        )
    return {
        "requested_k": k,
        "selected_count": len(chosen),
        "candidates_in": total_in,
        "removed_dedup": len(removed),
        "suppressed_cross_day": len(suppressed),
        "per_lane_balance": per_lane_balance,
        "lane_counts": lane_counts,
        "picks": picks,
    }
