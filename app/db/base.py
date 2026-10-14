"""Database connectivity, declarative base, and session management."""

from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


# Global engine singleton
_settings = get_settings()

_connect_args: dict[str, Any] = {}
if "postgresql" in _settings.DATABASE_URL:
    _connect_args = {
        "connect_timeout": _settings.DB_CONNECT_TIMEOUT_SECONDS,
        "options": f"-c statement_timeout={_settings.DB_STATEMENT_TIMEOUT_MS}",
    }

engine = create_engine(
    _settings.DATABASE_URL,
    pool_size=_settings.DB_POOL_SIZE,
    max_overflow=_settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)


def get_db_session() -> Generator[Session, None, None]:
    """Dependency providing a transactional database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
