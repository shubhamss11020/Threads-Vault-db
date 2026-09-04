"""
Thread-Vault v2 — SQLAlchemy table definitions.

PostgreSQL is the source of truth. Markdown/Git is a downstream projection.
Tables:
  users             — resolved from CLAUDE_OV_USERS at startup
  threads           — one per conversation
  messages          — individual exchanges within a thread
  ingestion_events  — raw capture events (append-only audit log)
  outbox_events     — events waiting for Markdown/Git projection
  analyses          — one-off analytical writeups
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    threads = relationship("Thread", back_populates="user", lazy="select")
    analyses = relationship("Analysis", back_populates="user", lazy="select")

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username={self.username!r})>"


class Thread(Base):
    __tablename__ = "threads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    source = Column(String(50), nullable=False)  # 'claude_code', 'claude_web', 'api', 'mcp_fallback'
    external_thread_id = Column(String(255), nullable=True)  # conversation_id from hooks/gateway
    title = Column(String(500), nullable=False)  # thread_name
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="threads")
    messages = relationship("Message", back_populates="thread", lazy="select",
                            order_by="Message.sequence_number")

    __table_args__ = (
        UniqueConstraint("user_id", "title", name="uq_threads_user_title"),
        Index("ix_threads_user_id", "user_id"),
        Index("ix_threads_external_thread_id", "external_thread_id"),
    )

    def __repr__(self) -> str:
        return f"<Thread(id={self.id}, title={self.title!r}, source={self.source!r})>"


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id = Column(UUID(as_uuid=True), ForeignKey("threads.id"), nullable=False)
    sequence_number = Column(Integer, nullable=False)
    role = Column(String(20), nullable=False)  # 'user', 'assistant', 'system'
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    metadata_ = Column("metadata", JSONB, default=dict)

    thread = relationship("Thread", back_populates="messages")

    __table_args__ = (
        UniqueConstraint("thread_id", "sequence_number", name="uq_messages_thread_seq"),
        Index("ix_messages_thread_id", "thread_id"),
    )

    def __repr__(self) -> str:
        return f"<Message(id={self.id}, thread={self.thread_id}, seq={self.sequence_number}, role={self.role!r})>"


class IngestionEvent(Base):
    """Append-only audit log of every raw capture event received by the Threads API.
    Every event that arrives — from hooks, gateway, or MCP fallback — gets a row here
    before being processed into threads/messages. This is the write-ahead log."""
    __tablename__ = "ingestion_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(String(255), unique=True, nullable=True)  # idempotency key from capture layer
    conversation_id = Column(String(255), nullable=True)  # external conversation identifier
    source = Column(String(50), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    raw_payload = Column(JSONB, nullable=True)
    status = Column(String(20), default="pending")  # 'pending', 'processed', 'failed'
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    processed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_ingestion_events_status", "status"),
        Index("ix_ingestion_events_conversation_id", "conversation_id"),
    )

    def __repr__(self) -> str:
        return f"<IngestionEvent(id={self.id}, event_id={self.event_id!r}, status={self.status!r})>"


class OutboxEvent(Base):
    """Transactional outbox: events waiting for projection to Markdown/Git.
    The background worker polls this table, processes events, and marks them done."""
    __tablename__ = "outbox_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type = Column(String(50), nullable=False)  # 'thread_updated', 'thread_created', 'analysis_saved'
    payload = Column(JSONB, nullable=False)
    status = Column(String(20), default="pending")  # 'pending', 'processed', 'failed'
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    processed_at = Column(DateTime(timezone=True), nullable=True)
    retry_count = Column(Integer, default=0)

    __table_args__ = (
        Index("ix_outbox_events_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<OutboxEvent(id={self.id}, type={self.event_type!r}, status={self.status!r})>"


class Analysis(Base):
    __tablename__ = "analyses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="analyses")

    __table_args__ = (
        Index("ix_analyses_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return f"<Analysis(id={self.id}, title={self.title!r})>"
