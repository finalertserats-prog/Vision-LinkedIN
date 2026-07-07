"""oauth_tokens: NOT NULL member_urn + UNIQUE (provider, member_urn)

Revision ID: 0001_oauth_unique
Revises:
Create Date: 2026-07-07

WHY this migration (threat model §3 "atomic token replacement / refresh races"):
two cron/worker processes refreshing the same LinkedIn account could both read
"no row" and INSERT, leaving two live credential rows — a later
``scalar_one_or_none`` then raises ``MultipleResultsFound`` and the account
breaks. This migration adds a database-level ``UNIQUE (provider, member_urn)`` so
a losing insert race fails fast (IntegrityError) and writers serialise on the DB.

``member_urn`` is also made NOT NULL: it is half the natural key and SQL treats
distinct NULLs as non-equal, so a nullable column would silently permit duplicate
NULL rows and defeat the constraint (fail-closed, BRD §22.9).

Portable across SQLite (dev) and PostgreSQL (prod) via Alembic batch mode, which
rebuilds the table on SQLite (no native ALTER for NOT NULL / ADD CONSTRAINT).
Review before applying to prod (BRD §22 verification loop).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Revision identifiers, used by Alembic.
revision: str = "0001_oauth_unique"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Shared constraint name so upgrade() and downgrade() reference the same object.
_UQ_NAME = "uq_oauth_tokens_provider_member"


def upgrade() -> None:
    """Enforce a single live credential per (provider, member_urn).

    Batch mode is used so the identical migration runs on SQLite (which lacks a
    native ``ALTER COLUMN ... SET NOT NULL`` and ``ADD CONSTRAINT``) by rebuilding
    the table, and on PostgreSQL as plain ALTERs.
    """
    with op.batch_alter_table("oauth_tokens", schema=None) as batch_op:
        # member_urn must exist for every real row before it can be the key half.
        batch_op.alter_column(
            "member_urn",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch_op.create_unique_constraint(_UQ_NAME, ["provider", "member_urn"])


def downgrade() -> None:
    """Revert to a nullable member_urn with no uniqueness guard."""
    with op.batch_alter_table("oauth_tokens", schema=None) as batch_op:
        batch_op.drop_constraint(_UQ_NAME, type_="unique")
        batch_op.alter_column(
            "member_urn",
            existing_type=sa.Text(),
            nullable=True,
        )
