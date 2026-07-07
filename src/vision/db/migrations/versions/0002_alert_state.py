"""alert_state: durable last-fired ledger for operational-alert dedup

Revision ID: 0002_alert_state
Revises: 0001_oauth_unique
Create Date: 2026-07-07

WHY this migration (§17, NFR-08 "actionable, not noisy"): alert dedup state was
process-local and in-memory, but every cron tick is a NEW process — so a
persistent fault (a dead feed, a token that needs re-auth) re-alerted on EVERY
tick and buried the owner. This table persists one last-fired timestamp per
``dedup_key`` so suppression survives process restarts: a permanent fault now
notifies once per window, not once per tick.

Portable across SQLite (dev) and PostgreSQL (prod): only ``Uuid``, ``Text`` and
``DateTime(timezone=True)`` columns and a plain UNIQUE constraint — no
Postgres-only DDL. Review before applying to prod (BRD §22 verification loop).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0002_alert_state"
down_revision: Union[str, None] = "0001_oauth_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Shared names so upgrade()/downgrade() reference the same objects.
_TABLE = "alert_state"
_UQ_NAME = "uq_alert_state_dedup_key"


def upgrade() -> None:
    """Create the durable alert-dedup ledger.

    UNIQUE on ``dedup_key`` guarantees exactly one last-fired row per incident
    identity so the alerter's read-modify-write upsert targets it deterministically
    and a concurrent-insert race fails fast (IntegrityError) rather than duplicating.
    """
    op.create_table(
        _TABLE,
        # Portable UUID PK generated in Python (mirrors UUIDPrimaryKeyMixin).
        sa.Column("id", sa.Uuid(), nullable=False),
        # Suppression key "{kind}::{subject}".
        sa.Column("dedup_key", sa.Text(), nullable=False),
        # Instant the alert last fired; the dedup window is measured from here.
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=False),
        # Row audit timestamps (mirror TimestampMixin, server-side defaults).
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key", name=_UQ_NAME),
    )


def downgrade() -> None:
    """Drop the alert-dedup ledger (reverts to in-memory-only suppression)."""
    op.drop_table(_TABLE)
