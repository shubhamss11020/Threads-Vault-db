"""
Thread-Vault v2 — Extracted Git operations.

Shared by the outbox worker and the OV2 xref system. Originally inline in
mcp_server.py, extracted here so the worker can import without circular deps.

These functions are synchronous (subprocess-based) — the worker runs them
in a background thread, not on the async event loop.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path


VAULT_ROOT = Path(__file__).parent
WIKI_DIR = VAULT_ROOT / "wiki"
RAW_DIR = VAULT_ROOT / "raw"
CHAT_DIR = RAW_DIR / "claude-chat-queries"
ANALYSES_DIR = WIKI_DIR / "analyses"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO_URL = os.environ.get(
    "GITHUB_REPO_URL", "https://github.com/shubhamss11020/Threads-Vault-db.git"
)
GIT_BRANCH = os.environ.get("GIT_BRANCH", "v2-pg")


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(VAULT_ROOT)] + args, capture_output=True, text=True)


def git_commit_and_push(rel_path: Path, commit_message: str) -> str:
    """Commit rel_path and push to GIT_BRANCH.

    Same logic as the original _git_commit_and_push in mcp_server.py:
    fetch + rebase before push, with divergence recovery."""
    if not GITHUB_TOKEN:
        return "WARNING: GITHUB_TOKEN not set — file saved locally but NOT pushed to GitHub."

    token_url = GITHUB_REPO_URL.replace("https://", f"https://{GITHUB_TOKEN}@")
    _git(["remote", "remove", "origin"])
    _git(["remote", "add", "origin", token_url])
    _git(["config", "user.email", "notes-mcp-server@eoxs.com"])
    _git(["config", "user.name", "Claude Notes MCP Server"])

    _git(["add", str(rel_path)])
    commit = _git(["commit", "-m", commit_message])
    if "nothing to commit" in (commit.stdout + commit.stderr):
        return "Nothing to commit (file unchanged)."
    if commit.returncode != 0:
        return f"git commit failed: {commit.stderr.strip()}"

    for attempt in range(2):
        fetch = _git(["fetch", "origin", GIT_BRANCH])
        if fetch.returncode != 0:
            return f"git commit succeeded locally but fetch before push failed: {fetch.stderr.strip()}"
        rebase = _git(["rebase", f"origin/{GIT_BRANCH}"])
        if rebase.returncode != 0:
            _git(["rebase", "--abort"])
            # Divergence recovery: capture local changes, reset to origin, replay
            pre_reset_head = _git(["rev-parse", "HEAD"]).stdout.strip()
            diff = _git(["diff", f"origin/{GIT_BRANCH}", pre_reset_head])
            if diff.returncode != 0 or not pre_reset_head:
                return (
                    f"git commit succeeded locally but rebase onto origin/{GIT_BRANCH} failed "
                    f"and recovery diff also failed: {diff.stderr.strip()}"
                )
            stranded_patch = diff.stdout
            reset = _git(["reset", "--hard", f"origin/{GIT_BRANCH}"])
            if reset.returncode != 0:
                return (
                    f"git commit succeeded locally but rebase onto origin/{GIT_BRANCH} failed "
                    f"and recovery reset also failed: {reset.stderr.strip()}"
                )
            if stranded_patch.strip():
                patch_path = VAULT_ROOT / ".git" / "_recovery.patch"
                patch_path.write_text(stranded_patch, encoding="utf-8")
                apply_ = _git(["apply", "--3way", str(patch_path.relative_to(VAULT_ROOT))])
                patch_path.unlink(missing_ok=True)
                if apply_.returncode != 0:
                    return (
                        f"git commit succeeded locally but rebase onto origin/{GIT_BRANCH} failed; "
                        f"reset to origin succeeded but replaying stranded local changes failed "
                        f"(previously stranded commits at {pre_reset_head} may need manual recovery): "
                        f"{apply_.stderr.strip()}"
                    )
            _git(["add", "-A"])
            recommit = _git(["commit", "-m", f"chat: recover stranded local changes\n\n{commit_message}"])
            if recommit.returncode != 0 and "nothing to commit" not in (recommit.stdout + recommit.stderr):
                return (
                    f"git commit succeeded locally but rebase onto origin/{GIT_BRANCH} failed; "
                    f"recovered by resetting to origin and replaying stranded changes, but recommit "
                    f"failed: {recommit.stderr.strip()}"
                )
            push = _git(["push", "origin", f"HEAD:{GIT_BRANCH}"])
            if push.returncode == 0:
                return (
                    f"Committed and pushed to origin/{GIT_BRANCH} (recovered from diverged local branch)."
                )
            return f"git commit succeeded locally but push failed even after divergence recovery: {push.stderr.strip()}"
        push = _git(["push", "origin", f"HEAD:{GIT_BRANCH}"])
        if push.returncode == 0:
            return f"Committed and pushed to origin/{GIT_BRANCH}."
        if attempt == 0:
            continue  # someone else pushed between our fetch and our push — retry once
        return f"git commit succeeded locally but push failed after retry: {push.stderr.strip()}"


def render_thread_to_markdown(
    username: str,
    thread_title: str,
    messages: list[dict],
    created_at: str,
    updated_at: str,
    source: str = "unknown",
) -> tuple[Path, str]:
    """Render a thread's messages into a Markdown file and write it to disk.
    Returns (file_path, action) where action is 'created' or 'updated'."""
    user_slug = re.sub(r"[^a-z0-9]+", "-", username.lower()).strip("-") or "unknown"
    title_slug = re.sub(r"[^a-z0-9]+", "-", thread_title.lower()).strip("-") or "conversation"

    # Determine the file path — reuse existing if present
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(CHAT_DIR.glob(f"{user_slug}_*_{title_slug}.md"))
    if existing:
        out = existing[0]
        is_new = False
    else:
        created_date = created_at[:10] if created_at else date.today().isoformat()
        out = CHAT_DIR / f"{user_slug}_{created_date}_{title_slug}.md"
        is_new = True

    # Preserve original created date from existing file
    created_date = created_at[:10] if created_at else date.today().isoformat()
    if not is_new:
        try:
            existing_content = out.read_text(encoding="utf-8")
            created_match = re.search(r'^created: (\S+)', existing_content, flags=re.MULTILINE)
            if created_match:
                created_date = created_match.group(1)
        except Exception:
            pass

    today = date.today().isoformat()
    frontmatter = (
        f"---\nthread_name: \"{thread_title}\"\nuser: \"{username}\"\ntype: claude-chat\n"
        f"source: \"{source}\"\ncreated: {created_date}\nupdated: {today}\n---\n\n"
    )

    # Render messages
    body_parts = []
    for m in messages:
        role_label = m["role"].capitalize()
        body_parts.append(f"**{role_label}:**\n{m['content']}")

    out.write_text(frontmatter + "\n\n".join(body_parts) + "\n", encoding="utf-8")
    action = "created" if is_new else "updated"
    return out, action


def render_analysis_to_markdown(
    username: str,
    title: str,
    content: str,
    created_at: str,
) -> tuple[Path, str]:
    """Render an analysis into a Markdown file and write it to disk.
    Returns (file_path, action)."""
    created_date = created_at[:10] if created_at else date.today().isoformat()
    today = date.today().isoformat()
    safe_title = re.sub(r'[<>:"/\\|?*]', "", title).strip()
    filename = f"{created_date} {safe_title}.md"
    out = ANALYSES_DIR / filename

    ANALYSES_DIR.mkdir(parents=True, exist_ok=True)

    n = 2
    while out.exists():
        out = ANALYSES_DIR / f"{created_date} {safe_title} ({n}).md"
        n += 1

    frontmatter = (
        f"---\ntitle: \"{safe_title}\"\ntype: analysis\nuser: \"{username}\"\n"
        f"created: {created_date}\nupdated: {today}\n---\n\n"
    )
    out.write_text(frontmatter + content.strip() + "\n", encoding="utf-8")
    return out, "created"
