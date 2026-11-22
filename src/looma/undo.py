"""Undoing one of Claude's changes, and putting it back. The change's recorded diffs are
applied in reverse to the files as they are now, with `git apply`: all of them or none.
Nothing is committed, so the undo shows as an ordinary uncommitted change to review and
commit, and git can always bring the files back. If any file changed since Claude edited
it, the undo is refused rather than guessed."""

import re
import subprocess
import tempfile
from pathlib import Path

from looma import git
from looma.store import connect
from looma.store.db import now

KEY = re.compile(r"^(turn|commit)-([0-9a-f]+)-(\d+)$")


class UndoError(ValueError):
    pass


class NotFound(UndoError):
    pass


def key(kind: str, ref, repo_id: int) -> str:
    return f"{kind}-{ref}-{repo_id}"


def undone() -> dict[str, str]:
    """Each undone change's key and when it was undone."""
    conn = connect()
    rows = conn.execute("SELECT change_key, undone_at FROM undone").fetchall()
    conn.close()
    return {row["change_key"]: row["undone_at"] for row in rows}


def undo(change_key: str) -> str:
    kind, ref, repo_id, root, diffs = _change(change_key)
    if change_key in undone():
        raise UndoError("That change is already undone.")
    if kind == "commit" and not _on_head(root, ref):
        # A cloud session's commit on a branch that isn't checked out: its lines aren't in these files.
        raise UndoError("That commit isn't on the branch checked out in this folder, so there's nothing here to "
                        "undo. Check out its branch first, or revert it there.")
    # Later diffs of a file build on earlier ones, so they come off first.
    _apply(root, list(reversed(diffs)), reverse=True)
    at = now()
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO undone (change_key, repo_id, turn_id, commit_sha, undone_at) VALUES (?, ?, ?, ?, ?)",
            (change_key, repo_id, int(ref) if kind == "turn" else None, ref if kind == "commit" else None, at),
        )
    conn.close()
    return at


def put_back(change_key: str) -> None:
    _, _, _, root, diffs = _change(change_key)
    if change_key not in undone():
        raise UndoError("That change isn't undone.")
    _apply(root, diffs, reverse=False)
    conn = connect()
    with conn:
        conn.execute("DELETE FROM undone WHERE change_key = ?", (change_key,))
    conn.close()


def _change(change_key: str):
    match = KEY.match(change_key)
    if not match:
        raise NotFound("No such change.")
    kind, ref, repo_id = match[1], match[2], int(match[3])
    conn = connect()
    if kind == "turn":
        rows = conn.execute(
            "SELECT c.diff, r.path FROM changes c JOIN repos r ON r.id = c.repo_id"
            " WHERE c.source = 'ai' AND c.turn_id = ? AND c.repo_id = ? ORDER BY c.id",
            (int(ref), repo_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT c.diff, r.path FROM changes c JOIN repos r ON r.id = c.repo_id"
            " WHERE c.source = 'ai' AND c.turn_id IS NULL AND c.commit_sha = ? AND c.repo_id = ? ORDER BY c.id",
            (ref, repo_id),
        ).fetchall()
    conn.close()
    if not rows:
        raise NotFound("No such change.")
    if any(not row["diff"] for row in rows):
        raise UndoError("This change is older than Settings keeps lines for, so it can't be undone from here.")
    root = Path(rows[0]["path"])
    if git.toplevel(root) != root.resolve():
        raise UndoError(f"The project's folder isn't there any more: {root}")
    return kind, ref, repo_id, root, [row["diff"] for row in rows]


def _on_head(root: Path, sha: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=root,
                          capture_output=True).returncode == 0


def _apply(root: Path, diffs: list[str], reverse: bool) -> None:
    with tempfile.TemporaryDirectory() as folder:
        names = []
        for number, diff in enumerate(diffs):
            name = Path(folder) / f"{number:04}.patch"
            # Bytes, so Windows doesn't turn the patch's line endings into \r\n.
            name.write_bytes((diff if diff.endswith("\n") else diff + "\n").encode("utf-8"))
            names.append(str(name))
        flags = ["-R"] if reverse else []
        check = subprocess.run(["git", "apply", "--check", *flags, *names], cwd=root,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check.returncode != 0:
            raise UndoError(_why(check.stderr, reverse))
        git.run(root, "apply", *flags, *names)


def _why(stderr: str, reverse: bool) -> str:
    files = set(re.findall(r"^error: patch failed: (.+):\d+$", stderr, re.M))
    files |= set(re.findall(r"^error: (?!patch failed)(.+?): ", stderr, re.M))
    what = "undo it" if reverse else "put it back"
    if files:
        names = sorted(files)
        listed = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        return (f"Can't {what}: {listed} changed since then, so the change no longer fits. "
                "Nothing was touched. Undo the later changes to it first, or edit it by hand.")
    return f"Can't {what}: git refused ({stderr.strip() or 'no reason given'}). Nothing was touched."
