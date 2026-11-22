"""What a branch changed against its base, read straight from git. Needs no hooks, so it
covers every change: the developer's own, and anything an AI tool committed."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from looma import git, paths
from looma.capture.record import AI_TRAILER, _ignored
from looma.store import connect

MAX_LINES_PER_FILE = 2000
MAX_UNTRACKED = 200
# How long a merged or deleted branch stays listed, marked as such, before Looma drops it.
RETIRE_AFTER = timedelta(minutes=15)

# Git only knows where functions start for some languages. These attributes turn its
# built-in drivers on, and add one for JavaScript and TypeScript, so each hunk header
# names the function or class the change sits in.
ATTRIBUTES = """\
*.py diff=python
*.go diff=golang
*.rs diff=rust
*.java diff=java
*.kt diff=kotlin
*.rb diff=ruby
*.php diff=php
*.cs diff=csharp
*.c diff=cpp
*.h diff=cpp
*.cpp diff=cpp
*.css diff=css
*.scss diff=css
*.html diff=html
*.md diff=markdown
*.js diff=looma-js
*.jsx diff=looma-js
*.mjs diff=looma-js
*.cjs diff=looma-js
*.ts diff=looma-js
*.tsx diff=looma-js
"""
# One pattern per line; the outer group is what git puts in the hunk header.
JS_FUNCNAME = "\n".join([
    r"^[ \t]*((export[ \t]+)?(default[ \t]+)?(async[ \t]+)?function\*?[ \t]+[A-Za-z_$][A-Za-z0-9_$]*.*)$",
    r"^[ \t]*((export[ \t]+)?(const|let|var)[ \t]+[A-Za-z_$][A-Za-z0-9_$]*[^=]*=[ \t]*(async[ \t]*)?(function|\(|[A-Za-z_$][A-Za-z0-9_$]*[ \t]*=>).*)$",
    r"^[ \t]*((export[ \t]+)?(default[ \t]+)?(abstract[ \t]+)?class[ \t]+[A-Za-z_$].*)$",
    r"^[ \t]*((export[ \t]+)?(interface|type|enum)[ \t]+[A-Za-z_$].*)$",
])

# Declarations on a changed line, to tell a new or deleted function from an edited one.
DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?(?:pub(?:\([\w:]+\))?\s+)?"
    r"(?:def|function\*?|class|func|fn|interface|type|enum|struct|trait)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b[^=]*=\s*(?:async\b\s*)?"
    r"(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)"
)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


class CompareError(ValueError):
    pass


def compare(repo: Path, base: str | None = None, head: str | None = None,
            uncommitted: bool = False) -> dict:
    """Everything `head` changed since it left `base`, like a pull request would show.
    With `uncommitted`, edits not yet committed on the checked-out branch are included."""
    current = git.branch(repo)
    head = head or current or "HEAD"
    base = base or default_base(repo)
    if base is None:
        raise CompareError("no main, master, develop or dev branch to compare against; pick one")
    for ref in (base, head):
        _verify(repo, ref)
    if uncommitted and head not in (current, "HEAD"):
        raise CompareError(f"uncommitted changes are only on the checked-out branch, {current}")

    # Compare with the newest copy of the base: after a pull request is merged on GitHub,
    # origin/Dev has the merge long before the local Dev is pulled.
    base_ref = freshest(repo, base)
    merge_base = fork_point(repo, base_ref, head)
    target = [] if uncommitted else [head]
    patch = _git_diff(repo, merge_base, *target)
    files = parse_patch(patch)
    not_committed = set()
    if uncommitted:
        files += _untracked(repo)
        not_committed = _not_committed(repo)
    for item in files:
        _finish(item)
        item["uncommitted"] = item["path"] in not_committed

    ahead = int(git.run(repo, "rev-list", "--count", f"{merge_base}..{head}").strip())
    behind = int(git.run(repo, "rev-list", "--count", f"{head}..{base_ref}").strip())
    return {
        "base": base,
        "base_ref": base_ref,
        "head": head,
        "merge_base": merge_base,
        "uncommitted": uncommitted,
        "ahead": ahead,
        "behind": behind,
        "commits": commits(repo, f"{merge_base}..{head}"),
        "files": files,
        "totals": {
            "files": len(files),
            "additions": sum(item["additions"] for item in files),
            "deletions": sum(item["deletions"] for item in files),
        },
    }


def state(repo: Path, uncommitted: bool = False) -> str:
    """A short fingerprint of everything the compare depends on: the checked-out branch,
    every branch tip (local and remote) and, with `uncommitted`, the files edited but not committed. The UI
    polls it and reloads only when it changes, so polling stays cheap."""
    parts = [
        git.branch(repo) or "",
        git.run(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/remotes"),
        git.run(repo, "rev-parse", "-q", "--verify", "HEAD", ok=(0, 1)),
    ]
    if uncommitted:
        status = git.run(repo, "-c", "core.quotePath=false", "status", "--porcelain=v1", "-z",
                         "--untracked-files=all")
        parts.append(status)
        # A second edit to an already-changed file leaves the status line the same, so
        # each file's size and modification time go into the fingerprint too.
        for entry in status.split("\x00"):
            name = entry[3:]
            if not name:
                continue
            try:
                info = (repo / name).stat()
                parts.append(f"{name} {info.st_size} {info.st_mtime_ns}")
            except OSError:
                parts.append(f"{name} gone")
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


# Usual names for the branch work gets merged into, in order of preference. Matched
# without regard to case, so Dev and dev both count.
BASE_NAMES = ["main", "master", "develop", "development", "dev"]


def default_base(repo: Path) -> str | None:
    """The branch to compare against: the remote's default branch when git knows it,
    else the first usual name found locally, else one that exists only on the remote."""
    try:
        remote_head = git.run(repo, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD").strip()
    except git.GitError:
        remote_head = ""
    local = {name.lower(): name for name in branches(repo)}
    remote = {name.split("/", 1)[1].lower(): name for name in _refs(repo, "refs/remotes/origin")
              if name != "origin/HEAD" and "/" in name}
    wanted = ([remote_head.split("/", 1)[-1].lower()] if remote_head else []) + BASE_NAMES
    for name in wanted:
        if name in local:
            return local[name]
    for name in wanted:
        if name in remote:
            return remote[name]
    return None


def _refs(repo: Path, prefix: str) -> list[str]:
    out = git.run(repo, "for-each-ref", "--format=%(refname:short)", prefix)
    return [line for line in out.splitlines() if line]


def branches(repo: Path) -> list[str]:
    out = git.run(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads")
    return [line for line in out.splitlines() if line]


def branch_details(repo: Path, base: str | None) -> list[dict]:
    """Each local branch, and whether it is already merged into the base or was deleted
    on the remote (it had an upstream that no longer exists, usually after a merged pull
    request). Such branches stay on this machine until removed with `git branch -d`."""
    base_ref = freshest(repo, base) if base else None
    out = git.run(repo, "for-each-ref", "--sort=-committerdate",
                  "--format=%(refname:short)%00%(upstream)%00%(upstream:track)", "refs/heads")
    result = []
    for line in out.splitlines():
        name, upstream, track = (line.split("\x00") + ["", ""])[:3]
        if not name:
            continue
        # A branch just made from the base, with nothing committed yet, is also part of
        # the base; it is new, not merged.
        merged = (bool(base_ref) and name != base and _is_ancestor(repo, name, base_ref)
                  and not _untouched(repo, name))
        result.append({"name": name, "merged": merged, "gone": bool(upstream) and "gone" in track})
    return result


def retire(key: str, details: list[dict], current: str | None, now: datetime | None = None) -> list[dict]:
    """Merged and deleted branches stay listed for RETIRE_AFTER from when Looma first saw
    them so, each with the time it goes (`removes_at`); after that they are left out. The
    checked-out branch always stays. When each was first seen is kept in the database, so
    a restart doesn't start the clock again. `key` names the project."""
    now = now or datetime.now(timezone.utc)
    setting = f"retired-branches:{key}"
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (setting,)).fetchone()
    seen = json.loads(row["value"]) if row else {}
    kept, still = [], {}
    for branch in details:
        if not (branch["merged"] or branch["gone"]):
            kept.append({**branch, "removes_at": None})
            continue
        first = seen.get(branch["name"]) or now.isoformat(timespec="seconds")
        still[branch["name"]] = first
        removes_at = datetime.fromisoformat(first) + RETIRE_AFTER
        if removes_at > now or branch["name"] == current:
            kept.append({**branch, "removes_at": removes_at.isoformat(timespec="seconds")})
    # A branch that was deleted, or got new work, is forgotten; seen again, its clock restarts.
    if still != seen:
        if still:
            conn.execute("INSERT INTO settings (name, value) VALUES (?, ?) "
                         "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (setting, json.dumps(still)))
        else:
            conn.execute("DELETE FROM settings WHERE name = ?", (setting,))
        conn.commit()
    conn.close()
    return kept


def fork_point(repo: Path, base_ref: str, head: str) -> str:
    """Where `head`'s own work starts. Usually where it left the base; but a branch made
    from another branch that isn't merged yet would then count that branch's commits as
    its own too, so when the branch's reflog says where it was made, and that is later,
    it starts there. A rebase or an expired reflog falls back to where it left the base."""
    merge_base = git.run(repo, "merge-base", base_ref, head).strip()
    created = _created_at(repo, head)
    if (created and created != merge_base and _is_ancestor(repo, merge_base, created)
            and _is_ancestor(repo, created, head)):
        return created
    return merge_base


def _reflog(repo: Path, branch: str) -> list[tuple[str, str]]:
    """A local branch's reflog, oldest first, as (commit, what happened); empty for
    anything that isn't a local branch."""
    try:
        out = git.run(repo, "reflog", "show", "--format=%H%x00%gs", f"refs/heads/{branch}")
    except git.GitError:
        return []
    entries = [tuple(line.split("\x00", 1)) for line in out.splitlines() if "\x00" in line]
    return entries[::-1]


def _created_at(repo: Path, branch: str) -> str | None:
    """The commit a local branch was made at, while its reflog still goes back that far."""
    entries = _reflog(repo, branch)
    if not entries:
        return None
    sha, subject = entries[0]
    if not subject.startswith("branch: Created from "):
        return None
    source = subject.removeprefix("branch: Created from ")
    # Checking out a branch that exists on the remote makes a local copy of it: its
    # commits there are the branch's own, not where it was made.
    if source.endswith("/" + branch):
        return None
    return sha


def _untouched(repo: Path, branch: str) -> bool:
    """True when the branch was made and nothing has moved it since."""
    entries = _reflog(repo, branch)
    return len(entries) == 1 and entries[0][1].startswith("branch: Created from ")


def freshest(repo: Path, branch: str) -> str:
    """The branch's upstream (origin/Dev) when it is ahead of the local copy, else the
    branch itself. Only a fast-forward counts: if the local branch has its own commits,
    the local one is what the user means."""
    try:
        upstream = git.run(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}").strip()
    except git.GitError:
        return branch
    if upstream and _is_ancestor(repo, branch, upstream) and not _is_ancestor(repo, upstream, branch):
        return upstream
    return branch


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    try:
        git.run(repo, "merge-base", "--is-ancestor", older, newer)
        return True
    except git.GitError:
        return False


def _not_committed(repo: Path) -> set[str]:
    """Files that differ from the last commit: edited, staged or new."""
    changed = git.run(repo, "-c", "core.quotePath=false", "diff", "--name-only", "-z", "HEAD", "--", ok=(0, 128))
    untracked = git.run(repo, "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z")
    return {name for name in (changed + untracked).split("\x00") if name}


def commits(repo: Path, revision_range: str) -> list[dict]:
    out = git.run(repo, "log", "--format=%H%x00%an%x00%aI%x00%s%x00%b%x1e", revision_range)
    result = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, author, authored_at, subject, body = record.split("\x00", 4)
        result.append({
            "sha": sha,
            "short_sha": sha[:7],
            "author": author,
            "authored_at": authored_at,
            "subject": subject,
            "ai": bool(AI_TRAILER.search(body)),
        })
    return result


def parse_patch(text: str) -> list[dict]:
    """Turns `git diff` output into one entry per file, each with its hunks and lines."""
    files: list[dict] = []
    item = hunk = None
    old_line = new_line = 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            item = _new_file(line)
            files.append(item)
            hunk = None
        elif item is None:
            continue
        elif hunk is None:
            match = HUNK.match(line)
            if match:
                old_line, new_line = int(match[1]), int(match[3])
                hunk = _new_hunk(match)
                item["hunks"].append(hunk)
            else:
                _read_header(item, line)
        elif line.startswith("@@"):
            match = HUNK.match(line)
            old_line, new_line = int(match[1]), int(match[3])
            hunk = _new_hunk(match)
            item["hunks"].append(hunk)
        elif line[:1] in ("+", "-", " "):
            kind = {"+": "add", "-": "delete", " ": "context"}[line[0]]
            entry = {"kind": kind, "text": line[1:],
                     "old": old_line if kind != "add" else None,
                     "new": new_line if kind != "delete" else None}
            old_line += kind != "add"
            new_line += kind != "delete"
            item["additions"] += kind == "add"
            item["deletions"] += kind == "delete"
            item["_lines"] += 1
            if item["_lines"] > MAX_LINES_PER_FILE:
                item["truncated"] = True
            else:
                hunk["lines"].append(entry)
    return files


def _new_file(line: str) -> dict:
    # Only used when git prints no ---/+++ lines (binary files, mode changes).
    rest = line[len("diff --git a/"):]
    half = (len(rest) - 3) // 2
    path = rest[:half] if rest[half:half + 3] == " b/" else rest
    return {"path": path, "old_path": None, "status": "modified", "binary": False,
            "generated": False, "truncated": False, "additions": 0, "deletions": 0,
            "functions": [], "hunks": [], "_lines": 0}


def _read_header(item: dict, line: str) -> None:
    if line.startswith("new file mode"):
        item["status"] = "added"
    elif line.startswith("deleted file mode"):
        item["status"] = "deleted"
    elif line.startswith("rename from "):
        item["status"] = "renamed"
        item["old_path"] = _unquote(line[len("rename from "):])
    elif line.startswith("rename to "):
        item["path"] = _unquote(line[len("rename to "):])
    elif line.startswith("Binary files "):
        item["binary"] = True
    elif line.startswith("+++ ") and line[4:] != "/dev/null":
        item["path"] = _unquote(line[4:])[2:]
    elif line.startswith("--- ") and line[4:] != "/dev/null" and item["status"] == "deleted":
        item["path"] = _unquote(line[4:])[2:]


def _new_hunk(match: re.Match) -> dict:
    return {
        "old_start": int(match[1]), "old_lines": int(match[2] or 1),
        "new_start": int(match[3]), "new_lines": int(match[4] or 1),
        "context": match[5].strip() or None,
        "lines": [],
    }


def _finish(item: dict) -> None:
    del item["_lines"]
    if _ignored(item["path"]):
        # Lockfiles and build output: counted, but their lines would drown the real change.
        item["generated"] = True
        item["hunks"] = []
        return
    item["functions"] = _functions(item["hunks"])


def _functions(hunks: list[dict]) -> list[dict]:
    """The functions and classes a file's change touches, in order, each marked as added,
    deleted or modified. A changed line belongs to the nearest declaration above it."""
    found: dict[str, str] = {}

    def mark(name: str, change: str) -> None:
        found[name] = change if found.get(name, change) == change else "modified"

    for hunk in hunks:
        added = {name for line in hunk["lines"] if line["kind"] == "add" for name in _declared(line["text"])}
        deleted = {name for line in hunk["lines"] if line["kind"] == "delete" for name in _declared(line["text"])}
        context = hunk["context"]
        current = (_declared(context) or [context])[0] if context else None
        for line in hunk["lines"]:
            names = _declared(line["text"])
            if not names and top_level(line["text"]):
                current = None
            if names:
                current = names[0]
                if line["kind"] == "add":
                    mark(current, "modified" if current in deleted else "added")
                elif line["kind"] == "delete":
                    mark(current, "modified" if current in added else "deleted")
            # A blank line added or removed between two functions changes neither.
            elif line["kind"] != "context" and line["text"].strip() and current and current not in found:
                found[current] = "modified"
    return [{"name": name, "change": change} for name, change in found.items()]


# Lines at the left edge that end a block rather than start top-level code.
_CLOSERS = ("}", ")", "]", "end", "#", "//", "/*", "*", "@", "<", "else", "elif", "except", "finally", "catch")


def top_level(text: str) -> bool:
    """A line of code at the left edge that declares nothing, such as a module constant or
    an import: it belongs to no function, so the function above it stops there."""
    return bool(text.strip()) and not text[0].isspace() and not _declared(text) \
        and not text.lstrip().startswith(_CLOSERS)


def _declared(text: str) -> list[str]:
    match = DECLARATION.match(text)
    return [match[1] or match[2]] if match else []


def _git_diff(repo: Path, *revisions: str) -> str:
    attributes = paths.home() / "diff.gitattributes"
    if not attributes.exists() or attributes.read_text(encoding="utf-8") != ATTRIBUTES:
        attributes.write_text(ATTRIBUTES, encoding="utf-8", newline="\n")
    return git.run(
        repo,
        "-c", "core.quotePath=false",
        "-c", f"core.attributesFile={attributes.as_posix()}",
        "-c", f"diff.looma-js.xfuncname={JS_FUNCNAME}",
        "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", "-U3", *revisions, "--",
        ok=(0, 1),
    )


def _untracked(repo: Path) -> list[dict]:
    out = git.run(repo, "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z")
    names = [name for name in out.split("\x00") if name and not _ignored(name)][:MAX_UNTRACKED]
    files = []
    for name in names:
        # --no-index exits 1 when the files differ, which they always do here.
        patch = _git_diff_no_index(repo, name)
        for item in parse_patch(patch):
            item["path"] = name
            item["status"] = "added"
            files.append(item)
    return files


def _git_diff_no_index(repo: Path, name: str) -> str:
    attributes = paths.home() / "diff.gitattributes"
    return git.run(
        repo,
        "-c", f"core.attributesFile={attributes.as_posix()}",
        "-c", f"diff.looma-js.xfuncname={JS_FUNCNAME}",
        "diff", "--no-color", "--no-ext-diff", "--no-index", "-U3", "--", "/dev/null", name,
        ok=(0, 1),
    )


def _verify(repo: Path, ref: str) -> None:
    if not _exists(repo, f"{ref}^{{commit}}"):
        raise CompareError(f"unknown branch or commit: {ref}")


def _exists(repo: Path, ref: str) -> bool:
    try:
        git.run(repo, "rev-parse", "--verify", "--quiet", ref)
        return True
    except git.GitError:
        return False


def _unquote(path: str) -> str:
    # Git quotes paths holding quotes, backslashes or control characters.
    if len(path) >= 2 and path[0] == path[-1] == '"':
        return path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode(
            "latin-1").decode("utf-8", "replace")
    return path
