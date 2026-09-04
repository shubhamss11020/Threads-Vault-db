#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Claude Code Hook: Stop
# ──────────────────────────────────────────────────────────────────────────────
# Fires automatically when Claude Code finishes a response (Stop lifecycle event).
# Sends the full conversation transcript to the Threads API checkpoint endpoint,
# which diffs against the existing thread and inserts only new messages.
#
# The model does NOT need to call save_chat_transcript — this hook handles it.
#
# Required environment variables:
#   THREADS_VAULT_URL    — base URL of the Thread-Vault Render service
#   THREADS_VAULT_SECRET — the user's secret from CLAUDE_OV_USERS
#
# Install: copy to .claude/hooks/ or configure in Claude Code settings
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

THREADS_API_URL="${THREADS_VAULT_URL:-}"
USER_SECRET="${THREADS_VAULT_SECRET:-}"

# Bail silently if not configured — don't break Claude Code
if [[ -z "$THREADS_API_URL" || -z "$USER_SECRET" ]]; then
    exit 0
fi

# Read the assistant response from stdin (Claude Code pipes it in)
RESPONSE=$(cat)

# Skip empty responses
if [[ -z "$RESPONSE" ]]; then
    exit 0
fi

CONVERSATION_ID="${CLAUDE_CONVERSATION_ID:-conv_$(date +%Y%m%d_%H%M%S)}"

# Derive a thread_name from the conversation (use conversation_id as fallback)
THREAD_NAME="${CLAUDE_THREAD_NAME:-${CONVERSATION_ID}}"

# Send the full response as a checkpoint to the Threads API
# The checkpoint endpoint diffs against existing state and only inserts new messages
curl -s --max-time 10 -X POST \
    "${THREADS_API_URL}/${USER_SECRET}/api/v1/threads/checkpoint" \
    -H "Content-Type: application/json" \
    -d "$(jq -n \
        --arg conversation_id "$CONVERSATION_ID" \
        --arg thread_name "$THREAD_NAME" \
        --arg transcript "$RESPONSE" \
        '{
            conversation_id: $conversation_id,
            thread_name: $thread_name,
            source: "claude_code",
            transcript: $transcript
        }'
    )" > /dev/null 2>&1 || true
