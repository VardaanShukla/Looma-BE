"""Turns hook events into rows. Runs inside Claude Code, Codex, Antigravity and git
hooks, so it must be fast, print nothing (Claude Code and Codex add a prompt hook's
stdout to the chat; Antigravity gets the one JSON answer it requires) and never fail
the hook."""

import difflib
import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path

from looma import git, paths
from looma.capture import install
from looma.store import connect, now

# Set on Claude Code runs that Looma starts itself (summaries), so they are not recorded.
INTERNAL_ENV = "LOOMA_INTERNAL"

# Claude Code's file-editing tools, and Codex's (it edits files by patch).
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "apply_patch"}
# The files a Codex patch adds, changes, deletes or moves to.
PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.MULTILINE)
# What Antigravity needs on stdout from each hook Looma adds, whatever happened: it
# reads hook output as JSON, and a Stop answer of anything but "continue" lets it stop.
ANTIGRAVITY_ANSWER = {"user-prompt-submit": "{}", "stop": '{"decision": "stop"}'}
# Lines of an Antigravity transcript read back from its end to find the request.
TRANSCRIPT_TAIL_BYTES = 2_000_000
MAX_FILE_BYTES = 1_000_000
# A project with more files than this already changed when a prompt starts isn't watched
# for that prompt, to keep the prompt hook quick; Edit and Write are still recorded.
MAX_WATCHED_FILES = 2000
WATCH_HOURS = timedelta(hours=3)
# A prompt still open after this long lost its Stop and its session (killed, or a session
# that was never resumed). It's closed with what its Edit, Write and shell hooks saw.
STALE_HOURS = timedelta(hours=6)
IGNORED = [
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "*.min.js", "*.min.css", "*.map",
    "node_modules/*", "*/node_modules/*", "dist/*", "build/*", "*/__pycache__/*", "__pycache__/*",
    ".git/*", ".venv/*", "venv/*",
    # Build output inside a project, such as a UI bundled into a Python package.
    "*/dist/*", "*/build/*", "web/assets/*", "*/web/assets/*", "*.bundle.js", "*.chunk.js",
]
# A changed line this long is minified or generated code: its diff is skipped.
MINIFIED_LINE = 2000
UNKNOWN_PROMPT = "(prompt not captured)"
# Where Claude writes the request behind its changes before committing, for tools whose
# edits never pass through Claude Code (the Claude app). Kept out of git by `looma init`.
PROMPT_FILE = ".looma/prompt.md"
# Where the prompt file was kept before the rename, and still is by instructions pasted then.
LEGACY_PROMPT_FILE = ".devdash/prompt.md"
HOOK_LOG_MAX_BYTES = 1_000_000

# Commits an AI tool made or helped make. Edits that never pass through Claude Code's
# own tools (a cloud session writing files, a shell command) still get attributed this way.
AI_TRAILER = re.compile(
    r"^Co-Authored-By:.*(claude|noreply@anthropic\.com|copilot|cursor)", re.IGNORECASE | re.MULTILINE
)


def run_hook(event: str, stdin=sys.stdin, tool: str = "claude", stdout=sys.stdout) -> int:
    """`tool` is the AI tool whose hook this is; each sends its own shape of payload."""
    try:
        if os.environ.get(INTERNAL_ENV):
            return 0
        try:
            raw = _read_stdin(stdin) if event != "post-commit" else ""
            payload = _normalize(tool, json.loads(raw) if raw.strip() else {})
            conn = connect()
            try:
                handle(conn, event, payload, cwd=Path(payload.get("cwd") or os.getcwd()), tool=tool)
            finally:
                conn.close()
            _log_call(event, payload)
        except Exception as error:
            with paths.error_log().open("a", encoding="utf-8") as log:
                log.write(f"{now()} {event}\n{traceback.format_exc()}\n")
            try:
                # In the call log too, so the two read in order: what was called, what failed.
                with paths.hook_log().open("a", encoding="utf-8") as log:
                    log.write(f"{now()} {event} tool={tool} ERROR {type(error).__name__}: {str(error)[:200]}\n")
            except OSError:
                pass
        return 0
    finally:
        if tool == "antigravity" and event in ANTIGRAVITY_ANSWER:
            stdout.write(ANTIGRAVITY_ANSWER[event] + "\n")
            stdout.flush()


def health(days: int = 7) -> dict:
    """How recording has gone lately, from the hook logs: when each hook was last called and
    the errors, so a prompt that's missing can be told apart from one that failed."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    errors, last_error = 0, None
    log = paths.error_log()
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        for at, line in enumerate(lines):
            stamp = line.split(" ", 1)[0]
            if re.match(r"\d{4}-\d\d-\d\dT", stamp) and stamp >= since:
                errors += 1
                # The exception's own line ends its traceback.
                detail = next((l for l in lines[at + 1:] if l and not l.startswith((" ", "Traceback"))), "")
                last_error = {"at": stamp, "event": line.split(" ", 1)[-1], "detail": detail[:300]}
    last_call: dict[str, str] = {}
    calls = paths.hook_log()
    if calls.exists():
        for line in calls.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
            parts = line.split(" ")
            if len(parts) > 1 and "ERROR" not in parts:
                last_call[parts[1]] = parts[0]
    # Problems to show on the dashboard: hooks that can't record (Repair fixes these), then
    # a failure in the last day.
    problems = [problem for tool in install.tools_in_use() if (problem := install.hooks_problem(tool))]
    repairable = bool(problems)
    stuck = _close_stale_prompts()
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    if last_error and last_error["at"] >= day_ago:
        problems.append(f"A hook failed at {last_error['at']} ({last_error['event']}): {last_error['detail']}")
    return {"errors": errors, "days": days, "last_error": last_error, "last_call": last_call,
            "error_log": str(log), "hook_log": str(calls), "problems": problems, "repairable": repairable, "stuck": stuck,
            "tools": install.tools_in_use()}


def _close_stale_prompts(conn: sqlite3.Connection | None = None) -> int:
    """Closes prompts open longer than STALE_HOURS, keeping what their hooks saw; returns how many."""
    own = conn is None
    conn = conn or connect()
    try:
        since = (datetime.now(timezone.utc) - STALE_HOURS).isoformat(timespec="seconds")
        stale = conn.execute("SELECT id, branch FROM turns WHERE ended_at IS NULL AND started_at < ?", (since,)).fetchall()
        with conn:
            for turn in stale:
                _finish_turn(conn, turn, watch=False)
        return len(stale)
    finally:
        if own:
            conn.close()


def _normalize(tool: str, payload: dict) -> dict:
    """Antigravity's payload in Claude Code's terms: the conversation is the session and
    its first workspace the folder it works in. Claude Code's and Codex's already match."""
    if tool != "antigravity":
        return payload
    workspaces = [w for w in payload.get("workspacePaths") or [] if isinstance(w, str)]
    return {**payload, "session_id": payload.get("conversationId"), "cwd": workspaces[0] if workspaces else None,
            "workspaces": workspaces}


def _read_stdin(stdin) -> str:
    # Claude Code sends UTF-8. Read the bytes where there are any, since sys.stdin on
    # Windows decodes with the system code page and would garble non-English prompts.
    buffer = getattr(stdin, "buffer", None)
    return buffer.read().decode("utf-8", "replace") if buffer is not None else stdin.read()


def _log_call(event: str, payload: dict) -> None:
    # One line per hook call, without prompt text, so a missed edit can be traced.
    log = paths.hook_log()
    if log.exists() and log.stat().st_size > HOOK_LOG_MAX_BYTES:
        log.replace(log.with_suffix(".log.1"))
    tool_input = payload.get("tool_input") or {}
    target = ",".join(_edit_targets(payload))
    with log.open("a", encoding="utf-8") as out:
        out.write(f"{now()} {event} session={str(payload.get('session_id', '-'))[:8]} "
                  f"tool={payload.get('tool_name', '-')} file={target or '-'}\n")


def handle(conn: sqlite3.Connection, event: str, payload: dict, cwd: Path, tool: str = "claude") -> None:
    repos = conn.execute("SELECT id, path FROM repos WHERE claude = 1").fetchall()
    if not repos:
        return
    cwd = cwd.resolve()
    repos = _follow_moves(conn, repos, event, payload, cwd)
    with conn:
        if event == "post-commit":
            root = git.toplevel(cwd)
            repo = _repo_for(repos, root) if root else None
            if repo is not None:
                _commit(conn, repo)
        elif event == "user-prompt-submit":
            _close_stale_prompts(conn)
            if tool == "antigravity":
                _antigravity_invocation(conn, repos, payload, cwd)
            else:
                _prompt(conn, repos, payload, cwd, tool)
        elif event == "pre-tool-use":
            _before_edit(conn, repos, payload, cwd, tool)
        elif event == "post-tool-use":
            turn = _open_turn(conn, payload.get("session_id"))
            if turn is not None:
                _note_shell_changes(conn, turn["id"])
        elif event in ("stop", "session-end"):
            # Ending the session (exit, /clear) also ends a prompt whose Stop never came.
            turn = _open_turn(conn, payload.get("session_id"))
            if turn is not None:
                _finish_turn(conn, turn, watch=True)
        else:
            raise ValueError(f"unknown event {event!r}")


def _prompt(conn, repos, payload, cwd: Path, tool: str = "claude", prompt: str | None = None) -> None:
    session_id = payload["session_id"]
    _ensure_session(conn, _repo_for(repos, cwd), session_id, tool)
    # A turn left open means its Stop never fired (crash, interrupt); close it first.
    stale = _open_turn(conn, session_id)
    if stale is not None:
        # Without its Stop there's no telling when it ended, so only its Edit and Write count.
        _finish_turn(conn, stale, watch=False)
    turn = _new_turn(conn, _repo_for(repos, cwd), session_id, prompt or payload.get("prompt") or UNKNOWN_PROMPT, tool)
    folders = [Path(w).resolve() for w in payload.get("workspaces") or []] or [cwd]
    _start_watch(conn, turn["id"], repos, folders)


def _antigravity_invocation(conn, repos, payload, cwd: Path) -> None:
    """Antigravity calls this before every model call. The first of a request (none open
    for the conversation yet, or its very first call) starts the turn."""
    if not payload.get("session_id"):
        return
    first = payload.get("invocationNum") in (0, "0")
    if _open_turn(conn, payload["session_id"]) is not None and not first:
        return
    _prompt(conn, repos, payload, cwd, "antigravity", prompt=_antigravity_prompt(payload.get("transcriptPath")))


def _antigravity_prompt(transcript: str | None) -> str | None:
    """The newest request the user typed, from Antigravity's transcript (JSON lines). Its
    hooks don't pass the prompt, and the transcript's layout isn't documented, so any
    entry marked as the user's counts, with its text wherever it's kept."""
    if not transcript:
        return None
    path = Path(transcript)
    try:
        with path.open("rb") as file:
            size = file.seek(0, os.SEEK_END)
            file.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            lines = file.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _user_text(entry) if isinstance(entry, dict) else None
        if text:
            return text
    return None


_USER_KEYS = ("userInput", "user_input", "userMessage", "user_message")
_TEXT_KEYS = ("userResponse", "user_response", "text", "content", "message", "prompt", "parts", "items")


def _user_text(entry: dict) -> str | None:
    for key in _USER_KEYS:
        if key in entry:
            return _text(entry[key])
    kind = " ".join(str(entry.get(k, "")) for k in ("type", "role", "source", "author", "stepType")).lower()
    if re.search(r"\buser\b|user_input|userinput|user_message", kind):
        for key in _TEXT_KEYS:
            if key in entry:
                text = _text(entry[key])
                if text:
                    return text
        for value in entry.values():
            if isinstance(value, dict):
                text = _user_text({**value, "type": "user"})
                if text:
                    return text
    for value in entry.values():
        if isinstance(value, dict):
            text = _user_text(value)
            if text:
                return text
    return None


def _text(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        found = [t for t in (_text(v) for v in value) if t]
        return "\n".join(found) or None
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            if key in value:
                text = _text(value[key])
                if text:
                    return text
    return None


def _start_watch(conn, turn_id: int, repos, folders: list[Path]) -> None:
    """Notes, for each connected project the session works in, the commit it's on and the
    files that already differ from it, with their contents. At Stop, every file that
    differs since counts as the prompt's, whichever tool changed it."""
    for repo in repos:
        root = Path(repo["path"])
        # The session's own project, or the projects inside the folder it started in.
        if not any(cwd == root or cwd.is_relative_to(root) or root.is_relative_to(cwd) for cwd in folders) \
                or not root.is_dir():
            continue
        try:
            head = git.run(root, "rev-parse", "-q", "--verify", "HEAD", ok=(0, 1)).strip() or None
            status = git.run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
        except git.GitError:
            continue
        dirty = [entry[3:] for entry in status.split("\x00") if len(entry) > 3]
        dirty = [path for path in dirty if not _ignored(path)]
        if len(dirty) > MAX_WATCHED_FILES:
            continue  # too much already changed to note cheaply; Edit and Write still count
        conn.execute("INSERT INTO turn_repos (turn_id, repo_id, head) VALUES (?, ?, ?)", (turn_id, repo["id"], head))
        for path in dirty:
            absolute = root / path
            before = _read_text(absolute) if absolute.is_file() else None
            seen = -1 if before is None and absolute.exists() else 0
            conn.execute(
                "INSERT OR IGNORE INTO turn_files (turn_id, repo_id, file_path, before_content, seen)"
                " VALUES (?, ?, ?, ?, ?)",
                (turn_id, repo["id"], path, before, seen),
            )


def _edit_targets(payload: dict) -> list[str]:
    """The files an edit tool is about to write: Claude Code names one, a Codex patch any number."""
    tool_input = payload.get("tool_input") or {}
    if payload.get("tool_name") == "apply_patch":
        patch = tool_input.get("command") or tool_input.get("patch") or tool_input.get("input") or ""
        return PATCH_FILE.findall(patch) if isinstance(patch, str) else []
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    return [target] if target else []


def _before_edit(conn, repos, payload, cwd: Path, tool: str = "claude") -> None:
    if payload.get("tool_name") not in EDIT_TOOLS:
        return
    for target in _edit_targets(payload):
        _snapshot(conn, repos, payload, cwd, target, tool)


def _snapshot(conn, repos, payload, cwd: Path, target: str, tool: str) -> None:
    # The file decides the repo, not the session's folder: a session started in the
    # parent of several repos still edits files inside each of them.
    absolute = (cwd / target).resolve()
    repo = _repo_for(repos, absolute)
    if repo is None:
        return
    relative = absolute.relative_to(repo["path"]).as_posix()
    if _ignored(relative):
        return

    session_id = payload["session_id"]
    _ensure_session(conn, _repo_for(repos, cwd), session_id, tool)
    turn = _open_turn(conn, session_id)
    if turn is None:
        # Hooks were installed mid-session, so the prompt was missed.
        turn = _new_turn(conn, repo, session_id, UNKNOWN_PROMPT, tool)
    elif turn["branch"] is None:
        conn.execute("UPDATE turns SET branch = ? WHERE id = ?", (git.branch(Path(repo["path"])), turn["id"]))
    # A session started outside the project (another folder, an editor, the desktop app)
    # wasn't watching it: from its first edit there, the rest of what changes in it counts too.
    if not conn.execute("SELECT 1 FROM turn_repos WHERE turn_id = ? AND repo_id = ?", (turn["id"], repo["id"])).fetchone():
        root = Path(repo["path"])
        _start_watch(conn, turn["id"], [repo], [root])

    before = None
    if absolute.exists():
        before = _read_text(absolute)
        if before is None:
            return  # binary or too large
    # Only the first snapshot in a turn is kept: the one from the prompt's start, or the
    # first edit. A file noted then but too large to keep gets this edit's copy.
    conn.execute(
        "INSERT INTO turn_files (turn_id, repo_id, file_path, before_content, seen) VALUES (?, ?, ?, ?, 1)"
        " ON CONFLICT (turn_id, repo_id, file_path) DO UPDATE SET seen = 1,"
        " before_content = CASE WHEN seen = -1 THEN excluded.before_content ELSE before_content END",
        (turn["id"], repo["id"], relative, before),
    )


def _finish_turn(conn, turn, watch: bool) -> None:
    files = [dict(row) for row in conn.execute(
        "SELECT f.repo_id, r.path, f.file_path, f.before_content, f.seen"
        " FROM turn_files f JOIN repos r ON r.id = f.repo_id WHERE f.turn_id = ?",
        (turn["id"],),
    )]
    if watch:
        files += _changed_since_start(conn, turn["id"], {(f["repo_id"], f["file_path"]) for f in files})
    others = _edited_by_others(conn, turn["id"])
    recorded = 0
    for row in files:
        if row["seen"] == -1:
            continue  # too large or not text when the prompt started
        if row["seen"] == 0 and not watch:
            # Its Stop never came, so there's no telling whether a later change to a file that
            # was only noted at the start was this prompt's or the person's own.
            continue
        if row["seen"] != 1 and (row["repo_id"], row["file_path"]) in others:
            continue  # another session's prompt changed it with Edit or Write: that one's change
        current_path = Path(row["path"]) / row["file_path"]
        after = None
        if current_path.exists():
            after = _read_text(current_path)
            if after is None:
                continue  # became binary or too large
        diff = unified_diff(row["file_path"], row["before_content"], after)
        if diff and not minified(diff):
            conn.execute(
                "INSERT INTO changes (repo_id, turn_id, file_path, diff, source, via, created_at)"
                " VALUES (?, ?, ?, ?, 'ai', ?, ?)",
                (row["repo_id"], turn["id"], row["file_path"], diff, "edit" if row["seen"] == 1 else "watch", now()),
            )
            recorded += 1
    if recorded:
        _link_commits_made_during(conn, turn["id"])
    conn.execute("DELETE FROM turn_files WHERE turn_id = ?", (turn["id"],))
    conn.execute("DELETE FROM turn_repos WHERE turn_id = ?", (turn["id"],))
    session = conn.execute(
        "SELECT s.id, s.repo_id FROM sessions s JOIN turns t ON t.session_id = s.id WHERE t.id = ?",
        (turn["id"],),
    ).fetchone()
    if not recorded and session["repo_id"] is None:
        # A session outside any tracked repo that changed nothing in one: keep no trace of it.
        conn.execute("DELETE FROM turns WHERE id = ?", (turn["id"],))
        conn.execute(
            "DELETE FROM sessions WHERE id = ? AND NOT EXISTS (SELECT 1 FROM turns WHERE session_id = ?)",
            (session["id"], session["id"]),
        )
        return
    conn.execute("UPDATE turns SET ended_at = ? WHERE id = ?", (now(), turn["id"]))


def _note_shell_changes(conn, turn_id: int) -> None:
    """After each shell command, notes the files it changed (seen = 2), so they still count
    when the prompt is interrupted and its Stop never comes."""
    known = {}
    for row in conn.execute(
        "SELECT f.repo_id, r.path, f.file_path, f.before_content, f.seen"
        " FROM turn_files f JOIN repos r ON r.id = f.repo_id WHERE f.turn_id = ?", (turn_id,)
    ):
        known[(row["repo_id"], row["file_path"])] = row
    others = _edited_by_others(conn, turn_id)
    for key, row in known.items():
        if row["seen"] != 0 or key in others:
            continue
        current = Path(row["path"]) / row["file_path"]
        after = _read_text(current) if current.exists() else None
        if after != row["before_content"]:
            conn.execute("UPDATE turn_files SET seen = 2 WHERE turn_id = ? AND repo_id = ? AND file_path = ?",
                         (turn_id, row["repo_id"], row["file_path"]))
    for found in _changed_since_start(conn, turn_id, set(known)):
        if (found["repo_id"], found["file_path"]) in others:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO turn_files (turn_id, repo_id, file_path, before_content, seen) VALUES (?, ?, ?, ?, 2)",
            (turn_id, found["repo_id"], found["file_path"], found["before_content"]),
        )


def _edited_by_others(conn, turn_id: int) -> set:
    """Files that another session's prompt changed with Edit or Write while this one ran.
    Two sessions in one project each see the other's edits appear; these are left to the
    prompt whose own tool made them, so no change is counted twice."""
    started = conn.execute("SELECT started_at, session_id FROM turns WHERE id = ?", (turn_id,)).fetchone()
    rows = conn.execute(
        "SELECT f.repo_id, f.file_path FROM turn_files f JOIN turns t ON t.id = f.turn_id"
        " WHERE f.seen = 1 AND t.session_id != ? AND t.ended_at IS NULL"
        " UNION SELECT c.repo_id, c.file_path FROM changes c JOIN turns t ON t.id = c.turn_id"
        " WHERE c.via = 'edit' AND t.session_id != ? AND c.created_at >= ?",
        (started["session_id"], started["session_id"], started["started_at"]),
    ).fetchall()
    return {(row["repo_id"], row["file_path"]) for row in rows}


def _link_commits_made_during(conn, turn_id: int) -> None:
    """A file the prompt changed and that was committed before it stopped belongs to the
    newest such commit, unless it changed again since. The post-commit hook left these
    files to the prompt (see _commit), so each is counted once."""
    for row in conn.execute(
        "SELECT w.repo_id, w.head, r.path FROM turn_repos w JOIN repos r ON r.id = w.repo_id WHERE w.turn_id = ?",
        (turn_id,),
    ).fetchall():
        root = Path(row["path"])
        try:
            log = git.run(root, "log", "--format=%x00%H", "--name-only", "--no-renames",
                          f"{row['head']}..HEAD" if row["head"] else "HEAD")
            status = git.run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
        except git.GitError:
            continue
        newest: dict[str, str] = {}
        for block in log.split("\x00")[1:]:
            sha, _, names = block.partition("\n")
            for name in names.split("\n"):
                if name.strip():
                    newest.setdefault(name.strip(), sha.strip())
        dirty = {entry[3:] for entry in status.split("\x00") if len(entry) > 3}
        for path, sha in newest.items():
            if path in dirty or not conn.execute("SELECT 1 FROM commits WHERE sha = ?", (sha,)).fetchone():
                continue
            conn.execute(
                "UPDATE changes SET commit_sha = ? WHERE turn_id = ? AND repo_id = ? AND file_path = ? AND commit_sha IS NULL",
                (sha, turn_id, row["repo_id"], path),
            )


def _changed_since_start(conn, turn_id: int, known: set) -> list[dict]:
    """Files that differ from how they were when the prompt started and that no snapshot
    covers: they were the same as the commit then, so that commit has their before copy."""
    found = []
    for row in conn.execute(
        "SELECT w.repo_id, w.head, r.path FROM turn_repos w JOIN repos r ON r.id = w.repo_id WHERE w.turn_id = ?",
        (turn_id,),
    ).fetchall():
        root = Path(row["path"])
        if not root.is_dir():
            continue
        try:
            # Against the starting commit, not HEAD, so a commit made during the prompt still counts.
            tracked = (git.run(root, "diff", "--name-only", "-z", "--no-renames", row["head"], "--") if row["head"]
                       else git.run(root, "ls-files", "-z"))
            untracked = git.run(root, "ls-files", "--others", "--exclude-standard", "-z")
        except git.GitError:
            continue
        paths = dict.fromkeys(p for p in (tracked + untracked).split("\x00") if p)
        for path in paths:
            if (row["repo_id"], path) in known or _ignored(path):
                continue
            exists, before = _at_commit(root, row["head"], path)
            if exists and before is None:
                continue  # too large or not text in the commit
            found.append({"repo_id": row["repo_id"], "path": row["path"], "file_path": path,
                          "before_content": before, "seen": 0})
    return found


def _at_commit(root: Path, head: str | None, path: str) -> tuple[bool, str | None]:
    """Whether the file was in the commit, and its text when it's text and not too large."""
    if head is None:
        return False, None
    result = subprocess.run(["git", "cat-file", "blob", f"{head}:{path}"], cwd=root, capture_output=True)
    if result.returncode != 0:
        return False, None
    if len(result.stdout) > MAX_FILE_BYTES:
        return True, None
    try:
        return True, result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return True, None


def _commit(conn, repo) -> None:
    root = Path(repo["path"])
    info = git.commit_info(root)
    sha = info["sha"]
    if conn.execute("SELECT 1 FROM commits WHERE sha = ?", (sha,)).fetchone():
        return
    conn.execute(
        "INSERT INTO commits (sha, repo_id, branch, message, authored_at) VALUES (?, ?, ?, ?, ?)",
        (sha, repo["id"], git.branch(root), info["message"], info["authored_at"]),
    )
    prompt_turn = _take_prompt_file(conn, repo)
    source = "ai" if prompt_turn or AI_TRAILER.search(info["message"]) else "human"
    # A Claude prompt is running and watching this project: its Stop records these files
    # and links them to this commit (see _link_commits_made_during), so they aren't added here.
    # A prompt open for hours lost its Stop, and is no reason to leave a commit unrecorded.
    recent = (datetime.now(timezone.utc) - WATCH_HOURS).isoformat(timespec="seconds")
    watched = conn.execute(
        "SELECT 1 FROM turn_repos w JOIN turns t ON t.id = w.turn_id"
        " WHERE w.repo_id = ? AND t.ended_at IS NULL AND t.started_at >= ?",
        (repo["id"], recent),
    ).fetchone() is not None
    for file_path in git.commit_files(root, sha):
        linked = conn.execute(
            "UPDATE changes SET commit_sha = ?"
            " WHERE repo_id = ? AND file_path = ? AND commit_sha IS NULL AND source = 'ai'"
            # An undone change didn't land in this commit, even if its file did.
            " AND (turn_id IS NULL OR turn_id NOT IN (SELECT turn_id FROM undone WHERE turn_id IS NOT NULL))",
            (sha, repo["id"], file_path),
        ).rowcount
        if linked or watched or _ignored(file_path):
            continue
        diff = git.commit_file_diff(root, sha, file_path)
        if diff and not minified(diff):
            conn.execute(
                "INSERT INTO changes (repo_id, turn_id, commit_sha, file_path, diff, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo["id"], prompt_turn, sha, file_path, diff, source, now()),
            )


def _take_prompt_file(conn, repo) -> int | None:
    """Turns a waiting prompt file into a turn for this commit, then clears it."""
    root = Path(repo["path"])
    path = next((root / name for name in (PROMPT_FILE, LEGACY_PROMPT_FILE) if (root / name).exists()), root / PROMPT_FILE)
    text = _read_text(path) if path.exists() else None
    if not text or not text.strip():
        return None
    session_id = f"git:{repo['id']}"  # one pseudo-session per repo for commit-captured prompts
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, repo_id, tool, started_at) VALUES (?, ?, 'git', ?)",
        (session_id, repo["id"], now()),
    )
    turn = _new_turn(conn, repo, session_id, text.strip())
    conn.execute("UPDATE turns SET ended_at = started_at WHERE id = ?", (turn["id"],))
    path.unlink()
    return turn["id"]


def minified(diff: str) -> bool:
    """True when a changed line is too long to be hand-written: a bundle or minified file."""
    return any(len(line) > MINIFIED_LINE for line in diff.splitlines() if line[:1] in "+-")


def ignored(relative: str) -> bool:
    return _ignored(relative)


def unified_diff(file_path: str, before: str | None, after: str | None) -> str:
    lines = difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile="/dev/null" if before is None else f"a/{file_path}",
        tofile="/dev/null" if after is None else f"b/{file_path}",
    )
    # Mark missing final newlines so each line of the diff ends with one.
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


# sessions.tool for each AI tool's hooks.
SESSION_TOOL = {"claude": "claude-code", "codex": "codex", "antigravity": "antigravity"}


def _ensure_session(conn, repo, session_id: str, tool: str = "claude") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, repo_id, tool, started_at) VALUES (?, ?, ?, ?)",
        (session_id, repo["id"] if repo else None, SESSION_TOOL.get(tool, tool), now()),
    )


def _open_turn(conn, session_id: str | None):
    if not session_id:
        return None
    return conn.execute(
        "SELECT id, branch FROM turns WHERE session_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def _new_turn(conn, repo, session_id: str, prompt: str, tool: str = "claude"):
    branch = git.branch(Path(repo["path"])) if repo else None
    # The session row can be gone by now: closing a stale turn that recorded nothing, or
    # removing a project, deletes sessions left without turns. Bring it back.
    _ensure_session(conn, repo, session_id, tool)
    cursor = conn.execute(
        "INSERT INTO turns (session_id, branch, prompt, started_at) VALUES (?, ?, ?, ?)",
        (session_id, branch, prompt, now()),
    )
    return {"id": cursor.lastrowid, "branch": branch}


def _follow_moves(conn, repos, event: str, payload: dict, cwd: Path):
    """Picks up a connected repo whose folder was moved or renamed, when a hook fires in
    its new place. Only looks when a connected repo's folder is missing, so the usual
    case costs nothing."""
    if all(Path(r["path"]).is_dir() for r in repos):
        return repos
    targets = _edit_targets(payload)
    where = (cwd / targets[0]).resolve().parent if event == "pre-tool-use" and targets else cwd
    if _repo_for(repos, where) is not None:
        return repos
    root = git.toplevel(where) if where.is_dir() else None
    if root is None or install.find_moved(conn, root) is None:
        return repos
    return conn.execute("SELECT id, path FROM repos WHERE claude = 1").fetchall()


def _repo_for(repos, path: Path):
    """The tracked repo containing path, innermost first for nested repos."""
    matches = [r for r in repos if path == Path(r["path"]) or path.is_relative_to(r["path"])]
    return max(matches, key=lambda r: len(r["path"]), default=None)


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _ignored(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return any(fnmatch(relative, pattern) or fnmatch(name, pattern) for pattern in IGNORED)
