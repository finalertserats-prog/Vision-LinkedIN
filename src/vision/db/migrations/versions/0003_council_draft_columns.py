"""drafts: add content_mode + council_meta for the council content pipeline

Revision ID: 0003_council_columns
Revises: 0002_alert_state
Create Date: 2026-07-07

WHY this migration (BRD §5 evolution / council-content-vision): the council path
produces a Draft the same table already stores, but tags it with WHICH pipeline
made it (``content_mode``) and carries the un-published deliberation provenance
(``council_meta``). Both are ADDITIVE and nullable-safe:

  * ``content_mode`` — NOT NULL with a server default of ``'news'`` so EVERY
    pre-existing daily draft back-fills to ``'news'`` in a single ALTER, with no
    value migration; council drafts write ``'council'``.
  * ``council_meta`` — nullable JSON; only council drafts populate it, a news
    draft leaves it NULL.

Portable across SQLite (dev) and PostgreSQL (prod): only ``Text`` and the generic
``JSON`` type (NOT Postgres-only ``JSONB``/``ARRAY``) and a plain server default —
no Postgres-only DDL. ``batch_alter_table`` is used so the ADD COLUMNs also apply
cleanly on SQLite, whose native ``ALTER TABLE`` support is limited (Alembic
emulates it via a table copy). Review before applying to prod (§22 verification).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic. Chains onto 0002 so the head advances
# linearly (0001 -> 0002 -> 0003).
revision: str = "0003_council_columns"
down_revision: Union[str, None] = "0002_alert_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Shared names so upgrade()/downgrade() reference the same objects (no drift).
_TABLE = "drafts"
_COL_CONTENT_MODE = "content_mode"
_COL_COUNCIL_META = "council_meta"
# The default content mode stamped onto every existing/news draft. A server-side
# default guarantees the NOT NULL back-fill happens in the database, not in Python.
_CONTENT_MODE_DEFAULT = "news"


def upgrade() -> None:
    """Add ``content_mode`` (NOT NULL, default 'news') and nullable ``council_meta``.

    ``batch_alter_table`` keeps the ADD COLUMNs portable to SQLite (which cannot
    add a NOT-NULL column with a default via a bare ``ALTER TABLE`` on older
    engines); Alembic performs the safe copy-and-swap where needed. The server
    default on ``content_mode`` back-fills existing rows to ``'news'`` atomically.
    """
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(
                _COL_CONTENT_MODE,
                sa.Text(),
                nullable=False,
                server_default=_CONTENT_MODE_DEFAULT,
            )
        )
        batch.add_column(
            sa.Column(
                _COL_COUNCIL_META,
                sa.JSON(),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Drop the two council columns (reverts drafts to the news-only shape)."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COL_COUNCIL_META)
        batch.drop_column(_COL_CONTENT_MODE)
