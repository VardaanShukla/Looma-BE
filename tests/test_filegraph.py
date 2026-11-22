from fastapi.testclient import TestClient

from conftest import git
from looma.api.app import create_app
from looma.capture.install import init
from looma.filegraph import file_graph, own_note


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def small_project(repo):
    (repo / "app.py").write_text('"""Starts the greeter.\n\nMore details here."""\nfrom helpers import shout\n\n'
                                 "print(shout('hi'))\n")
    (repo / "helpers.py").write_text("# Text helpers.\ndef shout(text):\n    return text.upper()\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_helpers.py").write_text("from helpers import shout\n\n\n"
                                                    "def test_shout():\n    assert shout('a') == 'A'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "greeter")


def test_each_file_knows_what_it_uses_and_what_uses_it(repo):
    small_project(repo)
    files = {f["path"]: f for f in file_graph(repo, "HEAD")["files"]}
    assert files["app.py"]["depends_on"] == ["helpers.py"]
    assert files["app.py"]["used_by"] == [] and files["app.py"]["flow"] == "Starts the app"
    assert files["app.py"]["lines"] == 6
    # Tests are kept apart from the files that really use it.
    assert files["helpers.py"]["used_by"] == ["app.py"]
    assert files["helpers.py"]["tests"] == ["tests/test_helpers.py"]
    assert files["helpers.py"]["flow"] == "Used by 1 file"
    assert files["tests/test_helpers.py"]["flow"] == "Test"
    # The file's own words: the first paragraph only.
    assert files["app.py"]["own_note"] == "Starts the greeter."
    assert files["helpers.py"]["own_note"] == "Text helpers."
    assert files["tests/test_helpers.py"]["own_note"] is None


def test_own_note_reads_only_what_is_written():
    assert own_note("a.ts", "/**\n * Talks to the server.\n */\nexport const x = 1\n") == "Talks to the server."
    assert own_note("a.ts", "// Shapes of the data.\n// Mirrors the server.\nimport x from 'y'\n") == \
        "Shapes of the data. Mirrors the server."
    assert own_note("a.ts", "import x from 'y'\n// later comment\n") is None
    assert own_note("README.md", "# Name\n\nA tool for teams.\nIt helps.\n\n## Run\n") == "A tool for teams. It helps."
    assert own_note("run.sh", "#!/bin/sh\n# Starts both servers.\nnpm start\n") == "Starts both servers."


def test_detail_route_adds_the_saved_notes(repo, tmp_path, monkeypatch):
    init(repo)
    small_project(repo)
    [listed] = client().get("/api/repos").json()
    detail = client().get(f"/api/repos/{listed['id']}/kt/detail").json()
    assert detail["complete"] and detail["explained"] == 0
    helpers = next(f for f in detail["files"] if f["path"] == "helpers.py")
    assert helpers["used_by"] == ["app.py"] and helpers["tests"] == ["tests/test_helpers.py"]
    assert helpers["explanation"] is None and helpers["own_note"] == "Text helpers."

    from test_explain import fake_claude
    fake_claude(tmp_path, monkeypatch, {"purpose": "Makes text loud.", "functions": [], "connects": "", "notes": []})
    client().post(f"/api/repos/{listed['id']}/explain/file", json={"path": "helpers.py"})
    detail = client().get(f"/api/repos/{listed['id']}/kt/detail").json()
    assert detail["explained"] == 1
    helpers = next(f for f in detail["files"] if f["path"] == "helpers.py")
    assert helpers["explanation"]["purpose"] == "Makes text loud."


def test_python_imports_one_after_another_each_count(repo):
    (repo / "src" / "pkg" / "store").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "git.py").write_text("")
    (repo / "src" / "pkg" / "paths.py").write_text("")
    (repo / "src" / "pkg" / "store" / "__init__.py").write_text("def connect():\n    pass\n")
    (repo / "src" / "pkg" / "main.py").write_text(
        "from pkg import git, paths as where\nfrom pkg.store import connect\nimport os, pkg.paths\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "pkg")
    files = {f["path"]: f for f in file_graph(repo, "HEAD")["files"]}
    # The package's __init__ counts only when something is taken from it, here store's connect.
    assert files["src/pkg/main.py"]["depends_on"] == ["src/pkg/git.py", "src/pkg/paths.py", "src/pkg/store/__init__.py"]


def test_a_file_note_can_be_written_again_and_a_failure_keeps_the_old_one(repo, tmp_path, monkeypatch):
    from test_explain import fake_claude

    init(repo)
    small_project(repo)
    [listed] = client().get("/api/repos").json()
    url = f"/api/repos/{listed['id']}/explain/file"
    calls = fake_claude(tmp_path, monkeypatch, {"purpose": "Old note.", "functions": [], "connects": "", "notes": []})
    client().post(url, json={"path": "helpers.py"})
    fake_claude(tmp_path, monkeypatch, {"purpose": "New note.", "functions": [], "connects": "", "notes": []})
    # Saved: asked again only with fresh.
    assert client().post(url, json={"path": "helpers.py"}).json()["purpose"] == "Old note."
    assert client().post(url, json={"path": "helpers.py", "fresh": True}).json()["purpose"] == "New note."
    assert len(calls.read_text().splitlines()) == 2

    fake_claude(tmp_path, monkeypatch, {}, is_error=True, error="Out of credit")
    assert client().post(url, json={"path": "helpers.py", "fresh": True}).status_code >= 400
    detail = client().get(f"/api/repos/{listed['id']}/kt/detail").json()
    assert next(f for f in detail["files"] if f["path"] == "helpers.py")["explanation"]["purpose"] == "New note."
