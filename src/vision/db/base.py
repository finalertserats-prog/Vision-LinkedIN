"""SQLAlchemy declarative base, portable types and reusable mixins.

WHY this module exists: BRD §22 mandates a DB-agnostic data layer — the same
models must run on SQLite (dev) and PostgreSQL (prod). This file centralises the
portable building blocks (UUID PK, timestamps, JSON, array-as-JSON, and an
embedding/vector-compat type) so no model ever emits Postgres-only DDL that
would break on SQLite.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# ---------------------------------------------------------------------------
# Portable column-type aliases.
#
# These names document *intent* at the model layer while resolving to types
# that behave identically on SQLite and Postgres:
#
#   * JSONType      -> SQLAlchemy's generic ``JSON`` (maps to Postgres JSON, NOT
#                      JSONB — JSONB is Postgres-only and would break SQLite).
#   * ArrayType     -> a JSON list. We deliberately DO NOT use ``ARRAY`` (a
#                      Postgres-only type); a JSON-encoded array is portable and
#                      round-trips as a Python list on both backends.
#   * VectorType    -> embeddings stored as a JSON list[float]. This keeps SQLite
#                      dev working with a Python cosine-similarity fallback. In
#                      Postgres prod, a migration can swap this column to
#                      ``pgvector`` (Vector) for indexed similarity search — the
#                      Python model stays a list[float] either way. See
#                      ``own_posts.embedding`` in models.py.
# ---------------------------------------------------------------------------
JSONType = JSON
ArrayType = JSON
VectorType = JSON


class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    A single base means Alembic can autogenerate migrations from one metadata
    object (``Base.metadata``), and every model shares consistent typing.
    """


class UUIDPrimaryKeyMixin:
    """Adds a portable UUID primary key.

    Uses ``sqlalchemy.Uuid`` (not the Postgres-only ``UUID``) so the column
    stores as a native ``uuid`` on Postgres and as a CHAR(32) on SQLite while the
    Python attribute is always a ``uuid.UUID``. Defaults are generated in Python
    (``uuid.uuid4``) so ids exist before flush — no dependency on a DB-side
    ``gen_random_uuid()`` that SQLite lacks.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
        doc="Portable UUID primary key (uuid4, generated in Python).",
    )


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` columns with server-side defaults.

    ``server_default=func.now()`` stamps the row at insert time on both backends;
    ``onupdate=func.now()`` bumps ``updated_at`` on every UPDATE. Timezone-aware
    ``DateTime(timezone=True)`` is used everywhere so timestamps survive the
    SQLite->Postgres move without silent tz loss (BRD §22).
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="Row creation time (UTC-aware, DB server default).",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        doc="Last-modified time (UTC-aware, bumped on UPDATE).",
    )
