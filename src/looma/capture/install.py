"""`looma init` registers a repo; its own git history needs no hooks.
`looma connect claude` (or codex, or antigravity) opts that repo into recording that AI
tool: a git post-commit hook and the tool's user-level hooks. `looma disconnect` undoes it."""

import json
import os
import shlex
import sys
from pathlib import Path

from looma import git
from looma.store import connect, now

MARKER = "# looma: records commits"
RECORD = "-m looma record"
# What Looma's hooks and files were called when it was DevDash: found and replaced by
# refresh(), so a computer set up before the rename keeps recording.
LEGACY_MARKER = "# devdash: records commits"
LEGACY_RECORD = "devdash record"
LEGACY_ANTIGRAVITY_HOOK = "devdash"
# Looma's folder inside a project, and its old name.
FOLDER = ".looma"
LEGACY_FOLDER = ".devdash"

# Claude Code hook event -> looma record event. PreToolUse snapshots files before the
# AI edits them, which is what lets a turn's diff leave out manual edits.
CLAUDE_EVENTS = {
    "UserPromptSubmit": ("user-prompt-submit", None),
    "PreToolUse": ("pre-tool-use", "Edit|MultiEdit|Write|NotebookEdit"),
    # After each shell command, the files it changed are noted, so an interrupted prompt
    # (Esc sends no Stop) still keeps them.
    "PostToolUse": ("post-tool-use", "Bash"),
    "Stop": ("stop", None),
    # Quitting or /clear ends a prompt whose Stop never came.
    "SessionEnd": ("session-end", None),
}
# The AI tools whose requests can be recorded. A project records one of them; `repos.claude`
# says it records at all, `repos.ai` which one.
TOOLS = ("claude", "codex", "antigravity")
TOOL_NAMES = {"claude": "Claude Code", "codex": "ChatGPT Codex", "antigravity": "Antigravity"}
# Codex's hooks mirror Claude Code's; its file edits go through apply_patch.
CODEX_EVENTS = {
    "UserPromptSubmit": ("user-prompt-submit", None),
    "PreToolUse": ("pre-tool-use", "apply_patch"),
    "Stop": ("stop", None),
}
# Antigravity has no hook that carries the prompt: the first model call of a request starts
# it (the prompt comes from its transcript) and Stop ends it. Its PreToolUse hooks must say
# whether a tool may run, so Looma stays out of them; the files a request changed are
# found by comparing the project at its start and end, as for edits Claude makes by shell.
ANTIGRAVITY_EVENTS = {"PreInvocation": "user-prompt-submit", "Stop": "stop"}
ANTIGRAVITY_HOOK = "looma"  # the name of Looma's entry in Antigravity's hooks.json


class InitError(RuntimeError):
    pass


def init(path: Path) -> Path:
    """Tracks the repo at path. A project Looma already knows from another folder (it was
    moved, renamed or cloned again, and the old folder is gone) keeps its history and its
    Claude setting instead of starting over."""
    root = _root(path)
    first = git.root_commit(root)
    conn = connect()
    with conn:
        if conn.execute("SELECT 1 FROM repos WHERE path = ?", (str(root),)).fetchone():
            conn.execute("UPDATE repos SET root_commit = COALESCE(root_commit, ?) WHERE path = ?",
                         (first, str(root)))
            moved = None
        else:
            moved = _moved(conn, root, first)
            if moved is None:
                conn.execute(
                    "INSERT INTO repos (path, name, added_at, root_commit) VALUES (?, ?, ?, ?)",
                    (str(root), root.name, now(), first),
                )
    conn.close()
    if moved is not None and moved["claude"]:
        install_git_hook(root)
    return root


def find_moved(conn, root: Path):
    """The tracked repo that now lives at root, if it was moved there; its path is updated.
    For hooks firing in a folder Looma doesn't know yet."""
    with conn:
        return _moved(conn, root, git.root_commit(root))


def _moved(conn, root: Path, first: str | None):
    # Only repos whose folder is gone, so two clones side by side stay two projects.
    gone = [row for row in conn.execute("SELECT id, path, name, claude, root_commit FROM repos")
            if not _still_there(Path(row["path"]))]
    matches = [row for row in gone if first and row["root_commit"] == first]
    if not matches:
        # Tracked before first commits were stored: the same folder name will do.
        matches = [row for row in gone if row["root_commit"] is None
                   and row["name"].casefold() == root.name.casefold()]
    if len(matches) != 1:
        return None
    [row] = matches
    conn.execute("UPDATE repos SET path = ?, name = ?, root_commit = COALESCE(?, root_commit) WHERE id = ?",
                 (str(root), root.name, first, row["id"]))
    return row


def _still_there(path: Path) -> bool:
    """The tracked folder is where it was: a git project at its own top, or a folder
    without git that still exists."""
    if not path.is_dir():
        return False
    top = git.toplevel(path)
    return top is None or top == path


def refresh() -> list[str]:
    """Brings every tracked repo's setup up to date with this install: hooks name the
    Python that runs Looma, which changes when it's reinstalled or moved. Returns
    what couldn't be fixed, one line each."""
    conn = connect()
    rows = conn.execute("SELECT path, claude FROM repos").fetchall()
    conn.close()
    problems = []
    for row in rows:
        root = Path(row["path"])
        if git.toplevel(root) is None:
            continue
        init(root)  # stores the first commit for repos tracked before it was
        _move_legacy_folder(root)
        if row["claude"]:
            try:
                install_git_hook(root)
            except (InitError, OSError, git.GitError) as error:
                problems.append(str(error))
    for tool in tools_in_use():
        try:
            install_tool_hooks(tool)
        except (InitError, OSError) as error:
            problems.append(f"{TOOL_NAMES[tool]} hooks: {error}")
    return problems


def _move_legacy_folder(root: Path) -> None:
    """A project set up before the rename keeps its prompt file and request inbox in
    .devdash/; they move to .looma/, which git is told to ignore here like the old one."""
    old, new = root / LEGACY_FOLDER, root / FOLDER
    if not old.is_dir() or new.exists():
        return
    try:
        old.rename(new)
        _exclude_from_git(root, f"/{FOLDER}/")
    except (OSError, git.GitError):
        pass  # read from where it is: the readers look in both folders


def tools_in_use() -> list[str]:
    """The AI tools some project records, in TOOLS order."""
    conn = connect()
    used = {row[0] or "claude" for row in conn.execute("SELECT ai FROM repos WHERE claude = 1")}
    conn.close()
    return [tool for tool in TOOLS if tool in used]


def connect_tool(path: Path, tool: str) -> Path:
    """Records the AI tool's requests in the repo at path, in place of any other tool."""
    if tool not in TOOLS:
        raise InitError(f"Looma can't record {tool}; it can record {', '.join(TOOLS)}")
    if git.toplevel(path) is None:
        raise InitError(f"Recording {TOOL_NAMES[tool]} needs git, and {path} isn't a git repository. "
                        "Run git init in it first.")
    root = init(path)
    install_git_hook(root)
    install_tool_hooks(tool)
    if tool == "claude":
        _remove_repo_claude_hooks(root)
    _exclude_from_git(root, f"/{FOLDER}/")
    conn = connect()
    with conn:
        conn.execute("UPDATE repos SET claude = 1, ai = ? WHERE path = ?", (tool, str(root)))
    conn.close()
    _remove_unused_hooks()  # the tool this repo recorded before, if no other repo uses it
    return root


def disconnect(path: Path) -> Path:
    """Stops recording AI changes in a repo. Recorded history is kept."""
    root = _root(path)
    if git.toplevel(root) is not None:
        remove_git_hook(root)
    conn = connect()
    with conn:
        conn.execute("UPDATE repos SET claude = 0, ai = NULL WHERE path = ?", (str(root),))
    conn.close()
    _remove_unused_hooks()
    return root


def _remove_unused_hooks() -> None:
    """Takes out the hooks of every tool no project records any more."""
    used = tools_in_use()
    for tool in TOOLS:
        if tool not in used:
            remove_tool_hooks(tool)


def remove_all_hooks() -> list[Path]:
    """Takes Looma out of every project and AI tool, before Looma itself is uninstalled:
    each project's git hook and each AI tool's hooks, which would otherwise call a Looma
    that's gone. Returns the projects it took the git hook out of."""
    conn = connect()
    rows = conn.execute("SELECT path FROM repos WHERE claude = 1").fetchall()
    conn.close()
    cleaned = []
    for row in rows:
        root = Path(row["path"])
        if git.toplevel(root) is None:
            continue  # moved or deleted: there's no hook left to take out
        remove_git_hook(root)
        _remove_repo_claude_hooks(root)
        cleaned.append(root)
    for tool in TOOLS:
        remove_tool_hooks(tool)
    return cleaned


def remove(repo_id: int) -> bool:
    """Stops tracking a repo and forgets what Looma recorded about it. The repo's own
    files and git history are untouched; Looma's git hook comes out if an AI tool was
    connected. Works even when the folder has moved or been deleted."""
    conn = connect()
    row = conn.execute("SELECT path, claude FROM repos WHERE id = ?", (repo_id,)).fetchone()
    conn.close()
    if row is None:
        return False
    path = Path(row["path"])
    if row["claude"] and git.toplevel(path) is not None:
        disconnect(path)
    conn = connect()
    with conn:
        conn.execute("DELETE FROM undone WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM turn_files WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM turn_repos WHERE repo_id = ?", (repo_id,))
        # A session may have touched other repos too, so it is detached, not deleted;
        # turns left with nothing recorded go, then sessions left with no turns.
        conn.execute("UPDATE sessions SET repo_id = NULL WHERE repo_id = ?", (repo_id,))
        conn.execute(
            """DELETE FROM turns WHERE ended_at IS NOT NULL
               AND id NOT IN (SELECT turn_id FROM changes WHERE turn_id IS NOT NULL)
               AND id NOT IN (SELECT turn_id FROM turn_files)
               AND session_id IN (SELECT id FROM sessions WHERE repo_id IS NULL)"""
        )
        conn.execute(
            "DELETE FROM sessions WHERE repo_id IS NULL AND id NOT IN (SELECT session_id FROM turns)"
        )
        conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
    conn.close()
    if row["claude"]:
        _remove_unused_hooks()
    return True


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write(path: Path, text: str) -> None:
    # UTF-8 with \n endings on every system: sh can't run a hook with \r\n endings.
    path.write_text(text, encoding="utf-8", newline="\n")


def _root(path: Path) -> Path:
    root = git.toplevel(path)
    if root is None:
        found = find_repos(path) if path.is_dir() else []
        if found:
            names = ", ".join(str(p) for p in found[:6]) + (" …" if len(found) > 6 else "")
            raise InitError(f"{path} isn't a git project itself, but these inside it are: {names}. Add one of them.")
        if not path.is_dir():
            raise InitError(f"No folder at {path}")
        # Not a git project: Looma reads its files; branches, commits and AI recording stay off.
        return path.resolve()
    return root


# Folders never searched for projects: dependencies, build output, caches.
SKIP_SEARCH = {"node_modules", ".venv", "venv", "env", ".tox", "__pycache__", "dist", "build", "target",
               ".next", ".cache", "site-packages", ".git", "vendor", "Library", "AppData"}
SEARCH_DEPTH = 3
MAX_FOUND = 50
WORKSPACES = "workspaces"


def find_repos(folder: Path, depth: int = SEARCH_DEPTH) -> list[Path]:
    """Git projects in folder and up to `depth` folders down; not inside each other."""
    found: list[Path] = []

    def walk(current: Path, left: int) -> None:
        if len(found) >= MAX_FOUND:
            return
        if (current / ".git").exists():
            found.append(current.resolve())
            return
        if left == 0:
            return
        try:
            children = sorted(p for p in current.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            return
        for child in children:
            if child.name not in SKIP_SEARCH and not child.name.startswith("."):
                walk(child, left - 1)

    walk(folder, depth)
    return found


def workspaces() -> list[str]:
    """Folders `looma init` was pointed at that hold projects, to offer when adding one."""
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (WORKSPACES,)).fetchone()
    conn.close()
    return json.loads(row["value"]) if row else []


def add_workspace(folder: Path) -> list[Path]:
    """Remembers a folder of projects, without adding any or changing anything in them.
    Returns the projects in it."""
    folder = folder.resolve()
    found = find_repos(folder)
    known = workspaces()
    if str(folder) not in known:
        conn = connect()
        with conn:
            conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)",
                         (WORKSPACES, json.dumps([*known, str(folder)])))
        conn.close()
    return found


def candidates() -> list[dict]:
    """Projects in the remembered folders that aren't added yet."""
    conn = connect()
    # normcase: on Windows C:\Users\Me and c:\users\me are the same folder.
    added = {os.path.normcase(row["path"]) for row in conn.execute("SELECT path FROM repos")}
    conn.close()
    result, seen = [], set()
    for folder in workspaces():
        if not Path(folder).is_dir():
            continue
        for repo in find_repos(Path(folder)):
            key = os.path.normcase(str(repo))
            if key not in added and key not in seen:
                seen.add(key)
                result.append({"path": str(repo), "name": repo.name, "folder": folder})
    return result


def record_command(event: str, tool: str = "claude") -> str:
    # Hooks of tools other than Claude Code name theirs, since their payloads differ.
    named = f" --tool {tool}" if tool != "claude" else ""
    # The absolute interpreter path keeps hooks working when looma is not on PATH;
    # -I stops a folder named looma in the repo from shadowing the package. Forward
    # slashes, since on Windows these run in Git's sh, where a backslash escapes.
    return f"{_quote(Path(sys.executable).as_posix())} -I {RECORD} {event}{named}"


def _quote(path: str) -> str:
    """A path for a hook command. On Windows the same command may run in Git's sh or in
    cmd (Antigravity), which share only double quotes; a user name with a space puts one
    in the path to pipx's Python."""
    if os.name != "nt":
        return shlex.quote(path)
    return f'"{path}"' if any(c in path for c in " &()^%!'") else path


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def codex_hooks_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"


def antigravity_hooks_path() -> Path:
    # Antigravity's global hooks, shared by its CLI (agy), app and IDE.
    return Path(os.environ.get("LOOMA_ANTIGRAVITY_CONFIG", Path.home() / ".gemini" / "config")) / "hooks.json"


def tool_hooks_path(tool: str) -> Path:
    return {"claude": claude_settings_path, "codex": codex_hooks_path, "antigravity": antigravity_hooks_path}[tool]()


def install_tool_hooks(tool: str) -> Path:
    return {"claude": install_claude_hooks, "codex": install_codex_hooks,
            "antigravity": install_antigravity_hooks}[tool]()


def remove_tool_hooks(tool: str) -> None:
    {"claude": remove_claude_hooks, "codex": remove_codex_hooks, "antigravity": remove_antigravity_hooks}[tool]()


def _load(path: Path) -> tuple[str, dict]:
    text = _read(path) if path.exists() else ""
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise InitError(f"{path} isn't valid JSON, so Looma can't add its hooks")
    if not isinstance(data, dict):
        raise InitError(f"{path} isn't a JSON object, so Looma can't add its hooks")
    return text, data


def _save(path: Path, text: str, data: dict) -> None:
    new = json.dumps(data, indent=2) + "\n"
    if new != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, new)


def _install_event_hooks(path: Path, events: dict, tool: str) -> Path:
    # Claude Code's settings.json and Codex's hooks.json share one layout:
    # {"hooks": {event: [{"matcher": ..., "hooks": [{"type": "command", ...}]}]}}.
    text, settings = _load(path)
    hooks = settings.setdefault("hooks", {})
    for tool_event, (event, matcher) in events.items():
        groups = [g for g in hooks.get(tool_event, []) if not _is_ours(g)]
        group = {"hooks": [{"type": "command", "command": record_command(event, tool)}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks[tool_event] = [*groups, group]
    _save(path, text, settings)
    return path


def _event_hooks_installed(path: Path, events: dict) -> bool:
    if not path.exists():
        return False
    try:
        hooks = json.loads(_read(path)).get("hooks", {})
    except (json.JSONDecodeError, AttributeError):
        return False
    return all(any(_is_ours(g) for g in hooks.get(event, [])) for event in events)


def install_claude_hooks() -> Path:
    # User-level hooks fire in every Claude Code session, wherever it starts (a repo,
    # or the parent folder of several). `looma record` exits at once unless an edited
    # file is inside a tracked repo.
    return _install_event_hooks(claude_settings_path(), CLAUDE_EVENTS, "claude")


def remove_claude_hooks() -> None:
    settings_path = claude_settings_path()
    if not settings_path.exists():
        return
    try:
        text, settings = _load(settings_path)
    except InitError:
        return  # not ours to fix, and nothing of ours can be found in it
    _drop_our_hooks(settings)
    _save(settings_path, text, settings)


def install_codex_hooks() -> Path:
    # User-level, so they fire in every Codex session (CLI, IDE extension or app). Codex
    # runs them only once the user has reviewed them in /hooks.
    return _install_event_hooks(codex_hooks_path(), CODEX_EVENTS, "codex")


def remove_codex_hooks() -> None:
    path = codex_hooks_path()
    if not path.exists():
        return
    try:
        text, settings = _load(path)
    except InitError:
        return
    _drop_our_hooks(settings)
    if settings:
        _save(path, text, settings)
    else:
        path.unlink()  # only ours was in it


def install_antigravity_hooks() -> Path:
    # Antigravity's hooks.json maps a name to its events; Looma's entry is its own name.
    path = antigravity_hooks_path()
    text, config = _load(path)
    # Antigravity wants JSON back from every hook, so `echo {}` answers for Looma when it
    # can't be reached (its container stopped, say); it reads the same in sh and cmd.
    config.pop(LEGACY_ANTIGRAVITY_HOOK, None)
    config[ANTIGRAVITY_HOOK] = {
        event: [{"type": "command", "command": f"{record_command(ours, 'antigravity')} || echo {{}}", "timeout": 30}]
        for event, ours in ANTIGRAVITY_EVENTS.items()
    }
    _save(path, text, config)
    return path


def remove_antigravity_hooks() -> None:
    path = antigravity_hooks_path()
    if not path.exists():
        return
    try:
        text, config = _load(path)
    except InitError:
        return
    found = [config.pop(name, None) for name in (ANTIGRAVITY_HOOK, LEGACY_ANTIGRAVITY_HOOK)]
    if not any(entry is not None for entry in found):
        return
    if config:
        _save(path, text, config)
    else:
        path.unlink()


def _drop_our_hooks(settings: dict) -> None:
    hooks = settings.get("hooks", {})
    for claude_event in list(hooks):
        hooks[claude_event] = [g for g in hooks[claude_event] if not _is_ours(g)]
        if not hooks[claude_event]:
            del hooks[claude_event]
    if not hooks:
        settings.pop("hooks", None)


def _remove_repo_claude_hooks(root: Path) -> None:
    # Version 0.1.0 put the hooks in the repo; with user-level hooks they would record twice.
    settings_path = root / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return
    settings = json.loads(_read(settings_path))
    _drop_our_hooks(settings)
    if settings:
        _write(settings_path, json.dumps(settings, indent=2) + "\n")
    else:
        settings_path.unlink()


def install_git_hook(root: Path) -> Path:
    hook = git.hooks_dir(root) / "post-commit"
    ours = f"{MARKER}\n{record_command('post-commit')} || true\n"
    if not hook.exists():
        hook.parent.mkdir(parents=True, exist_ok=True)
        _write(hook, "#!/bin/sh\n" + ours)
    else:
        text = _read(hook)
        first = text.splitlines()[0] if text else ""
        if not first.startswith("#!") or not any(sh in first for sh in ("sh", "bash", "zsh")):
            raise InitError(
                f"{hook} is not a shell script, so looma can't add to it. "
                f"Add this line to it yourself: {record_command('post-commit')}"
            )
        lines = text.splitlines(keepends=True)
        if _marker_line(lines) is not None:
            # Replace our two lines, e.g. after Python moved or from before the rename.
            at = _marker_line(lines)
            lines[at : at + 2] = [ours]
        else:
            lines.append(("" if text.endswith("\n") else "\n") + ours)
        _write(hook, "".join(lines))
    os.chmod(hook, 0o755)
    return hook


def remove_git_hook(root: Path) -> None:
    hook = git.hooks_dir(root) / "post-commit"
    if not hook.exists():
        return
    lines = _read(hook).splitlines(keepends=True)
    at = _marker_line(lines)
    if at is None:
        return
    del lines[at : at + 2]
    if "".join(lines).strip() in ("", "#!/bin/sh"):
        hook.unlink()  # only ours was in it
    else:
        _write(hook, "".join(lines))


def _exclude_from_git(root: Path, pattern: str) -> None:
    # .git/info/exclude ignores the path on this machine without editing .gitignore.
    exclude = (root / git.run(root, "rev-parse", "--git-path", "info/exclude").strip()).resolve()
    text = _read(exclude) if exclude.exists() else ""
    if pattern not in text.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        _write(exclude, text + ("" if not text or text.endswith("\n") else "\n") + pattern + "\n")


def git_hook_installed(root: Path) -> bool:
    hook = git.hooks_dir(root) / "post-commit"
    return hook.exists() and MARKER in _read(hook)


def _marker_line(lines: list[str]) -> int | None:
    """Where Looma's two lines start in a post-commit hook, under its marker or the old one."""
    return next((i for i, line in enumerate(lines) if line.startswith((MARKER, LEGACY_MARKER))), None)


def claude_hooks_installed() -> bool:
    return _event_hooks_installed(claude_settings_path(), CLAUDE_EVENTS)


def codex_hooks_installed() -> bool:
    return _event_hooks_installed(codex_hooks_path(), CODEX_EVENTS)


def antigravity_hooks_installed() -> bool:
    path = antigravity_hooks_path()
    if not path.exists():
        return False
    try:
        ours = json.loads(_read(path)).get(ANTIGRAVITY_HOOK) or {}
        return all(any(_is_ours({"hooks": [h]}) for h in ours.get(event, [])) for event in ANTIGRAVITY_EVENTS)
    except (json.JSONDecodeError, AttributeError):
        return False


def setup_steps(root: Path, ai: str | None) -> list[dict]:
    """What it takes to use Looma in a repo that records `ai` (None: no AI tool), each
    step with whether it is done. `done` is None where Looma can't check (something
    the user does elsewhere)."""
    if git.toplevel(root) is None:
        return [
            {"id": "track", "title": "Track the project",
             "detail": "Looma reads the files in this folder. It isn't a git repository, so branches, "
                       "changes, pull requests and AI recording are off.",
             "command": f"looma init {shlex.quote(str(root))}", "done": True},
            {"id": "serve", "title": "Start Looma",
             "detail": "Run this in a terminal and leave it running, then open http://127.0.0.1:8765.",
             "command": "looma serve", "done": None},
            {"id": "git", "title": "Optional: turn on git",
             "detail": "Make the folder a git repository to see branches and changes, and to record your AI tool.",
             "command": f"git -C {shlex.quote(str(root))} init", "done": False},
        ]
    steps = [
        {"id": "track", "title": "Track the project",
         "detail": "Looma reads your branches and commits straight from git. Nothing is added to the project.",
         "command": f"looma init {shlex.quote(str(root))}", "done": True},
        {"id": "serve", "title": "Start Looma",
         "detail": "Run this in a terminal and leave it running, then open http://127.0.0.1:8765.",
         "command": "looma serve", "done": None},
    ]
    if not ai:
        steps.append({"id": "ai", "title": "Optional: record your AI tool",
                      "detail": "Pick your AI coding tool to also record each request it worked on, next to the change it made.",
                      "command": f"looma connect claude {shlex.quote(str(root))}", "done": False})
        return steps
    steps.append({"id": "git-hook", "title": "Git hook installed",
                  "detail": "Links each commit to the AI request behind it. Added to any hook you already have.",
                  "command": None, "done": git_hook_installed(root)})
    if ai == "codex":
        return steps + [
            {"id": "codex-hooks", "title": "Codex hooks installed",
             "detail": f"Records each request to Codex and the files it changed. Lives in {codex_hooks_path()}.",
             "command": None, "done": codex_hooks_installed()},
            {"id": "codex-review", "title": "Trust Looma's hooks in Codex",
             "detail": "Codex runs a new hook only after you review it: open Codex, type /hooks and trust "
                       "Looma's three hooks. Restart Codex sessions that were already open.",
             "command": "/hooks", "done": None},
        ]
    if ai == "antigravity":
        return steps + [
            {"id": "antigravity-hooks", "title": "Antigravity hooks installed",
             "detail": f"Records each request to Antigravity and the files it changed. Lives in {antigravity_hooks_path()}.",
             "command": None, "done": antigravity_hooks_installed()},
            {"id": "restart", "title": "Restart Antigravity",
             "detail": "Antigravity loads hooks when it starts, so restart agy or the Antigravity app if it's open.",
             "command": None, "done": None},
        ]
    return steps + [
        {"id": "claude-hooks", "title": "Claude Code hooks installed",
         "detail": f"Records Claude Code edits as they happen. Lives in {claude_settings_path()}.",
         "command": None, "done": claude_hooks_installed()},
        {"id": "restart", "title": "Restart open Claude Code sessions",
         "detail": "Sessions that were already open don't load the new hooks until restarted.",
         "command": None, "done": None},
        {"id": "app-instructions", "title": "For Claude app chats: add the project instructions",
         "detail": "App chats edit files outside Claude Code. Paste this into the Claude project's instructions "
                   "so Claude notes each request before it commits.",
         "command": INSTRUCTIONS.rstrip(), "done": None},
    ]


AI_TOOLS = [
    {"id": "claude", "name": "Claude", "available": True,
     "description": "Records each request to Claude Code or a Claude app chat, with the change it made."},
    {"id": "codex", "name": "ChatGPT Codex", "available": True,
     "description": "Records each request to Codex (CLI, IDE extension or app), with the change it made."},
    {"id": "antigravity", "name": "Gemini Antigravity", "available": True,
     "description": "Records each request to Antigravity (agy, the app or the IDE), with the change it made."},
    {"id": "copilot", "name": "GitHub Copilot", "available": False,
     "description": "Coming later. Copilot commits are already marked as AI from their co-author line."},
    {"id": "cursor", "name": "Cursor", "available": False,
     "description": "Coming later. Cursor commits are already marked as AI from their co-author line."},
]


INSTRUCTIONS = """\
This repo is tracked by Looma, which records what AI changes and why.
When you change files here:
1. Before each commit, append the user's request that led to the change to
   .looma/prompt.md at the repo root, word for word. Create the file if needed.
   Separate requests with a line containing only ---.
2. Commit each logical change on its own, and end the message with
   Co-Authored-By: Claude <noreply@anthropic.com>
3. Never commit .looma/ (git already ignores it on this machine).
4. If you leave the change uncommitted, write one file per request instead, once
   your edits are done: .looma/turns/<UTC time, like 20261031T183000Z>.json with
   {"prompt": "<the request, word for word>",
    "at": "<UTC time the request was made>",
    "files": [{"path": "<path from the repo root>",
               "before": "<the whole file before your first edit, or null if you created it>"}]}
   Looma records it as the request's change, then removes the file.
"""


def hooks_problem(tool: str) -> str | None:
    """What's wrong with the AI tool's hooks, in a sentence, or None when each one is there
    and calls this install of Looma. A hook naming a Python that was since removed fails
    before Looma runs, so nothing would be logged: this is the only way to see it."""
    path = tool_hooks_path(tool)
    name = TOOL_NAMES[tool]
    if not path.exists():
        return f"{name}'s hooks aren't installed ({path} doesn't exist)."
    try:
        _, data = _load(path)
    except InitError as error:
        return str(error)
    if tool == "antigravity":
        entry = data.get(ANTIGRAVITY_HOOK) or {}
        found = {event: [h.get("command", "") for h in entry.get(event) or [] if isinstance(h, dict)]
                 for event in ANTIGRAVITY_EVENTS}
        wanted = {event: f"{record_command(ours, tool)} || echo {{}}" for event, ours in ANTIGRAVITY_EVENTS.items()}
    else:
        events = CLAUDE_EVENTS if tool == "claude" else CODEX_EVENTS
        hooks = data.get("hooks") or {}
        found = {event: [h.get("command", "") for g in hooks.get(event) or [] if isinstance(g, dict) and _is_ours(g)
                         for h in g.get("hooks", [])] for event in events}
        wanted = {event: record_command(ours, tool) for event, (ours, _) in events.items()}
    missing = [event for event, commands in found.items() if not commands]
    if missing:
        return f"{name} is missing Looma's {', '.join(missing)} hook{'s' if len(missing) > 1 else ''}."
    stale = [event for event, commands in found.items() if wanted[event] not in commands]
    if stale:
        return (f"{name}'s hooks call an older Looma, so {', '.join(stale)} may not be recorded. "
                "Repair them, or restart `looma serve`.")
    return None


def _is_ours(group: dict) -> bool:
    # `python -I -m looma record …`, or an older form calling `looma record` (or, from
    # before the rename, `devdash record`) some other way, which is replaced by this one.
    return any(name in h.get("command", "") for h in group.get("hooks", []) for name in ("looma record", LEGACY_RECORD))
