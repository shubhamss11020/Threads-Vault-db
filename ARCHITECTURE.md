# Architecture — Threads Vault (Claude Notes Vault)

This document is a complete technical reference for how this system is built, hosted, and operated: tech stack, runtime architecture, data flow, identity model, git-based persistence, CORS handling, and every tool/endpoint exposed. It complements `CLAUDE.md` (governing rules) and `SKILL.md` (agent-facing workflow instructions).

---

## 1. System Overview

**Threads Vault** is a single-process **MCP (Model Context Protocol) server** that gives Claude persistent memory for chat transcripts, analyses, and tribal knowledge. It has no database, no message queues, and no external cache — its only backing store is a **Git repository**, and its primary ingestion mechanism is Claude itself calling tools during live conversations.

### Key Goals:
- **Persistent Chat Storage**: Automatically save every assistant exchange into `raw/claude-chat-queries/<user>_<date>_<thread>.md`.
- **Knowledge Synthesis**: Structure tribal knowledge, undocumented workflows, and business rules into `wiki/chat-summaries/` and `wiki/analyses/`.
- **Multi-User Isolation**: Host a single Render instance while providing isolated, attributed endpoints for multiple team members via path-based URL secrets.
- **Cross-Vault Referencing**: Propose and stage pointers into external wikis without direct uncontrolled writes.

---

## 2. Technology Stack

| Layer | Technology | Purpose & Details |
|---|---|---|
| **Language** | Python 3.10+ | Single-file implementation in [`mcp_server.py`](mcp_server.py) (~900 lines) |
| **Protocol** | Model Context Protocol (MCP) | FastMCP SSE Transport (`mcp<2.0.0`) |
| **ASGI Engine** | `uvicorn` & `starlette` | ASGI server with `CORSMiddleware` and `_IdentityMiddleware` |
| **Persistence** | Git & GitHub | Every save executes an automated `git add / commit / fetch / rebase / push` cycle |
| **Hosting** | Render Web Service | Python runtime tracking the `data` branch with automatic deployment on push |
| **Source Control** | GitHub | Primary Repo: `https://github.com/shubhamss11020/Threads-Vault-db.git` |
| **Format** | Markdown + YAML frontmatter | Standardized `.md` files for both raw transcripts and synthesized summaries |
| **Dependencies** | Minimal | `mcp<2.0.0`, `starlette`, `uvicorn>=0.30.0` (see [`requirements.txt`](requirements.txt)) |

---

## 3. Hosting & Deployment Specifications

- **Platform**: Render Web Service (`runtime: python`, plan: free/starter).
- **Branch**: `data` (code and content reside on the same branch).
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `python mcp_server.py`
- **Port**: Configured via `PORT` env var (default: `8000`).

### Environment Variables

| Variable | Type | Description | Required? |
|---|---|---|---|
| `CLAUDE_OV_USERS` | JSON String | Map of `secret_key -> username` (e.g. `{"pQ1Wt...": "shubh"}`) | **Yes** |
| `GITHUB_TOKEN` | Secret String | GitHub Personal Access Token (PAT) with `Contents: Read and write` permission | **Yes** (for auto-push) |
| `GITHUB_REPO_URL` | String URL | `https://github.com/shubhamss11020/Threads-Vault-db.git` | Yes (defaults to repo) |
| `GIT_BRANCH` | String | Working branch (default: `data`) | Optional (default: `data`) |
| `PORT` | Integer | Internal HTTP port (default: `8000`) | Optional |
| `OV2_GITHUB_TOKEN` | Secret String | Scoped token for external wiki cross-referencing | Optional |
| `OV2_REPO_URL` | String URL | External wiki repo URL | Optional |
| `OV2_BRANCH` | String | External wiki branch (default: `data`) | Optional |

---

## 4. Runtime Architecture & Request Flow

```
                                    ┌───────────────────────────────────────────────┐
                                    │               Render Web Service              │
                                    │                                               │
   Claude Client (Desktop / Web)    │   uvicorn (Port 8000)                         │
   Connects to:                     │     └─ CORSMiddleware (Allows all origins)     │
   https://<host>/<secret>/sse ────▶│         └─ _IdentityMiddleware (ASGI)         │
                                    │             │                                 │
                                    │             ├── 1. Path & Secret Resolution   │
                                    │             ├── 2. OPTIONS / CORS Preflight   │
                                    │             ├── 3. Probe POST Handling        │
                                    │             ├── 4. Direct /api/save Hook      │
                                    │             └─ Rewrites to FastMCP internal   │
                                    │                     │                         │
                                    │         ┌───────────▼───────────┐             │
                                    │         │   FastMCP (SSE App)   │             │
                                    │         │   - /_internal/sse    │             │
                                    │         │   - /_internal/messages│            │
                                    │         └───────────┬───────────┘             │
                                    │                     │                         │
                                    │         ┌───────────▼───────────┐             │
                                    │         │ MCP Tool Handlers     │             │
                                    │         │ - save_chat_transcript│             │
                                    │         │ - save_analysis       │             │
                                    │         │ - search / get / list │             │
                                    │         └───────────┬───────────┘             │
                                    └─────────────────────┼─────────────────────────┘
                                                          │
                                           git add + commit + fetch + rebase + push
                                                          │
                                           ┌──────────────▼─────────────┐
                                           │ GitHub Repository           │
                                           │ shubhamss11020/Threads-Vault│
                                           │ Branch: data               │
                                           └────────────────────────────┘
```

---

## 5. Middleware & Protocol Handling (`_IdentityMiddleware`)

### 5.1 Multi-User Identity Resolution
1. **Secret Lookup**: When a request arrives at `/<secret>/sse`, the middleware extracts `<secret>` and looks it up in `CLAUDE_OV_USERS`. If not found, it immediately responds with `404 Not Found`.
2. **Context Propagation**: The resolved username is stored in Python's `contextvars.ContextVar` (`_current_user`), guaranteeing that tool execution knows the caller identity without trusting any model-provided argument.
3. **Session Snooping (Leg 1 $\rightarrow$ Leg 2)**:
   - MCP over SSE is a 2-step protocol:
     - **Leg 1 (`GET /<secret>/sse`)**: The server establishes the SSE stream and emits an `endpoint` event containing a relative message URL (`/_internal/messages/?session_id=<id>`).
     - **Leg 2 (`POST /_internal/messages/?session_id=<id>`)**: Tool invocations and responses are posted to this message endpoint.
   - The middleware inspects the outgoing SSE handshake body, records `_session_users[session_id] = username`, and maps subsequent Leg 2 requests back to the correct user.

### 5.2 Handshake & CORS Compliance
- **CORS Support**: `CORSMiddleware` wraps the ASGI application to allow browser-based clients (such as Claude.ai Web Connectors) without cross-origin blocks.
- **`OPTIONS` Preflight**: Handled globally with status `200 OK` and permissive `Access-Control-Allow-*` headers.
- **Probe `POST` Handling**: Connector validation pings to `/<secret>/sse` without a session ID return a `200 OK` JSON response (`{"jsonrpc": "2.0", "result": {"status": "ok", ...}}`) to satisfy verification probes.

---

## 6. Storage & Directory Hierarchy

```
Threads-Vault-db/
├── ARCHITECTURE.md          # Complete technical reference (this file)
├── CLAUDE.md                # System rules & directives for Claude
├── SKILL.md                 # Agent tool catalog & workflow definitions
├── mcp_server.py            # Complete MCP server implementation
├── render.yaml              # Render blueprint specification
├── requirements.txt         # Python dependencies
├── raw/
│   └── claude-chat-queries/ # Raw conversation transcripts
│       └── <user>_<date>_<thread_name>.md
└── wiki/
    ├── chat-summaries/      # Synthesized multi-session knowledge pages
    └── analyses/            # One-off analytical writeups
```

---

## 7. Git Auto-Sync & Conflict Recovery Flow

Whenever `save_chat_transcript` or `save_analysis` is called:
1. **Write to Disk**: The target markdown file is created or updated.
2. **Stage & Commit**: `git add <file>` and `git commit -m "<message>"`.
3. **Fetch & Rebase**:
   - Executes `git fetch origin data`.
   - Executes `git rebase origin/data`.
4. **Divergence Recovery**:
   - If a rebase fails due to concurrent saves or upstream changes, `_git_commit_and_push` aborts the rebase, captures local changes, syncs onto remote `origin/data`, and reapplies the diff cleanly.
5. **Push**: Pushes to `https://<GITHUB_TOKEN>@github.com/shubhamss11020/Threads-Vault-db.git`.

---

## 8. Available MCP Tools Reference

### Primary Write Tools
- **`save_chat_transcript(thread_name, new_messages)`**: Appends or creates conversation transcripts under `raw/claude-chat-queries/<user>_<date>_<thread_name>.md`. Injected with mandatory reminders on every model turn.
- **`save_analysis(title, content)`**: Saves synthesized analyses to `wiki/analyses/<title>.md`.

### Read & Query Tools
- **`list_claude_chat_queries(user=None)`**: Lists transcripts (optionally filtered by user).
- **`get_claude_chat_query(filename)`**: Retrieves full transcript content.
- **`search_claude_chat_queries(query, user=None)`**: Text search across all raw transcripts.
- **`list_chat_summaries()`** / **`get_chat_summary(filename)`** / **`search_chat_summaries(query)`**: Interacts with synthesized summary documents.
- **`list_analyses()`** / **`get_analysis(filename)`** / **`search_analyses(query)`**: Interacts with one-off saved analyses.

### Cross-Linking Tools
- **`propose_ov2_xref(ov2_page_path, pointer_line, chat_summary_title)`**: Stages a 1-2 line pointer locally in `.ov2-xref-staging/`.
- **`list_staged_ov2_xrefs()`**: Inspects staged proposals.
- **`apply_ov2_xref(staged_id)`**: Pushes approved pointer lines to an external repository via `OV2_GITHUB_TOKEN`.
