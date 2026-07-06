"""Database package — SQLAlchemy 2.0 data layer (DB-agnostic).

Exposes the declarative ``Base``, all ORM models, and the session/engine
helpers. Kept DB-agnostic (SQLite dev / PostgreSQL prod) via portable column
types only — see ``base.py`` (BRD §22).
"""

from vision.db.base import Base  # re-exported for Alembic + convenience imports

__all__ = ["Base"]
