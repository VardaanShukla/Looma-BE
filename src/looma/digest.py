"""One day of recorded changes: what Claude changed, prompt by prompt, with the functions
each change touched and plain rules that flag the risky ones (sign-in, payments, the
database, removed tests, secrets). No AI is involved. Days are the laptop's own days."""

import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from looma import compare, prompts, undo
from looma.capture import inbox, scan
from looma.store import connect

# Flags that make a change worth a careful look, then ones that are only worth knowing.
RISKY = ("auth", "payments", "database", "tests", "secret")
BIG_TURN_LINES = 300

# Each rule: a name, a label, a pattern for file paths and one for changed lines. A word
# may run on in camelCase (passwordHash) but not in lower case (authored, display).
_PART = r"(^|[/_.-])({})([/_.-]|$)"
_END = r"(?-i:(?![a-z]))"
RULES = [
    ("auth", "Sign-in or permissions",
     re.compile(_PART.format(r"auth|authentication|authorization|login|logout|signin|signup|sessions?|"
                             r"passwords?|permissions?|oauth|jwt|acl|rbac"), re.I),
     re.compile(r"(password|passwd|authenticat\w*|authoriz\w*|login|logout|jwt|oauth|permission|admin|"
                r"csrf|cookie|(access|refresh|auth|bearer|csrf)[_ ]?token)" + _END, re.I)),
    ("payments", "Payments or money",
     re.compile(_PART.format(r"payments?|billing|invoices?|checkout|stripe|razorpay|paypal|subscriptions?|refunds?"),
                re.I),
     re.compile(r"(payment|billing|invoice|checkout|stripe|razorpay|paypal|refund|charge)s?" + _END,
                re.I)),
    ("database", "Database structure",
     re.compile(r"(^|/)(migrations?|alembic|prisma)/|schema|\.sql$|(^|/)models?\.py$", re.I),
     re.compile(r"\b(ALTER|DROP|CREATE|TRUNCATE)\s+(TABLE|INDEX|COLUMN|DATABASE)\b|\bDELETE\s+FROM\b", re.I)),
]
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9_-]{20,}|\bghp_[A-Za-z0-9]{30,}"
    r"|\b\w*(api[_-]?key|secret|passw(or)?d|token)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]", re.I)
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$")
TEST_CASE = re.compile(r"^\s*(?:async\s+)?def\s+test_|^\s*(?:it|test)\s*\(")
# Files of prose, and the parts of a line that are text rather than code.
PROSE = re.compile(r"\.(md|mdx|markdown|txt|rst|adoc)$", re.I)
_STRING = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`")
_COMMENT = re.compile(r"(^|\s)(#|//|--)\s.*$|/\*.*?\*/|^\s*\*.*$")


def _code_only(line: str) -> str:
    return _COMMENT.sub("", _STRING.sub('""', line))


CONFIG = re.compile(r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml|Dockerfile|docker-compose[^/]*"
                    r"|\.env[^/]*|vite\.config\.\w+|tsconfig[^/]*\.json)$|(^|/)\.github/workflows/")


# The AI tool behind a recorded prompt, by sessions.tool ('git': a Claude app chat's prompt file).
TOOL_NAMES = {"claude-code": "Claude", "git": "Claude", "claude-app": "Claude", "codex": "Codex", "antigravity": "Antigravity"}


class DigestError(ValueError):
    pass


class NotFound(DigestError):
    pass


_SELECT = ("SELECT c.id, c.repo_id, r.name AS repo_name, r.path AS repo_path, c.turn_id, c.commit_sha,"
           " c.file_path, c.diff, c.source, c.via, c.created_at, t.prompt, t.branch, t.started_at,"
           " m.message, m.branch AS commit_branch, m.found_by, s.tool,"
           " c.additions AS kept_additions, c.deletions AS kept_deletions, c.pruned_at"
           " FROM changes c JOIN repos r ON r.id = c.repo_id"
           " LEFT JOIN turns t ON t.id = c.turn_id LEFT JOIN commits m ON m.sha = c.commit_sha"
           " LEFT JOIN sessions s ON s.id = t.session_id")


def digest(day: date | None = None, repo_id: int | None = None) -> dict:
    """One day's changes; only one project's when `repo_id` is given."""
    day = day or datetime.now().astimezone().date()
    start = datetime.combine(day, time.min).astimezone()
    end = start + timedelta(days=1)
    # Commits Claude made where no hook saw it, such as a cloud session that pushed.
    scan.scan_all()
    # Requests from Claude app chats, which leave a note in the project instead of a hook call.
    inbox.ingest_all()
    conn = connect()
    where, params = " WHERE c.created_at >= ? AND c.created_at < ?", [_utc(start), _utc(end)]
    if repo_id is not None:
        where, params = where + " AND c.repo_id = ?", params + [repo_id]
    rows = conn.execute(_SELECT + where + " ORDER BY c.created_at, c.id", params).fetchall()
    conn.close()

    groups: dict[tuple, dict] = {}
    human = {"files": set(), "additions": 0, "deletions": 0, "commits": set()}
    for row in rows:
        item = _file(row)
        if row["source"] == "human":
            human["files"].add((row["repo_id"], row["file_path"]))
            human["additions"] += item["additions"]
            human["deletions"] += item["deletions"]
            if row["commit_sha"]:
                human["commits"].add(row["commit_sha"])
            continue
        # A prompt, or an AI-co-authored commit nobody's prompt was recorded for.
        # One prompt can edit several projects when Claude Code starts in their parent folder.
        key = ("turn", row["turn_id"], row["repo_id"]) if row["turn_id"] else ("commit", row["commit_sha"], row["repo_id"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = _group(row)
        _add(group, row, item)

    # Newest first; rows come oldest first, so the order groups were met breaks ties in time.
    changes = [_finish(group) for group in reversed(groups.values())]
    gone = undo.undone()
    for change in changes:
        change["undone_at"] = gone.get(change["id"])
    changes.sort(key=lambda g: g["at"], reverse=True)
    # Undone changes stay listed, so they can be put back, but no longer count: the totals
    # describe what's in the files now.
    live = [g for g in changes if not g["undone_at"]]
    functions = {(g["repo"]["id"], f["path"], fn["name"]) for g in live for f in g["files"] for fn in f["functions"]}
    return {
        "date": day.isoformat(),
        "claude": {
            # One prompt that edits several projects is listed once per project but is one prompt.
            "prompts": len({_prompt_of(g) for g in live}),
            "files": len({(g["repo"]["id"], f["path"]) for g in live for f in g["files"]}),
            "functions": len(functions),
            "additions": sum(g["additions"] for g in live),
            "deletions": sum(g["deletions"] for g in live),
            "risky": sum(1 for g in live if g["risky"]),
            "uncommitted": sum(1 for g in live if g["commit"] is None),
            "undone": len(changes) - len(live),
            # Files changed while a prompt ran but not through its Edit or Write (a shell
            # command, or possibly the person at the same time): counted, but less certain.
            "watched_files": len({(g["repo"]["id"], f["path"]) for g in live for f in g["files"] if f["watched"]}),
        },
        "you": {
            "files": len(human["files"]),
            "additions": human["additions"],
            "deletions": human["deletions"],
            "commits": len(human["commits"]),
        },
        "changes": changes,
    }


def change(change_key: str) -> dict:
    """One prompt's change (or one AI commit's) with its lines: each file's hunks and the
    parts it touched (functions, or the whole file when it has none), each with only its
    own lines, so the page can show exactly what the prompt changed and explain each part."""
    match = undo.KEY.match(change_key)
    if not match:
        raise NotFound("No such change.")
    kind, ref, repo_id = match[1], match[2], int(match[3])
    where = " WHERE c.source = 'ai' AND c.repo_id = ? AND " + (
        "c.turn_id = ?" if kind == "turn" else "c.turn_id IS NULL AND c.commit_sha = ?")
    conn = connect()
    rows = conn.execute(_SELECT + where + " ORDER BY c.created_at, c.id",
                        (repo_id, int(ref) if kind == "turn" else ref)).fetchall()
    conn.close()
    if not rows:
        raise NotFound("No such change.")
    group = _group(rows[0])
    hunks: dict[str, list[dict]] = {}
    for row in rows:
        item = _file(row, keep_hunks=True)
        hunks.setdefault(item["path"], []).extend(item.pop("hunks"))
        _add(group, row, item)
    result = _finish(group)
    result["undone_at"] = undo.undone().get(result["id"])
    for file in result["files"]:
        file["hunks"] = hunks.get(file["path"], [])
        file["units"] = units(file)
    return result


def units(file: dict) -> list[dict]:
    """The parts of a file one change touched, each with the lines that belong to it: a
    changed line belongs to the nearest declaration above it, as in compare._functions.
    A file with no functions (styles, settings, docs) is one part, the whole file."""
    parts: dict[str, list[str]] = {}
    for hunk in file["hunks"]:
        context = hunk["context"]
        current = (compare._declared(context) or [context])[0] if context else None
        started: set[str | None] = set()
        for line in hunk["lines"]:
            names = compare._declared(line["text"])
            if not names and compare.top_level(line["text"]):
                current = None
            if names:
                current = names[0]
            name = current if any(f["name"] == current for f in file["functions"]) else None
            if name not in started:
                started.add(name)
                parts.setdefault(name, []).append(
                    f"@@ -{hunk['old_start']},{hunk['old_lines']} +{hunk['new_start']},{hunk['new_lines']} @@")
            sign = {"add": "+", "delete": "-", "context": " "}[line["kind"]]
            parts[name].append(sign + line["text"])
    found = [{"name": fn["name"], "kind": "function", "change": fn["change"], "diff": "\n".join(parts[fn["name"]])}
             for fn in file["functions"] if fn["name"] in parts]
    if not found:
        change = {"added": "added", "deleted": "deleted"}.get(file["status"], "modified")
        lines = [line for hunk in file["hunks"] for line in hunk["lines"]]
        diff = "\n".join(sum(parts.values(), [])) if lines else ""
        found = [{"name": file["path"].rsplit("/", 1)[-1], "kind": "file", "change": change, "diff": diff}]
    return found


def unit_subject(path: str, unit: dict) -> dict:
    """What a part's explanation depends on, so the same lines are explained once."""
    return {"path": path, "name": unit["name"], "kind": unit["kind"], "change": unit["change"],
            "diff": unit["diff"], "scope": "prompt"}


def _prompt_of(group: dict) -> str:
    """The prompt (or commit) behind a group, without the project: turn-<id> or commit-<sha>."""
    return group["id"].rsplit("-", 1)[0]


def parse_day(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise DigestError("Give the day as YYYY-MM-DD.")


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _group(row) -> dict:
    prompt = prompts.readable(row["prompt"]) if row["prompt"] else None
    return {
        "id": undo.key("turn", row["turn_id"], row["repo_id"]) if row["turn_id"]
        else undo.key("commit", row["commit_sha"], row["repo_id"]),
        "repo": {"id": row["repo_id"], "name": row["repo_name"]},
        "branch": row["branch"] or row["commit_branch"],
        # Without a recorded prompt, the commit message says what was asked.
        "prompt": prompt or (row["message"] or "").split("\n")[0] or None,
        "prompt_recorded": prompt is not None,
        # prompt: recorded by the Claude Code hooks (or a prompt file); commit: an AI-co-authored
        # commit made here; cloud: a Claude commit found on a branch, made where no hook ran.
        "origin": "prompt" if row["turn_id"] else "cloud" if row["found_by"] == "scan" else "commit",
        "tool": TOOL_NAMES.get(row["tool"]) if row["turn_id"] else None,
        "at": row["started_at"] or row["created_at"],
        "commit": None,
        "_commits": {},
        "_files": {},
        "_repo_path": row["repo_path"],
    }


def _add(group: dict, row, item: dict) -> None:
    if row["commit_sha"]:
        group["_commits"][row["commit_sha"]] = (row["message"] or "").split("\n")[0]
    else:
        group["_uncommitted"] = True
    # A file changed twice in one prompt is rare; keep the totals of both.
    seen = group["_files"].get(item["path"])
    if seen:
        seen["additions"] += item["additions"]
        seen["deletions"] += item["deletions"]
        names = {f["name"] for f in seen["functions"]}
        seen["functions"] += [f for f in item["functions"] if f["name"] not in names]
        seen["flags"] += [f for f in item["flags"] if f not in seen["flags"]]
        seen["watched"] = seen["watched"] and item["watched"]
    else:
        group["_files"][item["path"]] = item


def _finish(group: dict) -> dict:
    files = sorted(group.pop("_files").values(), key=lambda f: (not _risky(f["flags"]), f["path"]))
    commits = group.pop("_commits")
    uncommitted = group.pop("_uncommitted", False)
    group.pop("_repo_path")
    if commits and not uncommitted:
        sha, message = next(iter(commits.items()))
        group["commit"] = {"sha": sha, "message": message, "count": len(commits)}
    group["files"] = files
    group["additions"] = sum(f["additions"] for f in files)
    group["deletions"] = sum(f["deletions"] for f in files)
    flags = []
    for f in files:
        for flag in f["flags"]:
            if flag["kind"] not in {x["kind"] for x in flags}:
                flags.append(flag)
    if group["additions"] + group["deletions"] >= BIG_TURN_LINES:
        flags.append({"kind": "big", "label": "Big change",
                      "reason": f"{group['additions'] + group['deletions']} lines changed in one go"})
    group["flags"] = flags
    group["risky"] = _risky(flags)
    return group


def _risky(flags: list[dict]) -> bool:
    return any(flag["kind"] in RISKY for flag in flags)


def _file(row, keep_hunks: bool = False) -> dict:
    """One recorded diff as a file entry with its line counts, functions and flags."""
    text = row["diff"]
    path = row["file_path"]
    if row["pruned_at"]:
        # Older than Settings keeps lines for: the counts were kept, the lines weren't.
        return {**({"hunks": []} if keep_hunks else {}), "path": path, "status": "modified",
                "additions": row["kept_additions"] or 0, "deletions": row["kept_deletions"] or 0,
                "functions": [], "flags": [], "watched": row["via"] == "watch"}
    if not text.startswith("diff --git "):
        # Diffs taken by the Claude Code hooks are plain unified diffs.
        text = f"diff --git a/{path} b/{path}\n{text}"
    parsed = compare.parse_patch(text)
    item = parsed[0] if parsed else {"path": path, "status": "modified", "hunks": [], "additions": 0,
                                     "deletions": 0, "functions": [], "generated": False, "_lines": 0}
    item["path"] = path
    if "--- /dev/null" in text.split("@@", 1)[0]:
        item["status"] = "added"
    elif "+++ /dev/null" in text.split("@@", 1)[0]:
        item["status"] = "deleted"
    _name_hunks(item["hunks"], Path(row["repo_path"]) / path)
    item.setdefault("_lines", 0)
    compare._finish(item)
    flags = _flags(item)
    return {
        **({"hunks": item["hunks"]} if keep_hunks else {}),
        "path": path,
        "status": item["status"],
        "additions": item["additions"],
        "deletions": item["deletions"],
        "functions": item["functions"],
        "flags": flags,
        # Changed while the prompt ran but not by Claude's Edit or Write: a shell command,
        # or possibly an edit by hand at the same time.
        "watched": row["via"] == "watch",
    }


def _name_hunks(hunks: list[dict], current: Path) -> None:
    """Hunks without a function in their header get the nearest declaration above them in
    the file as it is now: the same guess git makes, from a slightly later copy."""
    if not hunks or all(h["context"] and compare._declared(h["context"]) for h in hunks):
        return
    try:
        lines = current.read_text(encoding="utf-8", errors="replace").split("\n") if current.is_file() else []
    except OSError:
        lines = []
    for hunk in hunks:
        if hunk["context"] and compare._declared(hunk["context"]):
            continue
        hunk["context"] = None
        for line in reversed(lines[: max(0, hunk["new_start"] - 1)]):
            if compare._declared(line):
                hunk["context"] = line.strip()
                break
            if compare.top_level(line):
                break  # module-level code above: the hunk starts outside any function


def _mask(match: re.Match) -> str:
    text = match[0]
    quote = re.search(r"['\"]", text)
    # Keep the name and the first two characters, so you can find it, but not the value.
    return text[: quote.end() + 2] + "•" * 8 + text[-1] if quote else text[:4] + "•" * 8


def _shown(line: str) -> str:
    # Any line the page shows has keys and passwords masked, whatever flagged it.
    line = SECRET.sub(_mask, line.strip())
    return line if len(line) <= 160 else line[:157] + "…"


def _flags(item: dict) -> list[dict]:
    path = item["path"]
    added = [line["text"] for hunk in item["hunks"] for line in hunk["lines"] if line["kind"] == "add"]
    deleted = [line["text"] for hunk in item["hunks"] for line in hunk["lines"] if line["kind"] == "delete"]
    flags = []
    # A test about payments isn't a payments change; removed tests are flagged below.
    for kind, label, path_rule, line_rule in [] if TEST_PATH.search(path) else RULES:
        # New lines say more about the change than the ones it replaced. Sign-in and payment
        # words count in code only: text that mentions payments (help, docs, comments) isn't
        # payment code. SQL lives inside strings, so database lines are read whole.
        code = (lambda t: "" if PROSE.search(path) else _code_only(t)) if kind in ("auth", "payments") else (lambda t: t)
        hit = next(((m, text) for text in added + deleted if (m := line_rule.search(code(text)))), None)
        if path_rule.search(path):
            flags.append({"kind": kind, "label": label, "reason": f"the file is {path}",
                          "line": _shown(hit[1]) if hit else None})
        elif hit:
            flags.append({"kind": kind, "label": label, "reason": f"a changed line mentions “{hit[0][0]}”",
                          "line": _shown(hit[1])})
    gone = [t for t in deleted if TEST_CASE.match(t)]
    removed_tests = len(gone) - sum(bool(TEST_CASE.match(t)) for t in added)
    if TEST_PATH.search(path) and item["status"] == "deleted":
        flags.append({"kind": "tests", "label": "Tests removed", "reason": "the whole test file was deleted"})
    elif removed_tests > 0:
        flags.append({"kind": "tests", "label": "Tests removed",
                      "reason": f"{removed_tests} test{'s' if removed_tests > 1 else ''} deleted", "line": _shown(gone[0])})
    secret = next((t for t in added if SECRET.search(t)), None)
    if secret is not None:
        # The page shows where it is, never the value itself.
        flags.append({"kind": "secret", "label": "Possible secret", "reason": "an added line looks like a key or password",
                      "line": _shown(secret)})
    if CONFIG.search(path):
        flags.append({"kind": "config", "label": "Setup or dependencies", "reason": f"the file is {path}"})
    if item["status"] == "deleted" and not any(f["kind"] == "tests" for f in flags):
        flags.append({"kind": "deleted", "label": "File deleted", "reason": f"{path} was removed"})
    return flags
