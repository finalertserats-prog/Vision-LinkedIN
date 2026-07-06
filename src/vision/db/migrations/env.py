"""Alembic environment — wired to the models' metadata and settings.DATABASE_URL.

WHY: migrations must target the SAME schema the app uses (``Base.metadata``) and
the SAME database (``settings.DATABASE_URL``), without hard-coding a URL in
alembic.ini (config-over-code / secrets discipline, §22). This module bridges
Alembic to VISION's configuration and ORM at migration time.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from vision.config import get_settings
from vision.db.base import Base

# Import models for their side effect: registering every table on Base.metadata
# so autogenerate can diff the full schema.
from vision.db import models  # noqa: F401

# Alembic Config object providing access to values in alembic.ini.
config = context.config

# Inject the runtime database URL from Settings, overriding the blank ini value.
# This is why alembic.ini ships without a URL — the real one comes from env.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Configure Python logging from the ini file, if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for 'autogenerate' — the single declarative Base.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live DB connection).

    Useful for generating a SQL script to review before applying to prod.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        # Compare column types so type changes are detected in autogenerate.
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # short-lived migration connection, no pooling needed
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # Batch mode makes ALTER TABLE work on SQLite (which lacks full ALTER),
            # keeping migrations portable across dev/prod.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


# Entry point: pick offline vs online based on how Alembic was invoked.
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
