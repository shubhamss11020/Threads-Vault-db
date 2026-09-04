#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Claude Code Hook: UserPromptSubmit
# ──────────────────────────────────────────────────────────────────────────────
# Fires automatically when the user submits a prompt in Claude Code.
# Captures the user's prompt and sends it to the Threads API.
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

# Read the user prompt from stdin (Claude Code pipes it in)
PROMPT=$(cat)

# Skip empty prompts
if [[ -z "$PROMPT" ]]; then
    exit 0
fi

# Generate a unique event ID and conversation ID
EVENT_ID="evt_$(date +%s)_$(head -c 8 /dev/urandom | xxd -p)"
CONVERSATION_ID="${CLAUDE_CONVERSATION_ID:-conv_$(date +%Y%m%d_%H%M%S)}"

# Send to Threads API (fire-and-forget, don't block Claude Code)
curl -s --max-time 5 -X POST \
    "${THREADS_API_URL}/${USER_SECRET}/api/v1/threads/events" \
    -H "Content-Type: application/json" \
    -d "$(jq -n \
        --arg event_id "$EVENT_ID" \
        --arg conversation_id "$CONVERSATION_ID" \
        --arg content "$PROMPT" \
        '{
            event_id: $event_id,
            conversation_id: $conversation_id,
            role: "user",
            content: $content,
            source: "claude_code"
        }'
    )" > /dev/null 2>&1 || true
