"""Source-seed loading and idempotent upsert into the ``sources`` table (§11.1).

WHY this module exists: BRD §22 mandates *config over code* — the owner curates
feeds in a file (``prep/sources_seed.yaml``), never in Python. This module reads
that seed and reconciles it into the DB ``sources`` table so a fresh checkout can
be provisioned, and re-running the seed after an edit updates existing rows
*without* creating duplicates or clobbering runtime feed-health state.

Two responsibilities:
  * :func:`seed_sources` — load the YAML and upsert it (idempotent by name).
  * :func:`get_enabled_sources` — the read helper the daily ingest uses to pull
    the currently-enabled sources (optionally filtered by lane).

Reuses the existing ORM (``vision.db.models.Source``) — this module never
redefines the schema, per the task constraint.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from vision.db.models import Source
from vision.logging_setup import get_logger

_log = get_logger(__name__)

# Default location of the curated seed, resolved relative to this file so it
# works regardless of the process CWD: src/vision/ingest/sources.py -> parents
# [0]=ingest, [1]=vision, [2]=src, [3]=repo root, then /prep/sources_seed.yaml.
# Kept as a module default (overridable per call) rather than hard-coded inline,
# so tests can point at a fixture file (config over code, BRD §22).
DEFAULT_SEED_PATH = Path(__file__).resolve().parents[3] / "prep" / "sources_seed.yaml"

# Fields on a seed row that the DB mirrors. ``last_ok_at`` is deliberately NOT
# here: it is runtime feed-health state (§17) owned by the fetcher, and a re-seed
# must never reset it.
_UPSERTABLE_FIELDS = ("lane", "kind", "url", "authority_weight", "enabled")


@dataclass(frozen=True)
class SeedSource:
    """One source as declared in the YAML seed.

    A typed, immutable view over a raw YAML dict so the rest of the module works
    against attributes (with validated defaults) instead of loose ``dict.get``
    calls. ``verify`` is a seed-only hint (health-check on first run) and is not
    persisted — it lives here only so loading doesn't choke on the extra key.
    """

    name: str
    lane: str
    kind: str
    url: str
    authority_weight: float = 0.5  # default trust weight if the seed omits it
    enabled: bool = True  # default to enabled unless the seed says otherwise
    verify: bool = False  # seed-only: flag to health-check this feed first run


def _coerce_seed_row(row: dict[str, Any]) -> SeedSource:
    """Turn one raw YAML mapping into a validated SeedSource.

    Raises a clear ValueError if a mandatory identity field is missing, so a
    malformed seed fails loudly at load time (BRD §22) rather than inserting a
    junk row that breaks ingestion later.
    """
    # ``name`` is the natural key for the upsert; ``lane``/``kind``/``url`` are
    # the minimum needed to actually fetch the source. Missing any is fatal.
    for required in ("name", "lane", "kind", "url"):
        if not row.get(required):
            raise ValueError(f"seed source missing required field '{required}': {row!r}")

    return SeedSource(
        name=str(row["name"]),
        lane=str(row["lane"]),
        kind=str(row["kind"]),
        url=str(row["url"]),
        # authority_weight is optional; float() gives an early, clear error on a
        # non-numeric value instead of a confusing DB failure downstream.
        authority_weight=float(row.get("authority_weight", 0.5)),
        enabled=bool(row.get("enabled", True)),
        verify=bool(row.get("verify", False)),
    )


def load_seed(path: Path | None = None) -> list[SeedSource]:
    """Load and flatten the YAML seed into a list of SeedSource records.

    The seed groups sources under lane keys (``hc_lane``, ``ai_lane``,
    ``crosscut_lane``, …). We iterate *every* top-level value that is a list, so
    new lane groups can be added to the file without touching this code (config
    over code). Order within/across groups is preserved for deterministic seeding.

    Raises:
        FileNotFoundError: if the seed file does not exist (surfaced, not hidden).
        ValueError: if the YAML is not a mapping or a row is malformed.
    """
    seed_path = path or DEFAULT_SEED_PATH
    if not seed_path.exists():
        raise FileNotFoundError(f"sources seed not found: {seed_path}")

    # ``safe_load`` (never ``load``) — the seed is trusted config, but safe_load
    # avoids any arbitrary-object construction on principle (security, §22).
    with seed_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    if not isinstance(document, dict):
        raise ValueError(f"seed must be a mapping of lane->list, got {type(document).__name__}")

    seeds: list[SeedSource] = []
    for group_name, group in document.items():
        # Skip any non-list top-level value (e.g. a stray scalar/comment block);
        # only lane groups (lists of source dicts) are meaningful here.
        if not isinstance(group, list):
            _log.debug("skipping non-list seed group '%s'", group_name)
            continue
        for row in group:
            if isinstance(row, dict):
                seeds.append(_coerce_seed_row(row))
    return seeds


def upsert_sources(session: Session, seeds: list[SeedSource]) -> dict[str, int]:
    """Upsert seed rows into ``sources``, idempotent by ``name``.

    For each seed: if a Source with that name exists, update its mutable config
    fields (lane/kind/url/authority_weight/enabled) in place; otherwise insert a
    new row. ``last_ok_at`` is never touched, so re-seeding after an edit keeps
    live feed-health intact. Returns counts for logging/observability.

    The caller owns the transaction boundary (``with get_session() as s: ...``),
    so this function flushes but does not commit — keeping it composable inside a
    larger unit of work.
    """
    inserted = 0
    updated = 0

    for seed in seeds:
        # Look the row up by its natural key. One query per seed keeps the logic
        # obvious; the seed list is tiny (tens of feeds), so this is not hot.
        existing = session.execute(
            select(Source).where(Source.name == seed.name)
        ).scalar_one_or_none()

        if existing is None:
            # New source: insert with all config fields; last_ok_at stays NULL
            # until the fetcher first succeeds.
            session.add(
                Source(
                    name=seed.name,
                    lane=seed.lane,
                    kind=seed.kind,
                    url=seed.url,
                    authority_weight=seed.authority_weight,
                    enabled=seed.enabled,
                )
            )
            inserted += 1
        else:
            # Existing source: reconcile only the config fields declared in the
            # seed. SQLAlchemy tracks these attribute writes as an UPDATE — this
            # is the ORM's intended change pattern, not a mutation of plain data.
            for attr in _UPSERTABLE_FIELDS:
                setattr(existing, attr, getattr(seed, attr))
            updated += 1

    # Flush so INSERTs get PKs and any constraint issues surface now, within the
    # caller's transaction, rather than at an unrelated later commit.
    session.flush()
    _log.info("seeded sources: %d inserted, %d updated", inserted, updated)
    return {"inserted": inserted, "updated": updated, "total": len(seeds)}


def seed_sources(session: Session, path: Path | None = None) -> dict[str, int]:
    """Load the YAML seed and upsert it — the one-call provisioning entry point."""
    seeds = load_seed(path)
    return upsert_sources(session, seeds)


def get_enabled_sources(session: Session, lane: str | None = None) -> list[Source]:
    """Return enabled sources, optionally filtered to a single lane.

    Ordered by ``authority_weight`` descending so the most trusted feeds are
    naturally fetched/considered first. This is the read helper the daily ingest
    uses to know *what* to fetch — enabled-only, so a feed the owner toggled off
    is excluded without any code change (config over code, §12.2).
    """
    query = select(Source).where(Source.enabled.is_(True))
    if lane is not None:
        # Lane filter lets the pipeline fetch the 'hc' and 'ai' lanes separately
        # (BRD §12.2 two-lane model) when it wants to balance the blend.
        query = query.where(Source.lane == lane)
    query = query.order_by(Source.authority_weight.desc())
    # ``.scalars().all()`` returns model instances; wrap in list for a concrete,
    # index-able result the caller can reason about.
    return list(session.execute(query).scalars().all())
