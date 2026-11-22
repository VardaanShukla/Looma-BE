"""Which project file uses which, file by file, for the detailed knowledge transfer: what
each file depends on, which files use it, and which tests check it. Read from the import
statements (see projectmap._imports), so it needs no AI and only names real files. Also
the text a file says about itself at its top, for files the AI hasn't written a note on."""

import re
from pathlib import Path, PurePosixPath

from looma.projectmap import WORKTREE, _files, _imports, _read_blobs, readable
from looma.summary import ENTRY_NAMES, _TEST_FILE, kt_files

MAX_OWN_NOTE = 600

_PY_DOC = re.compile(r'^[rRuUbB]{0,2}("""|\'\'\')(.*?)\1', re.S)
_BLOCK_COMMENT = re.compile(r"^/\*+(.*?)\*/", re.S)


def is_test(path: str) -> bool:
    return bool(_TEST_FILE.search(path))


def file_graph(repo: Path, revision: str, with_text: bool = False) -> dict:
    """Every file the KT explains, with its lines, what it depends on, what uses it (tests
    apart), where it sits in the flow and the comment at its top; with_text adds its text."""
    revision = readable(repo, revision)
    files = _files(repo, revision)
    links, complete = _imports(repo, files)
    depends: dict[str, set[str]] = {}
    users: dict[str, set[str]] = {}
    for source, target in links:
        depends.setdefault(source, set()).add(target)
        users.setdefault(target, set()).add(source)

    entries = kt_files(repo, revision)
    # From the working tree each "sha" is the file's path (see projectmap._files).
    blob = {path: sha for path, sha, _ in files}
    texts = _read_blobs(repo, [blob[f["path"]] for f in entries], worktree=revision == WORKTREE) if entries else []
    result = []
    for entry, text in zip(entries, texts):
        path = entry["path"]
        binary = "\x00" in text[:8000]
        used = users.get(path, set())
        result.append({
            **entry,
            "lines": 0 if binary else len(text.splitlines()),
            "depends_on": sorted(depends.get(path, set())),
            "used_by": sorted(u for u in used if not is_test(u)),
            "tests": sorted(u for u in used if is_test(u)),
            "flow": _flow(path, depends.get(path, set()), used),
            "own_note": None if binary else own_note(path, text),
            **({"text": "" if binary else text} if with_text else {}),
        })
    return {"revision": revision, "files": result, "complete": complete, "links": links}


def _flow(path: str, depends: set[str], used: set[str]) -> str:
    """Where the file sits, in a few words, from its imports alone."""
    if is_test(path):
        return "Test"
    production = [u for u in used if not is_test(u)]
    if production:
        return f"Used by {len(production)} file{'s' * (len(production) != 1)}"
    if PurePosixPath(path).name.lower() in ENTRY_NAMES and depends:
        return "Starts the app"
    if depends:
        return "Top of its chain: no file imports it"
    return "Stands alone"


def own_note(path: str, text: str) -> str | None:
    """What the file says about itself before its code starts: a Python docstring, a
    leading block or line comment, or a readme's first paragraph. Never made up."""
    suffix = PurePosixPath(path).suffix.lower()
    lines = text.lstrip("﻿").splitlines()
    # A shebang, encoding line or "use client" comes before what the file says.
    while lines and (lines[0].startswith("#!") or "coding" in lines[0][:30] and lines[0].startswith("#")
                     or lines[0].strip() in ("'use client';", '"use client";', "'use strict';", '"use strict";')):
        lines = lines[1:]
    body = "\n".join(lines).lstrip()
    found = None
    if suffix in (".md", ".mdx", ".markdown", ".rst", ".txt"):
        paragraph = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "<", "!", "[!", "=", "-" * 3)):
                if paragraph:
                    break
                continue
            paragraph.append(stripped)
        found = " ".join(paragraph)
    elif suffix == ".py" and (match := _PY_DOC.match(body)):
        found = match.group(2)
    elif match := _BLOCK_COMMENT.match(body):
        found = "\n".join(line.strip().lstrip("*").strip() for line in match.group(1).splitlines())
    else:
        marker = "//" if body.startswith("//") else "#" if body.startswith("#") and suffix != ".md" else \
            "--" if body.startswith("--") else None
        if marker:
            comment = []
            for line in body.splitlines():
                if not line.strip().startswith(marker):
                    break
                comment.append(line.strip()[len(marker):].lstrip("/#- ").strip())
            found = "\n".join(comment)
    if not found:
        return None
    # The first paragraph, on one line: later ones are usually licence text or details.
    found = re.split(r"\n\s*\n", found.strip())[0]
    found = " ".join(found.split())
    if len(found) > MAX_OWN_NOTE:
        found = found[:MAX_OWN_NOTE].rsplit(" ", 1)[0] + " ..."
    return found or None
