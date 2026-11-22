"""Keeping the database small. Diffs of build output are never kept (see capture.record),
and the lines of changes older than the number of days picked in Settings are cleared.
A cleared change keeps its prompt, files and line counts, so AI changes still lists it
and older code stays attributed to its prompt; its lines can't be shown or undone."""

import threading
import time
from datetime import datetime, timedelta, timezone

from looma import compare, preferences
from looma.capture import record
from looma.store import connect, now

# How often the API looks for diffs to clear, at most.
EVERY_SECONDS = 3600
# Space worth giving back to the disk; below this the file keeps the free pages for reuse.
VACUUM_BYTES = 1_000_000

_lock = threading.Lock()
_last = 0.0


def prune(days: int | None = None) -> dict:
    """Removes recorded diffs of build output, and clears the lines of changes older than
    `days` (the Settings choice when None; 0 keeps them for good)."""
    days = preferences.get()["keep_diffs_days"] if days is None else days
    conn = connect()
    freed = 0
    removed = cleared = 0
    with conn:
        for row in conn.execute("SELECT id, file_path, length(diff) AS size FROM changes").fetchall():
            if record.ignored(row["file_path"]):
                conn.execute("DELETE FROM changes WHERE id = ?", (row["id"],))
                removed += 1
                freed += row["size"]
        if days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
            for row in conn.execute(
                "SELECT id, file_path, diff FROM changes WHERE pruned_at IS NULL AND created_at < ?", (cutoff,)
            ).fetchall():
                additions, deletions = counts(row["file_path"], row["diff"])
                conn.execute("UPDATE changes SET diff = '', additions = ?, deletions = ?, pruned_at = ? WHERE id = ?",
                             (additions, deletions, now(), row["id"]))
                cleared += 1
                freed += len(row["diff"])
    if freed >= VACUUM_BYTES:
        conn.execute("VACUUM")
    conn.close()
    return {"removed": removed, "cleared": cleared, "freed_bytes": freed}


def maybe_prune(force: bool = False) -> None:
    """prune(), at most once an hour unless forced, never two at once, and never failing
    the caller."""
    global _last
    if not force and _last and time.monotonic() - _last < EVERY_SECONDS:
        return
    if not _lock.acquire(blocking=False):
        return
    try:
        _last = time.monotonic()
        prune()
    except Exception:  # a busy database or a full disk: try again next hour
        pass
    finally:
        _lock.release()


def counts(path: str, diff: str) -> tuple[int, int]:
    text = diff if diff.startswith("diff --git ") else f"diff --git a/{path} b/{path}\n{diff}"
    parsed = compare.parse_patch(text)
    return (parsed[0]["additions"], parsed[0]["deletions"]) if parsed else (0, 0)
