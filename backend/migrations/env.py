"""Alembic environment.

Two things make this file non-obvious:

1. The database URL comes from application Settings (``DATABASE_URL``), never
   from ``alembic.ini``. That keeps credentials out of version control and lets
   one migration set target PostgreSQL in production and SQLite in development.

2. ``JSONVariant`` is a portable JSON type: it renders as PostgreSQL ``JSONB``
   and falls back to plain ``JSON`` elsewhere. Autogenerate must emit that type
   by name rather than dialect-specific SQL, or the generated migration would
   only run on one database. ``render_item`` below handles that.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.db import JSONVariant, Base  # noqa: E402
# Importing the models module is REQUIRED: table objects register themselves on
# Base.metadata as a side effect of import. Without this, autogenerate compares
# against an empty metadata and silently emits a no-op migration.
from app import models  # noqa: E402,F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the URL, honouring an explicit ``-x db_url=...`` override."""
    opts = context.get_x_argument(as_dictionary=True)
    return opts.get("db_url") or get_settings().database_url


def render_item(type_: str, obj: object, autogen_context: object) -> str | None:
    """Render portable column types in generated migrations."""
    if type_ == "type" and isinstance(obj, JSONVariant):
        # Import the type into the generated module so it resolves at runtime.
        autogen_context.imports.add("from app.db import JSONVariant")  # type: ignore[attr-defined]
        return "JSONVariant()"
    return None


def run_migrations_offline() -> None:
    """Emit SQL without a live DBAPI connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run against a live connection."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    is_sqlite = connectable.dialect.name == "sqlite"
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
                # SQLite cannot ALTER most constraints in place; batch mode rewrites
            # the table. Harmless on PostgreSQL, where it is not triggered.
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
