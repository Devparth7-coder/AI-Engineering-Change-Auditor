"""Database engine, session management and portable JSON column type.

PostgreSQL is the production target and uses native ``JSONB`` for evidence and
report payloads.  SQLite is supported as a development/demo fallback and
transparently uses ``JSON`` instead, so the same models work on both.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import JSON, TypeDecorator

from .config import get_settings


class JSONVariant(TypeDecorator):
    """JSONB on PostgreSQL, JSON elsewhere.

    ``impl`` is set at DDL-compile time so Alembic migrations and
    ``create_all`` both emit the right column type.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # noqa: ANN001, ANN201
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB

            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONVariant, list[Any]: JSONVariant}


_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine():  # noqa: ANN201
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        kwargs: dict[str, Any] = {"echo": settings.database_echo, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 20
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, class_=Session
        )
    return _SessionFactory


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for background workers (outside the request cycle)."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create tables.

    Alembic migrations are the authoritative schema path for real deployments
    (see ``backend/migrations``); this keeps local/dev startup frictionless and
    is idempotent.
    """
    from . import models  # noqa: F401  (ensure mappers are registered)

    Base.metadata.create_all(bind=get_engine())


def reset_engine_for_tests(database_url: str) -> None:
    """Rebind the engine (used by the test-suite to point at a temp database)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
    get_settings().database_url = database_url
