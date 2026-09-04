"""
Thread-Vault v2 — Outbox Worker.

Background thread that projects PostgreSQL data to Markdown files + Git.

Flow:
    outbox_events (status='pending')
          ↓
    Worker polls every 5 seconds
          ↓
    For each event:
      1. Read thread/analysis data from PG
      2. Render to Markdown file
      3. git add → commit → push
      4. Mark outbox event as 'processed'
          ↓
    Retry up to 3 times on failure

Runs as a daemon thread inside the same process as the MCP server.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone


def _process_thread_updated(payload: dict) -> str:
    """Process a 'thread_updated' outbox event → render to Markdown → git push."""
    from db import queries as db_q
    from git_ops import git_commit_and_push, render_thread_to_markdown, VAULT_ROOT

    thread_id = uuid.UUID(payload["thread_id"])
    username = payload.get("username", "unknown")

    thread = db_q.get_thread(thread_id)
    if not thread:
        return f"Thread {thread_id} not found in PG — skipping."

    messages = thread.get("messages", [])
    if not messages:
        return f"Thread {thread_id} has no messages — skipping."

    # Render to Markdown
    out_path, action = render_thread_to_markdown(
        username=thread["username"],
        thread_title=thread["title"],
        messages=messages,
        created_at=thread["created_at"],
        updated_at=thread["updated_at"],
        source=thread["source"],
    )

    # Git commit + push
    rel_path = out_path.relative_to(VAULT_ROOT)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    push_result = git_commit_and_push(
        rel_path, f"chat: {action} {rel_path.name} ({timestamp})"
    )

    return f"[worker] thread_updated: {action} {rel_path.name} — {push_result}"


def _process_analysis_saved(payload: dict) -> str:
    """Process an 'analysis_saved' outbox event → render to Markdown → git push."""
    from db import queries as db_q
    from git_ops import git_commit_and_push, render_analysis_to_markdown, VAULT_ROOT

    analysis_id = uuid.UUID(payload["analysis_id"])
    analysis = db_q.get_analysis(analysis_id)
    if not analysis:
        return f"Analysis {analysis_id} not found in PG — skipping."

    out_path, action = render_analysis_to_markdown(
        username=analysis["username"],
        title=analysis["title"],
        content=analysis["content"],
        created_at=analysis["created_at"],
    )

    rel_path = out_path.relative_to(VAULT_ROOT)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    push_result = git_commit_and_push(
        rel_path, f"analysis: {action} {rel_path.name} ({timestamp})"
    )

    return f"[worker] analysis_saved: {action} {rel_path.name} — {push_result}"


# Event type → processor function
_PROCESSORS = {
    "thread_updated": _process_thread_updated,
    "analysis_saved": _process_analysis_saved,
}


def _worker_loop() -> None:
    """Main worker loop — polls outbox_events and processes them."""
    from db import queries as db_q

    print("[worker] Outbox worker loop started.")

    while True:
        try:
            events = db_q.get_pending_outbox_events(limit=10)
            if not events:
                time.sleep(5)
                continue

            for event in events:
                event_id = event["id"]
                event_type = event["event_type"]
                payload = event["payload"]
                retry_count = event["retry_count"]

                processor = _PROCESSORS.get(event_type)
                if not processor:
                    print(f"[worker] Unknown event type '{event_type}' — marking as failed.", file=sys.stderr)
                    db_q.mark_outbox_failed(event_id)
                    continue

                try:
                    result = processor(payload)
                    db_q.mark_outbox_processed(event_id)
                    print(result)
                except Exception as e:
                    print(f"[worker] Error processing {event_type} (retry {retry_count}): {e}\n"
                          f"{traceback.format_exc()}", file=sys.stderr)
                    db_q.mark_outbox_failed(event_id)

            # Small delay between batches to avoid hammering git
            time.sleep(2)

        except Exception as e:
            print(f"[worker] Worker loop error: {e}\n{traceback.format_exc()}", file=sys.stderr)
            time.sleep(10)  # Back off on unexpected errors


def start_worker_thread() -> threading.Thread:
    """Start the outbox worker as a daemon thread.
    Returns the thread object (for testing/monitoring)."""
    thread = threading.Thread(target=_worker_loop, name="outbox-worker", daemon=True)
    thread.start()
    return thread
