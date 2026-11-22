"""The facts behind the detailed knowledge transfer, all read from the code so they're true
for any project: its parts (tests apart, as Checks), what role each file plays, a reading
path for someone new, where its data lives (tables, settings, browser storage), its web
routes and commands, and the checks that keep what the AI writes honest: a flow the AI
proposes keeps only steps that name real files linked by real imports."""

import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath

from looma import git
from looma.filegraph import file_graph, is_test
from looma.projectmap import WORKTREE, _area_for, _areas, _name, _role, readable
from looma.summary import ENTRY_NAMES

CHECKS = "checks"  # the part that holds the tests
MAX_READING = 10
MAX_FOUND = 40  # the most items of each kind a scanner lists
MAX_CAUTIONS = 8
MIN_FLOW_STEPS = 3

SCREEN_SUFFIXES = {".tsx", ".jsx", ".vue", ".svelte", ".html", ".css", ".scss"}
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".go", ".rs", ".java",
                 ".kt", ".cs", ".php", ".rb", ".swift", ".c", ".cpp", ".h", ".sh", ".ps1", ".bat"}
SETTINGS_SUFFIXES = {".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".lock", ".env"}
DATA_FOLDERS = {"store", "db", "database", "models", "migrations", "schema", "schemas", "entities"}
HELPER_FOLDERS = {"utils", "lib", "helpers", "common", "shared"}

# Where the data lives, read from the code.
_SQL_TABLE = re.compile(r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?(\w+)", re.I)
_ORM_TABLE = re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]")
_DJANGO_MODEL = re.compile(r"^class\s+(\w+)\(\s*(?:models\.)?Model\s*\)", re.M)
_ENV = re.compile(r"os\.environ\.get\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]|os\.environ\[\s*['\"]([A-Z][A-Z0-9_]+)['\"]"
                  r"|os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]|setenv\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]"
                  r"|process\.env\.([A-Z][A-Z0-9_]+)|process\.env\[\s*['\"]([A-Z][A-Z0-9_]+)['\"]"
                  r"|import\.meta\.env\.([A-Z][A-Z0-9_]+)")
_ENV_EXAMPLE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]+)\s*=", re.M)
_STORAGE = re.compile(r"(?:localStorage|sessionStorage)\.(?:getItem|setItem|removeItem)\(\s*['\"`]([^'\"`$]+)['\"`]")
# Web routes: FastAPI and Flask decorators, Express calls.
_PY_ROUTE = re.compile(r"@\w+\.(get|post|put|patch|delete|route|websocket)\(\s*['\"]([^'\"]+)['\"](?P<rest>[^\n]*)")
_JS_ROUTE = re.compile(r"\b(?:app|router)\.(get|post|put|patch|delete|all)\(\s*['\"`](/[^'\"`]*)['\"`]")


def has_changes(repo: Path) -> bool:
    """Whether the working folder differs from its last commit (a folder without git counts as
    its working folder already)."""
    if readable(repo, "HEAD") == WORKTREE:
        return False
    return bool(git.run(repo, "status", "--porcelain", "--untracked-files=normal").strip())


def source(repo: Path, revision: str) -> dict:
    """What the KT was read from: a branch and its commit, or the working folder."""
    if revision == WORKTREE:
        return {"kind": "worktree", "branch": None, "commit": None}
    branch = git.run(repo, "rev-parse", "--abbrev-ref", revision, ok=(0, 128)).strip() or revision
    commit = git.run(repo, "rev-parse", "--short", revision, ok=(0, 128)).strip() or None
    return {"kind": "branch", "branch": None if branch == "HEAD" else branch, "commit": commit}


def document(repo: Path, revision: str, worktree: bool = False) -> dict:
    """Everything the detailed KT shows that comes from the code."""
    revision = WORKTREE if worktree else readable(repo, revision)
    graph = file_graph(repo, revision, with_text=True)
    files = graph["files"]
    texts = {f["path"]: f.pop("text") for f in files}
    areas = _areas([f["path"] for f in files])
    for f in files:
        f["part"] = CHECKS if is_test(f["path"]) else _area_for(f["path"], areas)
        f["role"] = role(f)
    parts = _parts(files)
    known = {f["path"] for f in files}
    edges = [(a, b) for a, b in graph["links"] if a in known and b in known]
    by_path = {f["path"]: f for f in files}
    links: Counter = Counter()
    for a, b in edges:
        pa, pb = by_path[a]["part"], by_path[b]["part"]
        if pa != pb and CHECKS not in (pa, pb):
            links[(pa, pb)] += 1
    return {
        "source": source(repo, revision),
        "revision": revision,
        "complete": graph["complete"],
        "files": files,
        "parts": parts,
        "links": [{"source": a, "target": b, "imports": n} for (a, b), n in links.most_common()],
        "reading": reading_path(files),
        "data": scan_data(texts),
        "routes": scan_routes(texts),
        "commands": scan_commands(texts),
        "size": {"files": len(files), "lines": sum(f["lines"] for f in files),
                 "languages": [{"name": n, "files": c} for n, c in
                               Counter(f["language"] for f in files if f["language"] != "Other").most_common(5)]},
    }


def role(file: dict) -> str:
    """The file's role in a word or two, from its path, kind and imports."""
    path = PurePosixPath(file["path"])
    suffix, folders = path.suffix.lower(), {p.lower() for p in path.parts[:-1]}
    if is_test(file["path"]):
        return "Check"
    if file["flow"] == "Starts the app" or (path.name.lower() in ENTRY_NAMES and not file["used_by"]
                                             and suffix in CODE_SUFFIXES):
        return "Starts the app"
    if file["language"] == "Docs" or path.name.upper().startswith(("LICENSE", "NOTICE", "CHANGELOG", "README")):
        return "Docs"
    if suffix == ".sql" or folders & DATA_FOLDERS:
        return "Data"
    if suffix in SCREEN_SUFFIXES:
        return "Screen"
    if suffix in SETTINGS_SUFFIXES or path.name.startswith(".") or ".config." in path.name:
        return "Settings"
    if folders & HELPER_FOLDERS or len(file["used_by"]) >= 3:
        return "Helper"
    if suffix in CODE_SUFFIXES:
        return "Logic"
    return "Settings"


def _parts(files: list[dict]) -> list[dict]:
    """The parts in reading order: the code first, then the main folder, then the checks."""
    found: dict[str, dict] = {}
    for f in files:
        part = found.setdefault(f["part"], {"id": f["part"], "folder": f["part"], "files": 0, "lines": 0,
                                            "languages": Counter()})
        part["files"] += 1
        part["lines"] += f["lines"]
        if f["language"] != "Other":
            part["languages"][f["language"]] += 1
    result = []
    for part in found.values():
        if part["id"] == CHECKS:
            name, what = "Checks", "Automated tests that check the rest works"
        elif part["id"] == "":
            name, what = "Main folder", "Project settings, licence and readme"
        else:
            name, what = _name(part["id"]), _role(part["id"])
        result.append({**part, "name": name, "what": what or "",
                       "languages": [n for n, _ in part["languages"].most_common(3)]})
    result.sort(key=lambda p: (p["id"] == CHECKS, p["id"] == "", -p["lines"]))
    return result


def reading_path(files: list[dict]) -> list[dict]:
    """The files to read first, in order: where the app starts, then the files most others
    use, then the main file of each part not yet visited. Each with why it comes now."""
    code = [f for f in files if f["role"] not in ("Check", "Docs", "Settings") and f["lines"]]
    picked: list[dict] = []

    def add(f: dict, why: str):
        if f["path"] not in {p["path"] for p in picked} and len(picked) < MAX_READING:
            picked.append({"path": f["path"], "why": why})

    readme = next((f for f in files if PurePosixPath(f["path"]).name.lower().startswith("readme")
                   and "/" not in f["path"]), None)
    if readme:
        add(readme, "The project's own introduction: what it is and how to run it.")
    for f in sorted((f for f in code if f["role"] == "Starts the app"), key=lambda f: -len(f["depends_on"]))[:2]:
        add(f, "This is where the app starts.")
    for f in sorted((f for f in code if len(f["used_by"]) >= 2), key=lambda f: (-len(f["used_by"]), f["path"]))[:4]:
        add(f, f"{len(f['used_by'])} other files use it, so it shows up everywhere.")
    seen = {next(f["part"] for f in files if f["path"] == p["path"]) for p in picked}
    for f in sorted(code, key=lambda f: (-(len(f["used_by"]) + len(f["depends_on"])), -f["lines"])):
        if f["part"] not in seen:
            seen.add(f["part"])
            add(f, "The busiest file in its part of the project.")
    return picked


def scan_data(texts: dict[str, str]) -> dict:
    """Database tables, settings read from the environment and browser storage keys, each
    with the files that name it. Only what the code itself shows."""
    tables: dict[str, set[str]] = {}
    settings: dict[str, set[str]] = {}
    storage: dict[str, set[str]] = {}
    for path, text in texts.items():
        name = PurePosixPath(path).name.lower()
        if is_test(path):
            continue
        if path.endswith(".sql") or path.endswith(".py"):
            for table in _SQL_TABLE.findall(text):
                # Tables made only to copy and rename (sessions_new) aren't tables of their own.
                if not table.lower().endswith(("_new", "_old", "_tmp")):
                    tables.setdefault(table, set()).add(path)
        for table in _ORM_TABLE.findall(text):
            tables.setdefault(table, set()).add(path)
        if path.endswith(".py") and "models" in name:
            for model in _DJANGO_MODEL.findall(text):
                tables.setdefault(model, set()).add(path)
        if name.startswith(".env") and name != ".env":
            for key in _ENV_EXAMPLE.findall(text):
                settings.setdefault(key, set()).add(path)
        for match in _ENV.finditer(text):
            settings.setdefault(next(g for g in match.groups() if g), set()).add(path)
        for key in _STORAGE.findall(text):
            storage.setdefault(key, set()).add(path)

    def listed(found: dict[str, set[str]]) -> list[dict]:
        return [{"name": k, "files": sorted(v)} for k, v in sorted(found.items())][:MAX_FOUND]
    return {"tables": listed(tables), "settings": listed(settings), "storage": listed(storage)}


def scan_routes(texts: dict[str, str]) -> list[dict]:
    """The web addresses the project answers, from FastAPI, Flask and Express code."""
    found = []
    for path, text in texts.items():
        if is_test(path):
            continue
        if path.endswith(".py"):
            for match in _PY_ROUTE.finditer(text):
                method, route = match.group(1).upper(), match.group(2)
                if method == "ROUTE":
                    methods = re.search(r"methods\s*=\s*\[([^\]]*)\]", match.group("rest"))
                    method = ",".join(m.strip(" '\"").upper() for m in methods.group(1).split(",")) if methods else "GET"
                if route.startswith("/"):
                    found.append({"method": method, "path": route, "file": path})
        elif PurePosixPath(path).suffix in (".js", ".ts", ".mjs", ".cjs"):
            for method, route in _JS_ROUTE.findall(text):
                found.append({"method": method.upper(), "path": route, "file": path})
    return found[:200]


def scan_commands(texts: dict[str, str]) -> list[dict]:
    """The commands the project defines: package.json scripts and Python console commands."""
    found = []
    for path, text in texts.items():
        name = PurePosixPath(path).name
        if name == "package.json":
            try:
                scripts = json.loads(text).get("scripts") or {}
            except (json.JSONDecodeError, AttributeError):
                continue
            found += [{"name": f"npm run {k}", "does": v, "file": path} for k, v in scripts.items() if isinstance(v, str)]
        elif name == "pyproject.toml":
            block = re.search(r"^\[project\.scripts\]\s*\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
            for key, value in re.findall(r"^\s*([\w.-]+)\s*=\s*['\"]([^'\"]+)['\"]", block.group(1) if block else "", re.M):
                found.append({"name": key, "does": f"runs {value}", "file": path})
    return found[:MAX_FOUND]


def cautions(files: list[dict]) -> list[dict]:
    """The things to be careful with, from the AI's notes on each file: the files most others
    use first, one note each."""
    ranked = sorted((f for f in files if f.get("explanation") and f["explanation"].get("notes")),
                    key=lambda f: (-len(f["used_by"]), f["path"]))
    found = []
    for f in ranked:
        note = next((n for n in f["explanation"]["notes"] if not re.search(r"\bunclear\b|\bempty\b", n, re.I)), None)
        if note:
            found.append({"path": f["path"], "note": note})
        if len(found) == MAX_CAUTIONS:
            break
    return found


def verify(ai: dict | None, doc: dict) -> dict | None:
    """The AI's part of the KT with only what the code bears out: parts and files that exist,
    and flows whose steps each name a real file linked to the one before by an import."""
    if not ai:
        return None
    paths = {f["path"] for f in doc["files"]}
    part_ids = {p["id"] for p in doc["parts"]}
    reading = {r["path"] for r in doc["reading"]}
    imports = {(f["path"], d) for f in doc["files"] for d in f["depends_on"]}
    flows = []
    for flow in ai.get("flows") or []:
        steps = []
        for step in flow["steps"]:
            if step["path"] not in paths:
                continue
            if steps:
                before = steps[-1]["path"]
                if (before, step["path"]) in imports:
                    step = {**step, "link": "uses"}
                elif (step["path"], before) in imports:
                    step = {**step, "link": "used by"}
                else:
                    continue
            else:
                step = {**step, "link": None}
            if step["path"] not in {s["path"] for s in steps}:
                steps.append(step)
        if len(steps) >= MIN_FLOW_STEPS:
            flows.append({"name": flow["name"], "steps": steps})
    return {
        "summary": ai.get("summary") or "",
        "audience": ai.get("audience") or "",
        "problem": ai.get("problem") or "",
        "parts": [p for p in ai.get("parts") or [] if p["id"] in part_ids],
        "reading": [r for r in ai.get("reading") or [] if r["path"] in reading],
        "flows": flows,
        "glossary": (ai.get("glossary") or [])[:12],
    }


def subject(doc: dict) -> dict:
    """What the AI's part depends on: the files as they are, so an edit asks again."""
    return {"files": sorted([f["path"], f["sha"]] for f in doc["files"])}


def context(doc: dict) -> dict:
    """What the AI is shown to write its part: the facts above and each file's note."""
    return {
        "parts": [{"id": p["id"], "folder": p["folder"] or "(main folder)", "files": p["files"], "guess": p["what"]}
                  for p in doc["parts"]],
        "files": [{"path": f["path"], "part": f["part"], "role": f["role"], "uses": f["depends_on"][:12],
                   "note": (f.get("explanation") or {}).get("purpose") or f.get("own_note") or ""}
                  for f in doc["files"] if f["role"] != "Check"],
        "reading": [r["path"] for r in doc["reading"]],
        "routes": len(doc["routes"]),
        "tables": [t["name"] for t in doc["data"]["tables"]],
    }
