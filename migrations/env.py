"""Alembic environment for UpgradeLens (stage 4).

The schema is defined by SQLAlchemy metadata in :mod:`upgradelens.db.models`, and
the FTS5 virtual table is created by :func:`upgradelens.db.database.create_schema`.
The initial migration delegates to that function so there is a single source of
truth for the schema.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow the URL to be overridden by the environment (used by tests/CI).
_env_url = os.environ.get("UPGRADELENS_DB_URL")
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, do not execute)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a real database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
