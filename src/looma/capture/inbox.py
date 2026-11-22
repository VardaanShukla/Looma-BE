"""Edits an AI made where no hook runs and nothing gets committed: a Claude app chat that
writes to the project through the desktop app's folder access. When it's done, the chat
leaves one file per request in the project's .looma/turns/ folder:

    {"prompt": "the request, word for word",
     "at": "2026-10-31T18:27:00Z (when the request was made; optional)",
     "files": [{"path": "src/app.py", "before": "the file before the first edit, or null if new"}]}

Each file becomes a recorded prompt with its changes, diffed against what's on disk now,
exactly as if Claude Code's hooks had seen it. The file is removed once it's recorded; one
that can't be read is renamed to .bad so it isn't tried again."""

import json
from datetime import datetime, timezone
from pathlib import Path

from looma.capture import record
from looma.store import connect

INBOX = ".looma/turns"
# Where requests were left before the rename, and still are by instructions pasted then.
LEGACY_INBOX = ".devdash/turns"
# sessions.tool for these prompts.
TOOL = "claude-app"


def ingest_all() -> int:
    """Records waiting requests in every tracked project. Returns how many were recorded."""
    conn = connect()
    try:
        return sum(ingest(conn, repo) for repo in conn.execute("SELECT id, path FROM repos").fetchall())
    finally:
        conn.close()


def ingest(conn, repo) -> int:
    folders = [Path(repo["path"]) / inbox for inbox in (INBOX, LEGACY_INBOX)]
    found = 0
    for item in sorted(item for folder in folders if folder.is_dir() for item in folder.glob("*.json")):
        try:
            request = _read(item)
        except (OSError, ValueError):
            try:
                item.replace(item.with_suffix(".json.bad"))
            except OSError:
                pass
            continue
        with conn:
            found += _record(conn, repo, item.stem, request, _when(request, item))
        item.unlink(missing_ok=True)
    return found


def _read(item: Path) -> dict:
    data = json.loads(item.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("prompt"), str) or not data["prompt"].strip():
        raise ValueError("needs a prompt")
    files = data.get("files")
    if not isinstance(files, list) or not all(
        isinstance(f, dict) and isinstance(f.get("path"), str) and (f.get("before") is None or isinstance(f["before"], str))
        for f in files
    ):
        raise ValueError("files is a list of {path, before}")
    return data


def _when(request: dict, item: Path) -> str:
    """When the request was made: its "at", or when the note was written. Not when Looma
    read it, which can be hours later, on another day."""
    at = request.get("at")
    if isinstance(at, str):
        try:
            return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.fromtimestamp(item.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _record(conn, repo, name: str, request: dict, at: str) -> int:
    root = Path(repo["path"]).resolve()
    session_id = f"app:{repo['id']}:{name}"
    turn = record._new_turn(conn, repo, session_id, request["prompt"].strip(), TOOL)
    recorded = 0
    for entry in request["files"]:
        target = (root / entry["path"]).resolve()
        if not target.is_relative_to(root):
            continue  # outside the project
        relative = target.relative_to(root).as_posix()
        if record.ignored(relative):
            continue
        after = record._read_text(target) if target.exists() else None
        if target.exists() and after is None:
            continue  # binary or too large
        diff = record.unified_diff(relative, entry["before"], after)
        if diff and not record.minified(diff):
            conn.execute(
                "INSERT INTO changes (repo_id, turn_id, file_path, diff, source, via, created_at)"
                " VALUES (?, ?, ?, ?, 'ai', 'edit', ?)",
                (repo["id"], turn["id"], relative, diff, at),
            )
            recorded += 1
    if not recorded:
        conn.execute("DELETE FROM turns WHERE id = ?", (turn["id"],))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return 0
    conn.execute("UPDATE turns SET started_at = ?, ended_at = ? WHERE id = ?", (at, at, turn["id"]))
    conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (at, session_id))
    return 1
