"""Database engine, session management and the declarative base.

Financial integrity notes (spec s96, s97):
  * SQLite is put into WAL mode with ``foreign_keys`` enforced -- without the
    pragma SQLite silently ignores foreign keys, which would let orphaned
    allocations accumulate.
  * All write paths go through :func:`session_scope` or the FastAPI
    ``get_db`` dependency so that a failure rolls the whole unit of work back.
    Partial writes are the classic source of "the ledger does not add up".
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    """Declarative base for every model in the system."""


def _make_engine() -> Engine:
    kwargs: dict = {"echo": settings.sql_echo, "future": True}
    if settings.is_sqlite:
        # check_same_thread=False: background job threads share the engine.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return create_engine(settings.database_url, **kwargs)


engine = _make_engine()


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record):  # pragma: no cover
    """Enforce foreign keys and enable WAL for concurrent reads."""
    if not settings.is_sqlite:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    # NORMAL is durable enough under WAL and far faster than FULL for the
    # write volume an import job produces.
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                            expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background jobs and scripts.

    Commits on success, rolls back on any exception. Never leaves a half
    written financial record behind.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def new_uid(prefix: str = "") -> str:
    """Stable, immutable public identifier for a record (spec s97).

    Internal integer primary keys are used for joins; this is what appears in
    URLs, reports and disputes so that an identifier quoted in a complaint
    letter never changes.
    """
    token = uuid.uuid4().hex[:16].upper()
    return f"{prefix}{token}" if prefix else token


def utcnow() -> datetime:
    """Timezone-aware current time. All stored timestamps are UTC."""
    return datetime.now(timezone.utc)


def init_db() -> None:
    """Create all tables. Import models first so they register on the Base."""
    from . import models  # noqa: F401  (registers mappers)

    settings.ensure_dirs()
    Base.metadata.create_all(bind=engine)
