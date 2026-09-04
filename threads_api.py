"""
Thread-Vault v2 — Threads API (Capture Endpoint).

The single ingestion point for all capture mechanisms:
  - Claude Code hooks (UserPromptSubmit, Stop)
  - LLM Gateway (future)
  - MCP fallback (save_chat_transcript calling internally)

All routes are Starlette-based and mounted in the same process as the MCP server.
Auth uses the same CLAUDE_OV_USERS secret-based identity resolution.

Endpoints:
  POST /api/v1/threads/events      — ingest a single conversation event
  POST /api/v1/threads/checkpoint   — batch ingest a full transcript
  GET  /api/v1/threads              — list threads
  GET  /api/v1/threads/{thread_id}  — get thread with messages
"""

from __future__ import annotations

import json
import re
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from db import queries


# ── Helpers ───────────────────────────────────────────────────────────────────

def _error(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def _success(data: dict) -> JSONResponse:
    return JSONResponse({"ok": True, **data})


def _resolve_user(request: Request) -> tuple[str | None, int | None]:
    """Extract username from request state (set by middleware) and resolve to user_id.
    Returns (username, user_id) or (None, None) if not authenticated."""
    username = getattr(request.state, "username", None)
    if not username:
        return None, None
    user_id = queries.get_user_id(username)
    if user_id is None:
        # Auto-create if somehow the user exists in CLAUDE_OV_USERS but not in PG
        user_id = queries.upsert_user(username)
    return username, user_id


def _parse_transcript_to_messages(content: str) -> list[dict[str, str]]:
    """Parse a Markdown-formatted transcript into a list of {role, content} dicts.

    Expected format (same as save_chat_transcript produces):
        **User:**
        prompt text here

        **Assistant:**
        response text here

    Also handles: ## User, ## Assistant, User:, Assistant: variants.
    """
    messages: list[dict[str, str]] = []
    current_role: str | None = None
    current_lines: list[str] = []

    # Patterns that mark a role boundary
    role_patterns = [
        (re.compile(r"^\*\*User:\*\*\s*$", re.IGNORECASE), "user"),
        (re.compile(r"^\*\*Assistant:\*\*\s*$", re.IGNORECASE), "assistant"),
        (re.compile(r"^##\s*User\s*$", re.IGNORECASE), "user"),
        (re.compile(r"^##\s*Assistant\s*$", re.IGNORECASE), "assistant"),
        (re.compile(r"^User:\s*$", re.IGNORECASE), "user"),
        (re.compile(r"^Assistant:\s*$", re.IGNORECASE), "assistant"),
        # Inline variants: "**User:** some text"
        (re.compile(r"^\*\*User:\*\*\s+(.+)", re.IGNORECASE), "user"),
        (re.compile(r"^\*\*Assistant:\*\*\s+(.+)", re.IGNORECASE), "assistant"),
    ]

    for line in content.splitlines():
        matched = False
        for pattern, role in role_patterns:
            m = pattern.match(line.strip())
            if m:
                # Save the previous block
                if current_role and current_lines:
                    text = "\n".join(current_lines).strip()
                    if text:
                        messages.append({"role": current_role, "content": text})
                current_role = role
                current_lines = []
                # If the pattern captured inline content, include it
                if m.lastindex and m.lastindex >= 1:
                    current_lines.append(m.group(1))
                matched = True
                break
        if not matched and current_role is not None:
            current_lines.append(line)

    # Don't forget the last block
    if current_role and current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            messages.append({"role": current_role, "content": text})

    return messages


# ── Event Ingestion ───────────────────────────────────────────────────────────

async def ingest_event(request: Request) -> JSONResponse:
    """POST /api/v1/threads/events

    Ingest a single conversation event (user prompt or assistant response).
    Idempotent via event_id — submitting the same event_id twice is a no-op.

    Payload:
    {
        "event_id": "evt_123",           // optional, for idempotency
        "conversation_id": "conv_456",   // required, groups messages into a thread
        "role": "user",                  // required: 'user' or 'assistant'
        "content": "Explain Redis...",   // required
        "source": "claude_code",         // required: 'claude_code', 'claude_web', 'api', 'mcp_fallback'
        "thread_name": "redis-pubsub",   // optional: human-readable thread title
        "timestamp": "2026-09-04T..."    // optional
    }
    """
    username, user_id = _resolve_user(request)
    if not user_id:
        return _error("Unauthorized", 401)

    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body")

    # Validate required fields
    conversation_id = body.get("conversation_id")
    role = body.get("role")
    content = body.get("content")
    source = body.get("source", "api")

    if not conversation_id:
        return _error("conversation_id is required")
    if role not in ("user", "assistant", "system"):
        return _error("role must be 'user', 'assistant', or 'system'")
    if not content:
        return _error("content is required")

    event_id = body.get("event_id")
    thread_name = body.get("thread_name") or conversation_id

    # 1. Write to ingestion_events (append-only audit log)
    ingestion_id = queries.insert_ingestion_event(
        event_id=event_id,
        conversation_id=conversation_id,
        source=source,
        role=role,
        content=content,
        raw_payload=body,
    )
    if ingestion_id is None:
        # Duplicate event_id — idempotent no-op
        return _success({"duplicate": True, "event_id": event_id})

    # 2. Upsert thread + insert message
    try:
        thread_id = queries.upsert_thread(
            user_id=user_id,
            title=thread_name,
            source=source,
            external_thread_id=conversation_id,
        )
        seq = queries.get_thread_message_count(thread_id) + 1
        message_id = queries.insert_message(
            thread_id=thread_id,
            sequence_number=seq,
            role=role,
            content=content,
            metadata={"event_id": event_id, "source": source},
        )

        # 3. Enqueue outbox event for Markdown/Git projection
        queries.insert_outbox_event(
            event_type="thread_updated",
            payload={"thread_id": str(thread_id), "username": username},
        )

        # 4. Mark ingestion event as processed
        if event_id:
            queries.mark_ingestion_processed(event_id)

        print(f"[threads_api] event ingested: user={username} thread={thread_name!r} "
              f"role={role} seq={seq} source={source}")

        return _success({
            "thread_id": str(thread_id),
            "message_id": str(message_id),
            "sequence_number": seq,
        })

    except Exception as e:
        if event_id:
            queries.mark_ingestion_failed(event_id)
        print(f"[threads_api] event processing failed: {e}\n{traceback.format_exc()}")
        return _error(f"Processing failed: {e}", 500)


# ── Checkpoint (Full Transcript) ──────────────────────────────────────────────

async def checkpoint(request: Request) -> JSONResponse:
    """POST /api/v1/threads/checkpoint

    Batch ingest: submit a full conversation transcript at once.
    Diffs against existing thread state, inserts only new messages.

    Payload:
    {
        "conversation_id": "conv_456",       // required
        "thread_name": "redis-pubsub",       // optional
        "source": "claude_code",             // required
        "transcript": "**User:**\\n...",     // required: full Markdown transcript
        "messages": [                        // alternative: pre-parsed messages
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."}
        ]
    }
    """
    username, user_id = _resolve_user(request)
    if not user_id:
        return _error("Unauthorized", 401)

    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body")

    conversation_id = body.get("conversation_id")
    source = body.get("source", "api")
    thread_name = body.get("thread_name") or conversation_id or "unknown"

    if not conversation_id and not body.get("thread_name"):
        return _error("conversation_id or thread_name is required")

    # Parse messages: either pre-parsed array or raw transcript
    parsed_messages = body.get("messages")
    if not parsed_messages:
        transcript = body.get("transcript", "")
        if not transcript:
            return _error("Either 'messages' array or 'transcript' string is required")
        parsed_messages = _parse_transcript_to_messages(transcript)

    if not parsed_messages:
        return _error("No messages could be parsed from the input")

    try:
        # Upsert thread
        thread_id = queries.upsert_thread(
            user_id=user_id,
            title=thread_name,
            source=source,
            external_thread_id=conversation_id,
        )

        # Get existing message count to determine which messages are new
        existing_count = queries.get_thread_message_count(thread_id)
        new_messages = parsed_messages[existing_count:]

        if not new_messages:
            return _success({
                "thread_id": str(thread_id),
                "new_messages": 0,
                "total_messages": existing_count,
            })

        # Insert only new messages
        to_insert = [
            {
                "sequence_number": existing_count + i + 1,
                "role": m["role"],
                "content": m["content"],
                "metadata": m.get("metadata", {"source": source}),
            }
            for i, m in enumerate(new_messages)
        ]
        inserted = queries.bulk_insert_messages(thread_id, to_insert)

        # Enqueue outbox event
        queries.insert_outbox_event(
            event_type="thread_updated",
            payload={"thread_id": str(thread_id), "username": username},
        )

        print(f"[threads_api] checkpoint: user={username} thread={thread_name!r} "
              f"new={inserted} total={existing_count + inserted} source={source}")

        return _success({
            "thread_id": str(thread_id),
            "new_messages": inserted,
            "total_messages": existing_count + inserted,
        })

    except Exception as e:
        print(f"[threads_api] checkpoint failed: {e}\n{traceback.format_exc()}")
        return _error(f"Checkpoint failed: {e}", 500)


# ── Internal Checkpoint (same-process call from MCP tools) ────────────────────

def checkpoint_sync(
    username: str,
    thread_name: str,
    content: str,
    source: str = "mcp_fallback",
) -> dict[str, Any]:
    """Synchronous checkpoint for use by MCP tool functions within the same process.
    Same logic as the async /checkpoint endpoint but without HTTP overhead.
    Returns a result dict."""
    user_id = queries.get_user_id(username)
    if user_id is None:
        user_id = queries.upsert_user(username)

    # Parse the transcript
    parsed_messages = _parse_transcript_to_messages(content)
    if not parsed_messages:
        # If parsing fails, treat the entire content as one assistant message
        parsed_messages = [{"role": "assistant", "content": content}]

    # Upsert thread
    thread_id = queries.upsert_thread(
        user_id=user_id,
        title=thread_name,
        source=source,
    )

    # Diff and insert new
    existing_count = queries.get_thread_message_count(thread_id)
    new_messages = parsed_messages[existing_count:]

    if not new_messages:
        return {
            "thread_id": str(thread_id),
            "new_messages": 0,
            "total_messages": existing_count,
            "action": "unchanged",
        }

    to_insert = [
        {
            "sequence_number": existing_count + i + 1,
            "role": m["role"],
            "content": m["content"],
            "metadata": {"source": source},
        }
        for i, m in enumerate(new_messages)
    ]
    inserted = queries.bulk_insert_messages(thread_id, to_insert)

    # Enqueue outbox event for Markdown/Git projection
    queries.insert_outbox_event(
        event_type="thread_updated",
        payload={"thread_id": str(thread_id), "username": username},
    )

    action = "created" if existing_count == 0 else "updated"
    print(f"[threads_api] checkpoint_sync: user={username} thread={thread_name!r} "
          f"action={action} new={inserted} total={existing_count + inserted}")

    return {
        "thread_id": str(thread_id),
        "new_messages": inserted,
        "total_messages": existing_count + inserted,
        "action": action,
    }


# ── Read Endpoints ────────────────────────────────────────────────────────────

async def list_threads(request: Request) -> JSONResponse:
    """GET /api/v1/threads?user=<username>&source=<source>&limit=150&offset=0"""
    username, user_id = _resolve_user(request)
    if not user_id:
        return _error("Unauthorized", 401)

    # Optional filters
    filter_user = request.query_params.get("user")
    filter_source = request.query_params.get("source")
    limit = int(request.query_params.get("limit", "150"))
    offset = int(request.query_params.get("offset", "0"))

    # If filtering by a specific user, resolve their id
    filter_user_id = None
    if filter_user:
        filter_user_id = queries.get_user_id(filter_user)
        if filter_user_id is None:
            return _success({"threads": [], "total": 0})

    threads = queries.list_threads(
        user_id=filter_user_id,
        source=filter_source,
        limit=limit,
        offset=offset,
    )
    return _success({"threads": threads})


async def get_thread(request: Request) -> JSONResponse:
    """GET /api/v1/threads/{thread_id}"""
    username, user_id = _resolve_user(request)
    if not user_id:
        return _error("Unauthorized", 401)

    thread_id_str = request.path_params.get("thread_id", "")
    try:
        thread_id = uuid.UUID(thread_id_str)
    except ValueError:
        return _error("Invalid thread_id format")

    thread = queries.get_thread(thread_id)
    if not thread:
        return _error("Thread not found", 404)

    return _success({"thread": thread})


# ── Route Table ───────────────────────────────────────────────────────────────
# These are mounted by the middleware in mcp_server.py

api_routes = [
    Route("/api/v1/threads/events", endpoint=ingest_event, methods=["POST"]),
    Route("/api/v1/threads/checkpoint", endpoint=checkpoint, methods=["POST"]),
    Route("/api/v1/threads", endpoint=list_threads, methods=["GET"]),
    Route("/api/v1/threads/{thread_id}", endpoint=get_thread, methods=["GET"]),
]
