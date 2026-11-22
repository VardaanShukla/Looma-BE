"""Finds Claude's commits that no hook saw being made: a cloud session (claude.ai/code)
that committed and pushed, or a commit made where Looma's git hook isn't installed.
Any commit on a local or fetched remote branch with a Claude co-author trailer counts as
Claude's work. Each is recorded once, under its commit message, since no prompt is known.

Edits written straight to disk with no hook and no commit can't be told apart from your
own, so they aren't guessed at: they show as uncommitted work like any other edit."""

import re
from datetime import datetime, timezone
from pathlib import Path

from looma import git
from looma.capture.record import _ignored
from looma.store import connect, now

# Only Claude here: the digest is Claude's changes, whichever tool other trailers name.
CLAUDE_TRAILER = re.compile(r"^Co-Authored-By:.*(claude|noreply@anthropic\.com)", re.IGNORECASE | re.MULTILINE)
GREP = r"^Co-Authored-By:.*(claude|noreply@anthropic\.com)"
# Older commits are history, not something to review today; this also bounds the first scan.
SINCE = "30.days"
# GitHub rewrites commits when a pull request is squashed or rebased on merge. The work
# was made (and seen) on the pull request's branch, so the copy isn't counted again.
GITHUB_COMMITTER = "noreply@github.com"


def scan_all() -> None:
    """Records Claude's new commits in every tracked project. Cheap when there are none:
    one `git log` per project."""
    conn = connect()
    try:
        repos = conn.execute("SELECT id, path FROM repos").fetchall()
        for repo in repos:
            root = Path(repo["path"])
            if not root.is_dir():
                continue
            try:
                scan(conn, repo)
            except git.GitError:
                continue  # not a git folder any more, or no commits yet
    finally:
        conn.close()


def scan(conn, repo) -> int:
    """Records the project's Claude co-authored commits not seen before. Returns how many."""
    root = Path(repo["path"])
    out = git.run(root, "log", "--branches", "--remotes", "--no-merges", f"--since={SINCE}", "--source",
                  "-i", "-E", f"--grep={GREP}", "--format=%H%x00%aI%x00%cI%x00%ce%x00%S%x00%B%x1e")
    remotes = git.run(root, "remote").split()
    found = 0
    for entry in out.split("\x1e"):
        entry = entry.strip("\n")
        if not entry:
            continue
        sha, authored, committed, committer, source, message = entry.split("\x00", 5)
        message = message.strip()
        if committer.lower() == GITHUB_COMMITTER or not CLAUDE_TRAILER.search(message):
            continue
        with conn:
            if _known(conn, repo["id"], sha, authored, message):
                continue
            _record(conn, repo, root, sha, authored, committed, _branch(source, remotes), message)
            found += 1
    return found


def _known(conn, repo_id: int, sha: str, authored: str, message: str) -> bool:
    if conn.execute("SELECT 1 FROM commits WHERE sha = ?", (sha,)).fetchone():
        return True
    # A rebased or cherry-picked copy keeps the author's time and message.
    return conn.execute("SELECT 1 FROM commits WHERE repo_id = ? AND authored_at = ? AND message = ?",
                        (repo_id, authored, message)).fetchone() is not None


def _record(conn, repo, root: Path, sha: str, authored: str, committed: str, branch: str | None,
            message: str) -> None:
    conn.execute(
        "INSERT INTO commits (sha, repo_id, branch, message, authored_at, found_by) VALUES (?, ?, ?, ?, ?, 'scan')",
        (sha, repo["id"], branch, message, authored),
    )
    files = git.commit_files(root, sha)
    diffs = _diffs(root, sha, files)
    for file_path in files:
        # Claude Code edits recorded by the hooks before this commit, on a machine whose
        # post-commit hook didn't fire, belong to their prompt: link them, don't count twice.
        linked = conn.execute(
            "UPDATE changes SET commit_sha = ?"
            " WHERE repo_id = ? AND file_path = ? AND commit_sha IS NULL AND source = 'ai'"
            " AND turn_id IS NOT NULL AND created_at <= ?"
            " AND turn_id NOT IN (SELECT turn_id FROM undone WHERE turn_id IS NOT NULL)",
            (sha, repo["id"], file_path, _utc(committed)),
        ).rowcount
        if linked or _ignored(file_path):
            continue
        diff = diffs.get(file_path)
        if diff is None:
            diff = git.commit_file_diff(root, sha, file_path)
        if diff:
            conn.execute(
                "INSERT INTO changes (repo_id, turn_id, commit_sha, file_path, diff, source, created_at)"
                " VALUES (?, NULL, ?, ?, ?, 'ai', ?)",
                (repo["id"], sha, file_path, diff, _utc(authored)),
            )


def _diffs(root: Path, sha: str, files: list[str]) -> dict[str, str]:
    """Each file's diff from one `git show`, in the order diff-tree lists the files. Empty
    when the two don't line up, and each file is then asked for on its own."""
    patch = git.run(root, "show", "--format=", "--patch", "--no-renames", "--no-color", "--no-ext-diff", sha)
    parts = re.split(r"^(?=diff --git )", patch, flags=re.MULTILINE)
    parts = [part for part in parts if part.startswith("diff --git ")]
    return dict(zip(files, parts)) if len(parts) == len(files) else {}


def _branch(source: str, remotes: list[str]) -> str | None:
    """The branch the commit was found on, without its remote's name: origin/x is x."""
    source = source.strip()
    for remote in remotes:
        if source.startswith(f"{remote}/"):
            return source[len(remote) + 1:]
    return source or None


def _utc(moment: str) -> str:
    try:
        return datetime.fromisoformat(moment).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return now()
