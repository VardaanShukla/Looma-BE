from fastapi.testclient import TestClient

from conftest import git
from looma import ktdoc
from looma.api.app import create_app
from looma.capture.install import init


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def shop(repo):
    """A small shop: a server that starts the app, routes, a database and a test."""
    (repo / "app.py").write_text("from api import routes\n\nroutes.start()\n")
    (repo / "api").mkdir()
    (repo / "api" / "__init__.py").write_text("")
    (repo / "api" / "routes.py").write_text(
        '"""The web addresses."""\nimport os\nfrom flask import Flask\nfrom store import db\n\napp = Flask(__name__)\n'
        'KEY = os.environ.get("SHOP_KEY")\n\n\n@app.route("/items", methods=["GET", "POST"])\ndef items():\n'
        "    return db.items()\n\n\n@app.get(\"/health\")\ndef health():\n    return 'ok'\n\n\ndef start():\n    pass\n")
    (repo / "store").mkdir()
    (repo / "store" / "__init__.py").write_text("")
    (repo / "store" / "db.py").write_text(
        'import os\nDB = os.getenv("SHOP_DB")\nSQL = "CREATE TABLE IF NOT EXISTS items (id INTEGER)"\n\n\n'
        "def items():\n    return []\n")
    (repo / "store" / "schema.sql").write_text("create table orders (id int);\ncreate table orders_new (id int);\n")
    (repo / ".env.example").write_text("SHOP_KEY=\nexport SHOP_MAIL=x\n")
    (repo / "web.js").write_text("localStorage.setItem('shop.cart', '[]');\nrouter.post('/pay', pay);\n")
    (repo / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    (repo / "pyproject.toml").write_text('[project]\nname = "shop"\n\n[project.scripts]\nshop = "app:main"\n')
    (repo / "tests").mkdir()
    (repo / "tests" / "test_db.py").write_text("from store import db\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "shop")


def test_scanners_find_tables_settings_storage_routes_and_commands(repo):
    shop(repo)
    doc = ktdoc.document(repo, "HEAD")
    data = doc["data"]
    assert [t["name"] for t in data["tables"]] == ["items", "orders"]  # orders_new is only a copy
    assert [s["name"] for s in data["settings"]] == ["SHOP_DB", "SHOP_KEY", "SHOP_MAIL"]
    assert data["storage"] == [{"name": "shop.cart", "files": ["web.js"]}]
    routes = {(r["method"], r["path"], r["file"]) for r in doc["routes"]}
    assert routes == {("GET,POST", "/items", "api/routes.py"), ("GET", "/health", "api/routes.py"),
                      ("POST", "/pay", "web.js")}
    commands = {(c["name"], c["does"]) for c in doc["commands"]}
    assert commands == {("npm run dev", "vite"), ("shop", "runs app:main")}


def test_tests_are_their_own_part_and_each_file_has_a_role(repo):
    shop(repo)
    doc = ktdoc.document(repo, "HEAD")
    files = {f["path"]: f for f in doc["files"]}
    assert files["tests/test_db.py"]["part"] == "checks" and files["tests/test_db.py"]["role"] == "Check"
    assert files["app.py"]["role"] == "Starts the app"
    assert files["store/db.py"]["role"] == "Data" and files["store/schema.sql"]["role"] == "Data"
    assert doc["parts"][-1]["id"] == "checks"
    # The tests' imports don't draw links between parts.
    assert all("checks" not in (link["source"], link["target"]) for link in doc["links"])


def test_reading_path_starts_where_the_app_starts(repo):
    shop(repo)
    (repo / "README.md").write_text("# Shop\n\nSells things.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "readme")
    reading = [r["path"] for r in ktdoc.document(repo, "HEAD")["reading"]]
    assert reading[:2] == ["README.md", "app.py"]
    assert "tests/test_db.py" not in reading and len(reading) <= ktdoc.MAX_READING


def test_flow_steps_that_name_fake_files_or_skip_imports_are_dropped(repo):
    shop(repo)
    doc = ktdoc.document(repo, "HEAD")
    ai = {"summary": "A shop.", "parts": [{"id": "api", "name": "Server", "what": "Answers."},
                                         {"id": "nowhere", "name": "Fake", "what": "Made up."}],
          "reading": [{"path": "app.py", "why": "Start.", "after": "After this you know."},
                      {"path": "fake.py", "why": "x", "after": "y"}],
          "flows": [{"name": "What happens when you list items",
                     "steps": [{"path": "app.py", "what": "Starts."}, {"path": "fake.py", "what": "Invented."},
                               {"path": "api/routes.py", "what": "Answers."}, {"path": "web.js", "what": "Not linked."},
                               {"path": "store/db.py", "what": "Reads."}]},
                    {"name": "Too short", "steps": [{"path": "app.py", "what": "x"}, {"path": "store/db.py", "what": "y"}]}],
          "glossary": [{"term": "route", "meaning": "A web address."}]}
    checked = ktdoc.verify(ai, doc)
    assert [p["id"] for p in checked["parts"]] == ["api"]
    assert [r["path"] for r in checked["reading"]] == ["app.py"]
    [flow] = checked["flows"]
    assert [(s["path"], s["link"]) for s in flow["steps"]] == [
        ("app.py", None), ("api/routes.py", "uses"), ("store/db.py", "uses")]


def test_working_folder_shows_files_not_committed_yet(repo):
    init(repo)
    shop(repo)
    (repo / "store" / "db.py").unlink()
    (repo / "new.py").write_text("x = 1\n")
    [listed] = client().get("/api/repos").json()
    url = f"/api/repos/{listed['id']}/kt/detail"
    # Changes not committed: the working folder by default.
    detail = client().get(url).json()
    paths = {f["path"] for f in detail["files"]}
    assert detail["worktree"] and detail["has_changes"] and detail["source"]["kind"] == "worktree"
    assert "new.py" in paths and "store/db.py" not in paths
    committed = client().get(url, params={"worktree": "false"}).json()
    paths = {f["path"] for f in committed["files"]}
    assert not committed["worktree"] and committed["source"]["kind"] == "branch" and committed["source"]["commit"]
    assert "store/db.py" in paths and "new.py" not in paths
    assert committed["written"] is False and committed["flows"] == [] and committed["summary"] == ""


def test_writing_the_detailed_kt_keeps_only_what_the_code_shows(repo, tmp_path, monkeypatch):
    from test_explain import fake_claude

    init(repo)
    shop(repo)
    [listed] = client().get("/api/repos").json()
    calls = fake_claude(tmp_path, monkeypatch, {
        "summary": "A small shop.", "audience": "Shop owners.", "problem": "Selling online.",
        "parts": [{"id": "store", "name": "Data", "what": "Keeps items."}],
        "reading": [{"path": "app.py", "why": "It starts it all.", "after": "After this you know the start."}],
        "flows": [{"name": "What happens when you open the shop", "steps": [
            {"path": "app.py", "what": "Starts."}, {"path": "api/routes.py", "what": "Answers."},
            {"path": "store/db.py", "what": "Reads."}, {"path": "ghost.py", "what": "Invented."}]}],
        "glossary": [{"term": "database", "meaning": "Where the shop keeps its data."}]})
    url = f"/api/repos/{listed['id']}/kt/detail"
    written = client().post(url, json={}).json()
    assert written["written"] and written["summary"] == "A small shop."
    assert next(p for p in written["parts"] if p["id"] == "store")["name"] == "Data"
    assert written["reading"][0]["after"] in (None, "After this you know the start.")
    assert [s["path"] for s in written["flows"][0]["steps"]] == ["app.py", "api/routes.py", "store/db.py"]
    assert client().get(url).json()["glossary"] == [{"term": "database", "meaning": "Where the shop keeps its data."}]
    client().post(url, json={})
    assert len(calls.read_text().splitlines()) == 1  # saved: asked once
    client().post(url, json={"fresh": True})
    assert len(calls.read_text().splitlines()) == 2
