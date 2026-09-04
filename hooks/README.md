# Claude Code Hooks for Thread-Vault

These hooks enable **deterministic conversation capture** from Claude Code. Instead of relying on the model to call `save_chat_transcript`, the Claude Code runtime fires these hooks automatically at the right lifecycle events.

## Architecture

```
User types a prompt
       ↓
Claude Code fires UserPromptSubmit hook
       ↓
on_user_prompt.sh → POST /api/v1/threads/events
       ↓
Claude generates response
       ↓
Claude Code fires Stop hook
       ↓
on_stop.sh → POST /api/v1/threads/checkpoint
       ↓
Threads API → PostgreSQL
       ↓
Outbox Worker → Markdown → Git (async)
```

**The model doesn't make the persistence decision.** The hooks fire regardless of what Claude does.

## Setup

### 1. Set Environment Variables

Add these to your shell profile (`.bashrc`, `.zshrc`, PowerShell `$PROFILE`, etc.):

```bash
export THREADS_VAULT_URL="https://your-render-host.onrender.com"
export THREADS_VAULT_SECRET="your-secret-from-CLAUDE_OV_USERS"
```

**Windows PowerShell:**
```powershell
$env:THREADS_VAULT_URL = "https://your-render-host.onrender.com"
$env:THREADS_VAULT_SECRET = "your-secret-from-CLAUDE_OV_USERS"
```

### 2. Configure Claude Code Hooks

**Option A: Per-project** — add to `.claude/hooks.json` in your project root:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "type": "command",
        "command": "bash /path/to/Thread-Vault/hooks/on_user_prompt.sh"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "bash /path/to/Thread-Vault/hooks/on_stop.sh"
      }
    ]
  }
}
```

**Option B: Global** — add to `~/.claude/hooks.json` for all projects.

### 3. Verify

After setup, start a Claude Code session and check:

```bash
# Check the Threads API received events
curl -s "https://your-render-host.onrender.com/YOUR_SECRET/api/v1/threads" \
  -H "Content-Type: application/json" | jq .
```

You should see a new thread with the messages from your Claude Code session.

## Hook Details

### `on_user_prompt.sh` (UserPromptSubmit)

- **Fires**: When the user submits a prompt
- **Sends**: Single event with `role: "user"` to `/api/v1/threads/events`
- **Timeout**: 5 seconds (fire-and-forget, never blocks Claude Code)
- **On failure**: Silently continues — doesn't break the Claude Code experience

### `on_stop.sh` (Stop)

- **Fires**: When Claude finishes generating a response
- **Sends**: Full transcript to `/api/v1/threads/checkpoint`
- **Timeout**: 10 seconds (slightly longer for larger transcripts)
- **On failure**: Silently continues

## Troubleshooting

1. **Hooks not firing**: Ensure `hooks.json` is valid JSON and the paths are absolute.
2. **`jq` not found**: Install `jq` — `brew install jq` (macOS), `apt install jq` (Linux), `choco install jq` (Windows).
3. **Events not appearing**: Check Render logs for `[threads_api]` entries. Verify `THREADS_VAULT_URL` and `THREADS_VAULT_SECRET` are correct.
4. **SSL errors**: Ensure the Render URL uses `https://`.

## Windows Support

For Windows (PowerShell), create equivalent `.ps1` scripts or use WSL to run the bash scripts. The hooks work the same way — the key is that they call the Threads API HTTP endpoint.
