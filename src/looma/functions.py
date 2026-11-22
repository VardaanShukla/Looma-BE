"""Each function a change touched, as it was and as it is now, with the facts git can show
about the difference: its inputs, what it calls, how many decisions it makes, what is
written about it. Python is read with its own parser; JavaScript, TypeScript and other
brace languages with patterns and brace matching, which is close but not exact. Style
sheets split into their rules and docs into their sections; any other file (settings,
HTML, SQL, scripts, data) is one unit, the whole file, with the lines that changed."""

import ast
import difflib
import re
from pathlib import Path, PurePosixPath

from looma import git

MAX_UNITS = 60
MAX_CODE_LINES = 300
MAX_USES = 8
# The same file's functions that a changed one calls, shown to the AI so it reads what they
# do instead of guessing from their names: at most this many, each cut to this many lines.
MAX_HELPERS = 3
MAX_HELPER_LINES = 60
MAX_HELPER_CHARS = 4_000
# The changed lines of a file read as a whole, for the AI and for its cache key.
MAX_DIFF_LINES = 200

BRACE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".cs", ".php",
                  ".c", ".h", ".cpp", ".swift"}
STYLE_SUFFIXES = {".css", ".scss", ".sass", ".less"}
DOC_SUFFIXES = {".md", ".mdx", ".markdown", ".rst"}
HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")

# A declaration at the start of a line, in the brace languages. Each pattern's group 1 is
# the name; the kind follows from which pattern matched.
JS_DECLARATIONS = [
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[<(]")),
    ("function", re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?"
        r"(?:function\b|\([^)]*\)?\s*(?::[^=]+)?=>|\(|[A-Za-z_$][\w$]*\s*=>)")),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("type", re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[(\[]")),
    ("function", re.compile(r"^\s*(?:pub(?:\([\w:]+\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")),
    ("type", re.compile(r"^\s*(?:pub(?:\([\w:]+\))?\s+)?(?:struct|trait|enum)\s+([A-Za-z_]\w*)")),
]
# A method inside a class body: name(args) { on one line.
METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|static|readonly|override|async|get|set|abstract)\s+)*"
    r"\*?([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;]*$")
NOT_NAMES = {"if", "for", "while", "switch", "catch", "return", "function", "else", "do", "try", "new", "with",
             "await", "typeof", "super", "constructor_"}
CALL = re.compile(r"(?<![\w$.])(?:new\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
DECISION = re.compile(r"\b(?:if|for|while|case|catch|elif|except)\b|\?\?|&&|\|\||\s\?\s")
# Calls too common to say anything about a change: built-ins and everyday methods on
# lists, text and maps. Left out of "uses", which is about the project's own code.
NOISE_CALLS = {
    "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple", "isinstance", "range", "sorted", "min",
    "max", "sum", "any", "all", "enumerate", "zip", "getattr", "hasattr", "setattr", "type", "super", "repr", "iter",
    "next", "map", "filter", "open", "Number", "String", "Boolean", "Array", "Object", "parseInt", "parseFloat",
}
NOISE_METHODS = {
    "some", "every", "map", "filter", "forEach", "reduce", "find", "findIndex", "includes", "indexOf", "push", "pop",
    "shift", "unshift", "slice", "splice", "concat", "join", "split", "sort", "reverse", "flat", "flatMap", "keys",
    "values", "entries", "trim", "replace", "replaceAll", "startsWith", "endsWith", "toLowerCase", "toUpperCase",
    "toString", "toFixed", "padStart", "padEnd", "get", "set", "has", "add", "delete", "append", "extend", "insert",
    "remove", "items", "update", "copy", "strip", "lstrip", "rstrip", "lower", "upper", "format", "splitlines",
    "startswith", "endswith", "encode", "decode", "then", "catch", "finally", "log", "warn", "error", "info", "debug",
    "stringify", "parse", "max", "min", "round", "floor", "ceil", "abs", "isArray", "from", "assign", "now",
}


def _meaningful(call: str) -> bool:
    if "." in call:
        return call.rsplit(".", 1)[1] not in NOISE_METHODS
    return call not in NOISE_CALLS


CALL_KEYWORDS = NOT_NAMES | {"function", "def", "class", "print", "elif", "and", "or", "not", "in", "lambda",
                             "assert", "yield", "import", "from", "async", "sizeof"}


def function_changes(repo: Path, before_rev: str, after_rev: str | None, path: str,
                     old_path: str | None = None) -> list[dict]:
    """The functions that differ between `before_rev:old_path` and `after_rev:path`, where
    a missing `after_rev` means the file as it is on disk now."""
    old_text = _read(repo, before_rev, old_path or path)
    new_text = _read(repo, after_rev, path)
    old_units = {unit["name"]: unit for unit in units(path, old_text)} if old_text is not None else {}
    new_units = {unit["name"]: unit for unit in units(path, new_text)} if new_text is not None else {}
    if not old_units and not new_units:
        # Settings, HTML, SQL, scripts, data, or code that doesn't parse: the file is the unit.
        whole = _whole_file(path, old_text, new_text)
        return [whole] if whole else []

    changes = []
    for name in list(new_units) + [n for n in old_units if n not in new_units]:
        before, after = old_units.get(name), new_units.get(name)
        if before and after and _normal(before["code"]) == _normal(after["code"]):
            continue
        change = "modified" if before and after else "added" if after else "deleted"
        changes.append({
            "name": name,
            "kind": (after or before)["kind"],
            "change": change,
            "before": before,
            "after": after,
            "facts": _facts(before, after),
            "used_by": [],
            "helpers": helpers(after, before, new_units),
        })
        if len(changes) >= MAX_UNITS:
            break
    for item in changes:
        if item["change"] != "deleted" and item["kind"] not in ("style", "section"):
            item["used_by"] = _uses(repo, after_rev, item["name"].split(".")[-1], path, item["after"])
    if not changes:
        # Only lines outside every function changed (imports, settings, comments): those
        # still get explained, as the file's change.
        whole = _whole_file(path, old_text, new_text)
        return [whole] if whole else []
    return changes


def helpers(after: dict | None, before: dict | None, file_units: dict[str, dict]) -> list[dict]:
    """The code of the functions in the same file that `after` calls, the ones it newly
    calls first: what a change's pros and cons often hang on."""
    if not after or not after.get("calls"):
        return []
    by_name = {}
    for name, unit in file_units.items():
        by_name.setdefault(name.rsplit(".", 1)[-1], unit)
    own = after["name"]
    old_calls = set((before or {}).get("calls") or [])
    calls = sorted(after["calls"], key=lambda c: c in old_calls)  # new calls first
    found, seen, room = [], set(), MAX_HELPER_CHARS
    for call in calls:
        unit = by_name.get(call.rsplit(".", 1)[-1])
        if not unit or unit["name"] == own or unit["name"] in seen or unit["kind"] in ("style", "section", "file"):
            continue
        lines = unit["code"].splitlines()
        code = "\n".join(lines[:MAX_HELPER_LINES]) + (f"\n… ({len(lines) - MAX_HELPER_LINES} more lines)"
                                                        if len(lines) > MAX_HELPER_LINES else "")
        if len(code) > room:
            continue
        seen.add(unit["name"])
        found.append({"name": unit["name"], "code": code, "new_call": call not in old_calls})
        room -= len(code)
        if len(found) >= MAX_HELPERS:
            break
    return found


def _whole_file(path: str, old_text: str | None, new_text: str | None) -> dict | None:
    """The whole file as one unit, with the changed lines as a diff, so a change to any
    kind of file can be read and explained."""
    if old_text is not None and new_text is not None and _normal(old_text) == _normal(new_text):
        return None
    if old_text is None and new_text is None:
        return None
    before = _file_unit(path, old_text) if old_text is not None else None
    after = _file_unit(path, new_text) if new_text is not None else None
    diff = None
    if before and after and not _binary(old_text) and not _binary(new_text):
        lines = list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=2))[2:]
        if len(lines) > MAX_DIFF_LINES:
            lines = lines[:MAX_DIFF_LINES] + [f"… {len(lines) - MAX_DIFF_LINES} more lines"]
        diff = "\n".join(lines)
    return {
        "name": PurePosixPath(path).name,
        "kind": "file",
        "change": "modified" if before and after else "added" if after else "deleted",
        "before": before,
        "after": after,
        "facts": _facts(before, after),
        "used_by": [],
        "diff": diff,
    }


def _binary(text: str) -> bool:
    return "\x00" in text[:8000]


def _file_unit(path: str, text: str) -> dict:
    lines = text.splitlines()
    code = "(a binary file, such as an image, so there is no text to show)" if _binary(text) else _clip(text)
    return {
        "name": PurePosixPath(path).name,
        "kind": "file",
        "start": 1,
        "end": max(1, len(lines)),
        "lines": len(lines),
        "code": code,
        "signature": lines[0].strip()[:200] if lines and not _binary(text) else "",
        "doc": None,
        "params": [],
        "returns": None,
        "is_async": False,
        "calls": [],
        "decisions": 0,
    }


def units(path: str, text: str) -> list[dict]:
    """The top-level functions, methods and types in a file, each with its lines and facts."""
    suffix = PurePosixPath(path).suffix
    if suffix == ".py":
        try:
            return _python_units(text)
        except (SyntaxError, ValueError):
            return []
    if suffix in BRACE_SUFFIXES:
        return _brace_units(path, text)
    if suffix in STYLE_SUFFIXES:
        return _style_units(text)
    if suffix in DOC_SUFFIXES:
        return _doc_units(text)
    return []


def _read(repo: Path, rev: str | None, path: str) -> str | None:
    if rev is None:
        file = (repo / path).resolve()
        # Only files inside the project.
        if not file.is_relative_to(repo.resolve()) or not file.is_file():
            return None
        return file.read_text(encoding="utf-8", errors="replace")
    try:
        return git.run(repo, "show", f"{rev}:{path}")
    except git.GitError:
        return None


# --- Python -----------------------------------------------------------------------------

def _python_units(text: str) -> list[dict]:
    tree = ast.parse(text)
    lines = text.splitlines()
    found = []

    def visit(node, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                methods = [c for c in child.body if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))]
                if not methods:
                    found.append(_python_unit(child, prefix, "class", lines))
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(_python_unit(child, prefix, "method" if prefix else "function", lines))

    visit(tree, "")
    return found


def _python_unit(node, prefix: str, kind: str, lines: list[str]) -> dict:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    end = node.end_lineno or node.lineno
    code = "\n".join(lines[start - 1:end])
    unit = {
        "name": prefix + node.name,
        "kind": kind,
        "start": start,
        "end": end,
        "lines": end - start + 1,
        "code": _clip(code),
        "signature": lines[node.lineno - 1].strip(),
        "doc": (ast.get_docstring(node) or _comments_above(lines, start, "#") or None),
        "params": [],
        "returns": None,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "calls": [],
        "decisions": 0,
    }
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        unit["params"] = [a.arg for a in [*args.posonlyargs, *args.args] if a.arg not in ("self", "cls")]
        if args.vararg:
            unit["params"].append("*" + args.vararg.arg)
        unit["params"] += [a.arg for a in args.kwonlyargs]
        if args.kwarg:
            unit["params"].append("**" + args.kwarg.arg)
        unit["returns"] = ast.unparse(node.returns) if node.returns else None
        calls, decisions = set(), 0
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                name = _call_name(inner.func)
                if name:
                    calls.add(name)
            elif isinstance(inner, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp,
                                    ast.comprehension, ast.match_case)):
                decisions += 1
            elif isinstance(inner, ast.BoolOp):
                decisions += len(inner.values) - 1
        unit["calls"] = sorted(c for c in calls if _meaningful(c))
        unit["decisions"] = decisions
    return unit


def _call_name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_name(func.value)
        return f"{base}.{func.attr}" if base and base not in ("self", "cls") else func.attr
    return None


# --- Brace languages --------------------------------------------------------------------

def _brace_units(path: str, text: str) -> list[dict]:
    lines = text.splitlines()
    component_file = PurePosixPath(path).suffix in (".tsx", ".jsx")
    found: list[dict] = []
    classes: list[tuple[str, int, int]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kind, name = _declaration(line)
        in_class = next((c for c in classes if c[1] < i + 1 <= c[2]), None)
        if not kind and in_class:
            match = METHOD.match(line)
            if match and match[1] not in NOT_NAMES and not line.strip().startswith(("return", "await", "new ")):
                kind, name = "method", match[1]
        if not kind:
            i += 1
            continue
        end = _block_end(lines, i)
        if kind == "class":
            classes.append((name, i + 1, end))
            # Methods are units of their own; a class with none is one unit.
            body = "\n".join(lines[i + 1:end - 1])
            if not any(METHOD.match(l) for l in body.splitlines()):
                found.append(_brace_unit(lines, i, end, name, "class"))
            i += 1
            continue
        if kind == "method":
            name = f"{in_class[0]}.{name}"
        elif kind == "function" and component_file and name[:1].isupper():
            kind = "component"
        found.append(_brace_unit(lines, i, end, name, kind))
        # Functions inside this one belong to it.
        i = end if kind != "type" else i + 1
    return found


def _declaration(line: str) -> tuple[str | None, str | None]:
    for kind, pattern in JS_DECLARATIONS:
        match = pattern.match(line)
        if match and match[1] not in NOT_NAMES:
            return kind, match[1]
    return None, None


def _block_end(lines: list[str], start: int) -> int:
    """The 1-based last line of the declaration starting at `start` (0-based): where its
    first brace block closes, or the first statement end when it has no block."""
    depth = 0
    opened = False
    in_block_comment = False
    for i in range(start, len(lines)):
        line = lines[i]
        j = 0
        quote = None
        while j < len(line):
            ch = line[j]
            nxt = line[j + 1] if j + 1 < len(line) else ""
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    j += 1
            elif quote:
                if ch == "\\":
                    j += 1
                elif ch == quote:
                    quote = None
            elif ch == "/" and nxt == "/":
                break
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                j += 1
            elif ch in "'\"`":
                quote = ch
            elif ch in "{([":
                depth += 1
                if ch == "{":
                    opened = True
            elif ch in "})]":
                depth -= 1
                if depth <= 0 and opened and ch == "}":
                    return i + 1
            elif ch == ";" and depth == 0:
                return i + 1
            j += 1
        # A one-line arrow or type with no braces and no semicolon.
        if depth == 0 and not opened and i > start and not line.rstrip().endswith((",", "=>", "(", "=", "|", "&")):
            return i + 1
        if depth == 0 and not opened and i == start and line.rstrip().endswith((")", '"', "'")) and "=>" in line:
            return i + 1
    return len(lines)


def _brace_unit(lines: list[str], start: int, end: int, name: str, kind: str) -> dict:
    code = "\n".join(lines[start:end])
    signature = lines[start].strip()
    body = "\n".join(_strip_comments(l) for l in lines[start + 1:end])
    calls = {m[1] for m in CALL.finditer(body)} - CALL_KEYWORDS - {name.split(".")[-1]}
    return {
        "name": name,
        "kind": kind,
        "start": start + 1,
        "end": end,
        "lines": end - start,
        "code": _clip(code),
        "signature": signature,
        "doc": _comments_above(lines, start + 1, "//") or None,
        "params": _params(code) if kind in ("function", "method", "component") else [],
        "returns": None,
        "is_async": bool(re.search(r"\basync\b", signature)),
        "calls": sorted(c for c in calls if c.split(".")[0] not in CALL_KEYWORDS and _meaningful(c)),
        "decisions": len(DECISION.findall(body)) if kind != "type" else 0,
    }


def _params(code: str) -> list[str]:
    opening = code.find("(")
    if opening == -1:
        return []
    depth, j = 0, opening
    for j in range(opening, len(code)):
        if code[j] in "([{<":
            depth += 1
        elif code[j] in ")]}>":
            depth -= 1
            if depth == 0:
                break
    inner = code[opening + 1:j]
    # Split on top-level commas only.
    parts, depth, current = [], 0, ""
    for ch in inner:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    names = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith("{"):
            names.extend(n.strip().split(":")[0].split("=")[0].strip() for n in part.strip("{} ").split("}")[0].split(",") if n.strip())
        else:
            part = re.sub(r"^(?:readonly|private|public|protected)\s+", "", part)
            names.append(re.split(r"[:=?]", part.lstrip("."))[0].strip() or part)
    return [n for n in names if n]


def _strip_comments(line: str) -> str:
    return re.sub(r"//.*$", "", line)


# --- Style sheets and docs --------------------------------------------------------------

def _style_units(text: str) -> list[dict]:
    """Each top-level rule (or @media block) of a style sheet, named by its selector."""
    lines = text.splitlines()
    found: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if "{" not in line or stripped.startswith(("/*", "*", "//")):
            i += 1
            continue
        # A selector can run over several lines: "a,\nb {".
        start = i
        while start > 0 and lines[start - 1].rstrip().endswith(","):
            start -= 1
        selector = " ".join(l.strip() for l in lines[start:i + 1]).split("{")[0].strip()
        end = _block_end(lines, i)
        if selector:
            unit = _brace_unit(lines, start, end, _unique(selector[:80], found), "style")
            unit.update(params=[], calls=[], decisions=0)
            found.append(unit)
        i = max(end, i + 1)
    return found


def _doc_units(text: str) -> list[dict]:
    """Each section of a doc, from one heading to the next."""
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    in_code = False
    for i, line in enumerate(lines):
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
        heading = None if in_code else HEADING.match(line)
        if heading:
            starts.append((i, heading[2]))
    if not starts or starts[0][0] > 0:
        starts.insert(0, (0, "Opening"))
    found: list[dict] = []
    for n, (start, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        if not "\n".join(lines[start:end]).strip():
            continue
        unit = _file_unit("", "\n".join(lines[start:end]))
        unit.update(name=_unique(title[:80], found), kind="section", start=start + 1, end=max(start + 1, end),
                    signature=lines[start].strip())
        found.append(unit)
    return found


def _unique(name: str, found: list[dict]) -> str:
    """The same selector or heading can appear twice; the second is told apart by number."""
    taken = {unit["name"] for unit in found}
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


# --- Shared -----------------------------------------------------------------------------

def _comments_above(lines: list[str], start: int, marker: str) -> str:
    """Comment lines right above a declaration (start is 1-based), as plain text."""
    collected = []
    i = start - 2
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith(marker) or stripped.startswith(("*", "/*", "*/")):
            text = stripped.lstrip("/#*").strip()
            if text and not text.startswith("@"):
                collected.append(text)
            elif stripped.startswith("/*") or stripped.startswith("/**"):
                pass
            i -= 1
            continue
        break
    return " ".join(reversed(collected)).strip()


def _facts(before: dict | None, after: dict | None) -> dict:
    if not before or not after:
        return None
    return {
        "params_added": [p for p in after["params"] if p not in before["params"]],
        "params_removed": [p for p in before["params"] if p not in after["params"]],
        "calls_added": [c for c in after["calls"] if c not in before["calls"]],
        "calls_removed": [c for c in before["calls"] if c not in after["calls"]],
        "lines": [before["lines"], after["lines"]],
        "decisions": [before["decisions"], after["decisions"]],
        "is_async": [before["is_async"], after["is_async"]],
        "returns": [before["returns"], after["returns"]],
        "doc_changed": (before["doc"] or "") != (after["doc"] or ""),
        "signature_changed": before["signature"] != after["signature"],
    }


def _uses(repo: Path, rev: str | None, name: str, own_path: str, unit: dict | None) -> list[dict]:
    """Where else the function's name is used, so a new function shows what calls it."""
    if len(name) < 3:
        return []
    # Three per file, counted here: git grep's --max-count needs git 2.38 or newer.
    args = ["grep", "-n", "-w", "-F", "-I", "-e", name]
    if rev:
        args.append(rev)
    try:
        out = git.run(repo, *args, ok=(0, 1))
    except git.GitError:
        return []
    found = []
    per_file: dict[str, int] = {}
    for line in out.splitlines():
        if rev:
            line = line[len(rev) + 1:]
        try:
            path, number, text = line.split(":", 2)
        except ValueError:
            continue
        number = int(number)
        if path == own_path and unit and unit["start"] <= number <= unit["end"]:
            continue
        if re.match(r"^\s*(import|from)\b", text):
            continue
        per_file[path] = per_file.get(path, 0) + 1
        if per_file[path] > 3:
            continue
        found.append({"path": path, "line": number, "text": text.strip()[:160]})
        if len(found) >= MAX_USES:
            break
    return found


def _normal(code: str) -> str:
    return "\n".join(line.rstrip() for line in code.strip().splitlines())


def _clip(code: str) -> str:
    lines = code.splitlines()
    if len(lines) <= MAX_CODE_LINES:
        return code
    return "\n".join(lines[:MAX_CODE_LINES] + [f"… {len(lines) - MAX_CODE_LINES} more lines"])
