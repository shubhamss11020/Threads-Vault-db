"""
Thread-Vault v2 — Database connection management.

Uses DATABASE_URL env var for connection. Provides both sync engine (for existing
synchronous MCP tool functions) and session factory.

On first startup, auto-creates all tables if they don't exist.
"""

import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

# ── Connection URL ────────────────────────────────────────────────────────────
# Supports both postgres:// (common shorthand) and postgresql:// (SQLAlchemy 2.x).
# Neon, Supabase, Render all provide postgres:// — SQLAlchemy 2.x needs postgresql://.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _normalize_url(url: str) -> str:
    """Convert postgres:// to postgresql:// for SQLAlchemy 2.x compatibility.
    Also handles sslmode for Neon/Supabase connections."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Neon requires SSL — ensure sslmode=require if not already present
    if "neon" in url and "sslmode" not in url:
        separator = "&" if "?" in url else "?"
        url += f"{separator}sslmode=require"
    return url


def get_engine() -> Engine:
    """Get or create the SQLAlchemy engine (singleton)."""
    global _engine
    if _engine is not None:
        return _engine

    if not DATABASE_URL:
        print(
            "WARNING: DATABASE_URL is not set. PostgreSQL features are disabled. "
            "The server will fall back to Git-only persistence.",
            file=sys.stderr,
        )
        raise RuntimeError("DATABASE_URL is not set")

    url = _normalize_url(DATABASE_URL)
    _engine = create_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_timeout=30,
        pool_recycle=1800,  # Recycle connections after 30 min (Neon auto-suspend friendly)
        pool_pre_ping=True,  # Verify connections are alive before using them
        echo=False,
    )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Get or create the session factory (singleton)."""
    global _SessionFactory
    if _SessionFactory is not None:
        return _SessionFactory

    engine = get_engine()
    _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
    return _SessionFactory


def get_session() -> Session:
    """Create a new database session. Caller is responsible for closing it.
    Usage:
        with get_session() as session:
            ...
    """
    factory = get_session_factory()
    return factory()


def create_tables() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("[db] All tables created/verified.")


def check_connection() -> bool:
    """Quick health check — can we reach the database?"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        print(f"[db] Connection check failed: {e}", file=sys.stderr)
        return False


def is_pg_available() -> bool:
    """Check if PostgreSQL is configured and reachable.
    Returns False gracefully if DATABASE_URL isn't set — allows the server to
    start in Git-only fallback mode during migration."""
    if not DATABASE_URL:
        return False
    try:
        return check_connection()
    except RuntimeError:
        return False
