"""
Thread-Vault v2 — Pure data-access functions.

Every database operation goes through this module. No business logic here —
just CRUD, upserts, and queries. The Threads API and MCP tools call these.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.connection import get_session
from db.models import Analysis, IngestionEvent, Message, OutboxEvent, Thread, User


# ── Users ─────────────────────────────────────────────────────────────────────

def upsert_user(username: str) -> int:
    """Insert a user if they don't exist, return their id either way."""
    with get_session() as session:
        stmt = pg_insert(User).values(username=username).on_conflict_do_nothing(
            index_elements=["username"]
        )
        session.execute(stmt)
        session.commit()
        user = session.execute(select(User).where(User.username == username)).scalar_one()
        return user.id


def get_user_id(username: str) -> int | None:
    """Look up a user's id by username. Returns None if not found."""
    with get_session() as session:
        user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
        return user.id if user else None


def sync_users(usernames: list[str]) -> dict[str, int]:
    """Ensure all usernames exist in the users table. Returns username → id map.
    Called once at startup from CLAUDE_OV_USERS."""
    result = {}
    for username in usernames:
        result[username] = upsert_user(username)
    return result


# ── Threads ───────────────────────────────────────────────────────────────────

def upsert_thread(
    user_id: int,
    title: str,
    source: str,
    external_thread_id: str | None = None,
) -> uuid.UUID:
    """Create a thread or return the existing one's id. Updates `updated_at` on conflict."""
    with get_session() as session:
        stmt = pg_insert(Thread).values(
            user_id=user_id,
            title=title,
            source=source,
            external_thread_id=external_thread_id,
        ).on_conflict_do_update(
            constraint="uq_threads_user_title",
            set_={
                "updated_at": datetime.now(timezone.utc),
                "external_thread_id": external_thread_id or Thread.external_thread_id,
            },
        ).returning(Thread.id)
        result = session.execute(stmt)
        session.commit()
        return result.scalar_one()


def get_thread(thread_id: uuid.UUID) -> dict | None:
    """Get a thread by id, with its messages."""
    with get_session() as session:
        thread = session.execute(select(Thread).where(Thread.id == thread_id)).scalar_one_or_none()
        if not thread:
            return None
        messages = session.execute(
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.sequence_number)
        ).scalars().all()
        user = session.execute(select(User).where(User.id == thread.user_id)).scalar_one()
        return {
            "id": str(thread.id),
            "user_id": thread.user_id,
            "username": user.username,
            "title": thread.title,
            "source": thread.source,
            "external_thread_id": thread.external_thread_id,
            "created_at": thread.created_at.isoformat(),
            "updated_at": thread.updated_at.isoformat(),
            "messages": [
                {
                    "id": str(m.id),
                    "sequence_number": m.sequence_number,
                    "role": m.role,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                    "metadata": m.metadata_,
                }
                for m in messages
            ],
        }


def get_thread_by_user_and_title(user_id: int, title: str) -> dict | None:
    """Look up a thread by (user_id, title) — the natural key used by save_chat_transcript."""
    with get_session() as session:
        thread = session.execute(
            select(Thread).where(Thread.user_id == user_id, Thread.title == title)
        ).scalar_one_or_none()
        if not thread:
            return None
        return {"id": thread.id, "created_at": thread.created_at}


def list_threads(
    user_id: int | None = None,
    source: str | None = None,
    limit: int = 150,
    offset: int = 0,
) -> list[dict]:
    """List threads, newest first. Optionally filtered by user and/or source."""
    with get_session() as session:
        stmt = select(Thread, User.username).join(User).order_by(Thread.updated_at.desc())
        if user_id is not None:
            stmt = stmt.where(Thread.user_id == user_id)
        if source is not None:
            stmt = stmt.where(Thread.source == source)
        stmt = stmt.limit(limit).offset(offset)
        rows = session.execute(stmt).all()
        return [
            {
                "id": str(t.id),
                "username": username,
                "title": t.title,
                "source": t.source,
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            }
            for t, username in rows
        ]


def get_thread_message_count(thread_id: uuid.UUID) -> int:
    """How many messages does this thread have?"""
    with get_session() as session:
        return session.execute(
            select(func.count(Message.id)).where(Message.thread_id == thread_id)
        ).scalar_one()


# ── Messages ──────────────────────────────────────────────────────────────────

def insert_message(
    thread_id: uuid.UUID,
    sequence_number: int,
    role: str,
    content: str,
    metadata: dict | None = None,
) -> uuid.UUID:
    """Insert a single message into a thread."""
    with get_session() as session:
        msg = Message(
            thread_id=thread_id,
            sequence_number=sequence_number,
            role=role,
            content=content,
            metadata_=metadata or {},
        )
        session.add(msg)
        session.commit()
        return msg.id


def bulk_insert_messages(
    thread_id: uuid.UUID,
    messages: list[dict[str, Any]],
) -> int:
    """Insert multiple messages at once. Each dict needs: sequence_number, role, content.
    Returns count of inserted messages."""
    if not messages:
        return 0
    with get_session() as session:
        objs = [
            Message(
                thread_id=thread_id,
                sequence_number=m["sequence_number"],
                role=m["role"],
                content=m["content"],
                metadata_=m.get("metadata", {}),
            )
            for m in messages
        ]
        session.add_all(objs)
        session.commit()
        return len(objs)


def get_thread_messages(
    thread_id: uuid.UUID,
    after_sequence: int | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Get messages for a thread, ordered by sequence number."""
    with get_session() as session:
        stmt = (
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.sequence_number)
            .limit(limit)
        )
        if after_sequence is not None:
            stmt = stmt.where(Message.sequence_number > after_sequence)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "id": str(m.id),
                "sequence_number": m.sequence_number,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
                "metadata": m.metadata_,
            }
            for m in rows
        ]


# ── Search ────────────────────────────────────────────────────────────────────

def search_messages(
    query: str,
    user_id: int | None = None,
    limit: int = 12,
) -> list[dict]:
    """Search message content using ILIKE (case-insensitive pattern match).
    Returns messages with their thread context."""
    with get_session() as session:
        stmt = (
            select(Message, Thread.title, User.username)
            .join(Thread, Message.thread_id == Thread.id)
            .join(User, Thread.user_id == User.id)
            .where(Message.content.ilike(f"%{query}%"))
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(Thread.user_id == user_id)
        rows = session.execute(stmt).all()
        return [
            {
                "message_id": str(msg.id),
                "thread_id": str(msg.thread_id),
                "thread_title": title,
                "username": username,
                "role": msg.role,
                "content": msg.content,
                "sequence_number": msg.sequence_number,
                "created_at": msg.created_at.isoformat(),
            }
            for msg, title, username in rows
        ]


def search_threads(
    query: str,
    user_id: int | None = None,
    limit: int = 12,
) -> list[dict]:
    """Search threads by title or by content of their messages."""
    with get_session() as session:
        # Find threads where the title matches OR any message content matches
        msg_thread_ids = (
            select(Message.thread_id)
            .where(Message.content.ilike(f"%{query}%"))
            .distinct()
            .subquery()
        )
        stmt = (
            select(Thread, User.username)
            .join(User)
            .where(
                or_(
                    Thread.title.ilike(f"%{query}%"),
                    Thread.id.in_(select(msg_thread_ids.c.thread_id)),
                )
            )
            .order_by(Thread.updated_at.desc())
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(Thread.user_id == user_id)
        rows = session.execute(stmt).all()
        return [
            {
                "id": str(t.id),
                "username": username,
                "title": t.title,
                "source": t.source,
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            }
            for t, username in rows
        ]


# ── Ingestion Events ─────────────────────────────────────────────────────────

def insert_ingestion_event(
    source: str,
    role: str,
    content: str,
    event_id: str | None = None,
    conversation_id: str | None = None,
    raw_payload: dict | None = None,
) -> uuid.UUID | None:
    """Insert a raw ingestion event. Returns the event's UUID, or None if the
    event_id already exists (idempotent — duplicate is a no-op)."""
    with get_session() as session:
        # Idempotency check: if event_id is provided and already exists, skip
        if event_id:
            existing = session.execute(
                select(IngestionEvent.id).where(IngestionEvent.event_id == event_id)
            ).scalar_one_or_none()
            if existing:
                return None  # Already processed

        evt = IngestionEvent(
            event_id=event_id,
            conversation_id=conversation_id,
            source=source,
            role=role,
            content=content,
            raw_payload=raw_payload,
            status="pending",
        )
        session.add(evt)
        session.commit()
        return evt.id


def mark_ingestion_processed(event_id: str) -> None:
    """Mark an ingestion event as processed."""
    with get_session() as session:
        session.execute(
            update(IngestionEvent)
            .where(IngestionEvent.event_id == event_id)
            .values(status="processed", processed_at=datetime.now(timezone.utc))
        )
        session.commit()


def mark_ingestion_failed(event_id: str) -> None:
    """Mark an ingestion event as failed."""
    with get_session() as session:
        session.execute(
            update(IngestionEvent)
            .where(IngestionEvent.event_id == event_id)
            .values(status="failed", processed_at=datetime.now(timezone.utc))
        )
        session.commit()


# ── Outbox Events ─────────────────────────────────────────────────────────────

def insert_outbox_event(event_type: str, payload: dict) -> uuid.UUID:
    """Enqueue an event for the background worker (Markdown/Git projection)."""
    with get_session() as session:
        evt = OutboxEvent(event_type=event_type, payload=payload, status="pending")
        session.add(evt)
        session.commit()
        return evt.id


def get_pending_outbox_events(limit: int = 10) -> list[dict]:
    """Fetch the oldest pending outbox events for processing."""
    with get_session() as session:
        rows = session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.status == "pending")
            .order_by(OutboxEvent.created_at)
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "payload": e.payload,
                "created_at": e.created_at.isoformat(),
                "retry_count": e.retry_count,
            }
            for e in rows
        ]


def mark_outbox_processed(event_id: str) -> None:
    """Mark an outbox event as processed after successful projection."""
    with get_session() as session:
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == uuid.UUID(event_id))
            .values(status="processed", processed_at=datetime.now(timezone.utc))
        )
        session.commit()


def mark_outbox_failed(event_id: str) -> None:
    """Increment retry count and mark failed. After 3 retries, status stays 'failed'."""
    with get_session() as session:
        evt = session.execute(
            select(OutboxEvent).where(OutboxEvent.id == uuid.UUID(event_id))
        ).scalar_one_or_none()
        if evt:
            new_count = evt.retry_count + 1
            new_status = "pending" if new_count < 3 else "failed"
            session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == uuid.UUID(event_id))
                .values(
                    retry_count=new_count,
                    status=new_status,
                    processed_at=datetime.now(timezone.utc),
                )
            )
            session.commit()


# ── Analyses ──────────────────────────────────────────────────────────────────

def insert_analysis(user_id: int, title: str, content: str) -> uuid.UUID:
    """Save a new analysis."""
    with get_session() as session:
        analysis = Analysis(user_id=user_id, title=title, content=content)
        session.add(analysis)
        session.commit()
        return analysis.id


def list_analyses(user_id: int | None = None, limit: int = 150) -> list[dict]:
    """List analyses, newest first."""
    with get_session() as session:
        stmt = (
            select(Analysis, User.username)
            .join(User)
            .order_by(Analysis.created_at.desc())
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(Analysis.user_id == user_id)
        rows = session.execute(stmt).all()
        return [
            {
                "id": str(a.id),
                "username": username,
                "title": a.title,
                "created_at": a.created_at.isoformat(),
                "updated_at": a.updated_at.isoformat(),
            }
            for a, username in rows
        ]


def get_analysis(analysis_id: uuid.UUID) -> dict | None:
    """Get a single analysis by id."""
    with get_session() as session:
        a = session.execute(
            select(Analysis, User.username)
            .join(User)
            .where(Analysis.id == analysis_id)
        ).one_or_none()
        if not a:
            return None
        analysis, username = a
        return {
            "id": str(analysis.id),
            "username": username,
            "title": analysis.title,
            "content": analysis.content,
            "created_at": analysis.created_at.isoformat(),
            "updated_at": analysis.updated_at.isoformat(),
        }


def search_analyses(query: str, user_id: int | None = None, limit: int = 12) -> list[dict]:
    """Search analyses by title or content."""
    with get_session() as session:
        stmt = (
            select(Analysis, User.username)
            .join(User)
            .where(
                or_(
                    Analysis.title.ilike(f"%{query}%"),
                    Analysis.content.ilike(f"%{query}%"),
                )
            )
            .order_by(Analysis.created_at.desc())
            .limit(limit)
        )
        if user_id is not None:
            stmt = stmt.where(Analysis.user_id == user_id)
        rows = session.execute(stmt).all()
        return [
            {
                "id": str(a.id),
                "username": username,
                "title": a.title,
                "created_at": a.created_at.isoformat(),
            }
            for a, username in rows
        ]
