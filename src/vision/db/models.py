"""SQLAlchemy ORM models for the VISION data model (BRD §11 + §13.6).

Every table carries a UUID PK and created_at/updated_at (via mixins) and uses
only portable column types (see base.py) so the identical schema runs on SQLite
in dev and PostgreSQL in prod. Each column is documented inline per BRD §22.

Tables:
  * sources      — configured feeds/APIs (§11.1)
  * items        — ingested raw signals (§11.2)
  * runs         — one row per daily execution (§11.3)
  * drafts       — candidate posts + state machine + image lane (§11.4 / §13.6)
  * own_posts    — dedup memory of the owner's published posts (§11.5)
  * oauth_tokens — encrypted LinkedIn tokens (§11.6)
  * audit_log    — append-only state-change log (§11.7)
  * used_tokens  — single-use nonces for approval links (§14.2)
  * alert_state  — durable last-fired ledger for alert dedup (§17, NFR-08)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vision.db.base import (
    ArrayType,
    Base,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VectorType,
)


class Source(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A configured content source — an RSS feed, API, or (rarely) a scrape target.

    Rows are toggled/curated by the owner over time without code changes
    (config-over-code, §12.2), which is why ``enabled`` and ``authority_weight``
    live in the DB rather than in code.
    """

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(Text, nullable=False, doc="Human label, e.g. 'STAT News'.")
    # Lane the source belongs to: 'hc' (life-sciences/healthcare) or 'ai'.
    lane: Mapped[str] = mapped_column(Text, nullable=False, doc="'hc' | 'ai' content lane.")
    # How the source is read; drives which ingestor handles it.
    kind: Mapped[str] = mapped_column(Text, nullable=False, doc="'rss' | 'api' | 'scrape'.")
    url: Mapped[str] = mapped_column(Text, nullable=False, doc="Feed/endpoint URL.")
    # Trust weight (0-1) folded into relevance scoring (§12.3).
    authority_weight: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5, doc="Source trust weight 0-1 for scoring."
    )
    # Lets the owner disable a noisy/broken feed without deleting history.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, doc="Toggle ingestion without code change."
    )
    # Feed-health tracking; ops alerts if a source is silent past a threshold (§17).
    last_ok_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Last successful fetch (feed-health)."
    )

    # Convenience navigation to the items captured from this source.
    items: Mapped[list["Item"]] = relationship(back_populates="source")


class Run(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per daily execution of the pipeline (§11.3).

    Declared before ``Item``/``Draft`` because both FK back to it; ``stats``
    captures counts/timings/token-usage/model-versions for observability (§17).
    """

    __tablename__ = "runs"

    # Overall outcome of the run; drives alerting.
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="ok", doc="'ok' | 'partial' | 'failed'."
    )
    # Structured metrics blob (portable JSON, not JSONB) — counts, timings, tokens.
    stats: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, doc="Counts, timings, token usage, model versions."
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, doc="Free-text run notes.")

    # Navigation to the items and drafts produced in this run.
    items: Mapped[list["Item"]] = relationship(back_populates="run")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="run")


class Item(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An ingested raw signal, normalised to the common schema (§11.2 / FR-02)."""

    __tablename__ = "items"

    # Which source produced this item (nullable so orphaned items survive a
    # source deletion for audit purposes).
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id"), nullable=True, doc="Originating source FK."
    )
    # Which daily run captured it — ties the item to a run's provenance.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id"), nullable=True, doc="Capturing run FK."
    )
    lane: Mapped[str] = mapped_column(Text, nullable=False, doc="'hc' | 'ai' lane.")
    title: Mapped[str] = mapped_column(Text, nullable=False, doc="Item title.")
    # URL is the primary natural key for dedup (§12.4); kept indexed-worthy.
    url: Mapped[str] = mapped_column(Text, nullable=False, doc="Canonical item URL (dedup key).")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Source publish time (recency filter)."
    )
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Source-provided abstract/snippet."
    )
    # Content hash for near-duplicate detection beyond URL/title (§12.4).
    content_hash: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Hash of normalised content for dedup."
    )
    relevance_score: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="Computed relevance score (§12.3)."
    )
    # Whether this item was chosen for a draft.
    selected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, doc="Chosen for a draft."
    )

    source: Mapped["Source | None"] = relationship(back_populates="items")
    run: Mapped["Run | None"] = relationship(back_populates="items")


class Draft(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A candidate LinkedIn post, its quality metadata, state machine, and image
    lane fields (§11.4 + §13.6)."""

    __tablename__ = "drafts"

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id"), nullable=True, doc="Producing run FK."
    )
    # Rotating daily focus that anchored the draft (§13.2).
    lane_focus: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Rotating focus of the day (§13.2)."
    )
    post_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Final candidate post text."
    )
    # Hashtags stored as a portable JSON array (not Postgres ARRAY).
    hashtags: Mapped[list | None] = mapped_column(
        ArrayType, nullable=True, doc="Hashtags as a JSON list."
    )
    # Provenance: the item ids that ground this draft, as a JSON list of UUID strings.
    source_item_ids: Mapped[list | None] = mapped_column(
        ArrayType, nullable=True, doc="Source item id strings (provenance) as JSON list."
    )
    # Quality report blob (§14.4) — grounding %, dedup, flags, confidence.
    quality_report: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, doc="Quality report JSON (§14.4)."
    )
    confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="Overall confidence 0-1 (§13.5)."
    )
    # Draft state machine value (§10.4); string keeps it portable + human-readable.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="new", doc="State machine value (§10.4)."
    )
    # HMAC of the issued approval token — NEVER store the raw token (§14.2).
    approve_token_hash: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="HMAC of the issued approval token (never raw)."
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Approval token expiry."
    )
    # When the draft is due to publish (the optimal slot, §10.3 / D7).
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Publish slot."
    )
    # LinkedIn URN + live URL, populated after a successful publish (§15.2).
    post_urn: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="LinkedIn post URN after publish."
    )
    post_url: Mapped[str | None] = mapped_column(Text, nullable=True, doc="Live post URL.")
    # Which model did generate/critique/verify + versions (§13.0).
    model_trace: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, doc="Per-pass model + version trace."
    )

    # --- Image lane (§13.6 data-model additions) ---------------------------
    # Decision outcome for the visual lane.
    image_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="none",
        doc="'none' | 'informative-card' | 'concept-illustration'.",
    )
    # On-disk path to the rendered/generated image (before LinkedIn upload).
    image_path: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Local path to the chosen image."
    )
    # Provenance of the image: 'deterministic' (card renderer) or a model id.
    image_source: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="'deterministic' | '<model-id>'."
    )
    # Prompt used for a concept illustration (null for deterministic cards).
    image_prompt: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Prompt for concept illustration."
    )
    # LinkedIn image URN returned by /rest/images after upload (§15.2).
    image_urn: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="LinkedIn image URN after upload."
    )

    run: Mapped["Run | None"] = relationship(back_populates="drafts")


class OwnPost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Dedup memory of the owner's own published posts (§11.5, FR-18).

    The embedding is stored as a portable JSON list[float]. In SQLite dev a
    Python cosine-similarity computes 90-day similarity; in Postgres prod this
    column can be migrated to ``pgvector`` (Vector) for indexed ANN search
    without changing the Python type (see base.VectorType docs).
    """

    __tablename__ = "own_posts"

    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drafts.id"), nullable=True, doc="Source draft FK."
    )
    post_urn: Mapped[str | None] = mapped_column(Text, nullable=True, doc="LinkedIn post URN.")
    post_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Published post text (for re-embedding)."
    )
    # Semantic embedding as JSON list[float]; pgvector in prod (see docstring).
    embedding: Mapped[list | None] = mapped_column(
        VectorType, nullable=True, doc="Embedding as JSON list[float] (pgvector in prod)."
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Publish time (90-day window)."
    )


class OAuthToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Encrypted OAuth tokens for LinkedIn (§11.6, NFR-05).

    Tokens are stored ENCRYPTED as raw bytes in ``LargeBinary`` (the portable
    equivalent of Postgres ``bytea``). Encryption/decryption happens in the token
    module; this table never holds plaintext, and the values are never logged.
    """

    __tablename__ = "oauth_tokens"

    # WHY a UNIQUE (provider, member_urn): a stored credential is the single live
    # record for one account, and token refresh is a read-modify-write that two
    # cron/worker processes can run concurrently. Without a DB-level uniqueness
    # guard, both could see "no row" and INSERT, leaving two live rows — a later
    # ``scalar_one_or_none`` would then raise ``MultipleResultsFound`` and the
    # account would be unusable. The constraint makes a losing insert race fail
    # fast (IntegrityError) so writers serialise on the database, not just on an
    # in-process lock (threat model §3 "atomic token replacement / refresh races").
    __table_args__ = (
        UniqueConstraint("provider", "member_urn", name="uq_oauth_tokens_provider_member"),
    )

    provider: Mapped[str] = mapped_column(
        Text, nullable=False, default="linkedin", doc="Token provider, e.g. 'linkedin'."
    )
    # Encrypted access/refresh tokens (bytes). LargeBinary -> bytea on Postgres,
    # BLOB on SQLite; portable either way.
    access_token_enc: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, doc="Encrypted access token (never plaintext)."
    )
    refresh_token_enc: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, doc="Encrypted refresh token (never plaintext)."
    )
    access_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Access token expiry (~60d)."
    )
    refresh_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Refresh token expiry (~365d)."
    )
    # The authenticated member's URN — the author identity for posts (§15.6).
    # NOT NULL: it is half the natural key (with ``provider``) and the associated
    # data the tokens are encrypted against, so a real credential row cannot exist
    # without it. A nullable member_urn would also defeat the UNIQUE guard above,
    # since SQL treats distinct NULLs as non-equal (duplicate NULL rows allowed).
    member_urn: Mapped[str] = mapped_column(
        Text, nullable=False, doc="urn:li:person:{sub} author identity (natural key half)."
    )


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only audit trail of every state change and publish (§11.7, §16).

    Rows are only ever inserted, never updated/deleted, giving a tamper-evident
    history for security review. ``at`` mirrors the event time explicitly (kept
    alongside ``created_at`` to match the BRD's field name).
    """

    __tablename__ = "audit_log"

    entity: Mapped[str] = mapped_column(Text, nullable=False, doc="Entity type, e.g. 'draft'.")
    entity_id: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Affected entity id (string for cross-table use)."
    )
    action: Mapped[str] = mapped_column(Text, nullable=False, doc="Action, e.g. 'approved'.")
    # Who performed it: 'owner', 'system', a job name, etc.
    actor: Mapped[str | None] = mapped_column(Text, nullable=True, doc="Actor performing action.")
    ip: Mapped[str | None] = mapped_column(Text, nullable=True, doc="Source IP for link actions.")
    # Extra context blob (portable JSON).
    meta: Mapped[dict | None] = mapped_column(JSONType, nullable=True, doc="Extra context JSON.")
    at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="Explicit event timestamp."
    )


class UsedToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Single-use nonce ledger for approval links (§14.2).

    An approval link is valid at most once: on use, its ``nonce`` is inserted
    here. A UNIQUE constraint means a replayed link fails the insert, enforcing
    single-use at the database level (fail-closed, NFR-04).
    """

    __tablename__ = "used_tokens"

    # The nonce embedded in the signed token; unique so replays are rejected.
    nonce: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True, doc="Single-use token nonce (unique)."
    )
    # Which draft + action the nonce was spent on (for audit correlation).
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drafts.id"), nullable=True, doc="Draft the token acted on."
    )
    action: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Action the token authorised (approve/reject/edit)."
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="When the token was spent."
    )


class AlertState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable last-fired ledger for operational-alert dedup (§17, NFR-08).

    WHY a persisted table and not just an in-process dict: every cron tick runs in a
    BRAND-NEW process, so an in-memory suppression map starts empty each tick and a
    persistent fault (a dead feed, a token that needs re-auth) would re-alert on
    EVERY tick and bury the owner. One row per ``dedup_key`` records when that alert
    last fired, so suppression survives process restarts and a permanent fault
    notifies once per window — not once per tick. Read/written by
    :mod:`vision.ops.alerts` (``DbAlertDedupStore``); it lives HERE so ``create_all``
    and Alembic autogenerate — which import only ``vision.db.models`` — register it.
    """

    __tablename__ = "alert_state"

    # The suppression key, ``"{kind}::{subject}"``. UNIQUE so there is exactly one
    # last-fired row per incident identity and an upsert can target it deterministically.
    dedup_key: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True, doc="Suppression key '{kind}::{subject}' (unique)."
    )
    # When this alert most recently fired; the instant the dedup window is measured from.
    last_fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="Most recent fire instant (UTC-aware)."
    )
