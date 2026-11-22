from fastapi.testclient import TestClient

from conftest import git
from looma.api.app import create_app
from looma.capture.install import init


def test_compare_over_http(repo):
    init(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text("def hello():\n    return 'hello'\n")
    git(repo, "commit", "-q", "-am", "edit")
    client = TestClient(create_app(), base_url="http://127.0.0.1")

    [listed] = client.get("/api/repos").json()
    assert listed["branch"] == "feature" and listed["claude"] is False

    branches = client.get(f"/api/repos/{listed['id']}/branches").json()
    assert (branches["current"], branches["default_base"], branches["branches"]) == ("feature", "main", ["feature", "main"])

    result = client.get(f"/api/repos/{listed['id']}/compare").json()
    assert result["files"][0]["functions"] == [{"name": "hello", "change": "modified"}]

    assert client.get(f"/api/repos/{listed['id']}/compare", params={"head": "nope"}).status_code == 400
    assert client.get("/api/repos/999/compare").status_code == 404


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_add_project_then_turn_claude_on_and_off(repo):
    api = client()
    tools = api.get("/api/ai-tools").json()
    assert [tool["id"] for tool in tools if tool["available"]] == ["claude", "codex", "antigravity"]

    added = api.post("/api/repos", json={"path": str(repo)})
    assert added.status_code == 201 and added.json()["ai"] is None
    repo_id = added.json()["id"]
    steps = {step["id"]: step for step in api.get(f"/api/repos/{repo_id}/setup").json()["steps"]}
    assert steps["track"]["done"] is True and steps["ai"]["done"] is False

    assert api.put(f"/api/repos/{repo_id}/ai", json={"ai": "claude"}).json()["ai"] == "claude"
    steps = {step["id"]: step for step in api.get(f"/api/repos/{repo_id}/setup").json()["steps"]}
    assert steps["git-hook"]["done"] is True and steps["claude-hooks"]["done"] is True

    assert api.put(f"/api/repos/{repo_id}/ai", json={"ai": None}).json()["ai"] is None
    assert not (repo / ".git" / "hooks" / "post-commit").exists()


def test_adding_a_folder_that_is_not_there(tmp_path):
    api = client()
    assert api.post("/api/repos", json={"path": str(tmp_path / "missing")}).status_code == 400


def test_a_folder_with_git_repos_inside_points_to_them(tmp_path, repo):
    response = client().post("/api/repos", json={"path": str(tmp_path)})
    assert response.status_code == 400 and "isn't a git project itself" in response.json()["detail"]


def test_a_folder_without_git_is_added_with_git_features_off(tmp_path):
    folder = tmp_path / "notes-app"
    (folder / "src").mkdir(parents=True)
    (folder / "README.md").write_text("# Notes app\n\nKeeps notes.\n")
    (folder / "src" / "main.py").write_text("from src import util\n\ndef run():\n    return util.go()\n")
    (folder / "src" / "util.py").write_text("def go():\n    return 1\n")
    (folder / ".env").write_text("SECRET=1\n")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "dep.js").write_text("x")
    api = client()

    # Asking to record an AI tool doesn't stop it being added; recording just stays off.
    added = api.post("/api/repos", json={"path": str(folder), "ai": "claude"})
    assert added.status_code == 201
    project = added.json()
    assert (project["exists"], project["git"], project["branch"], project["ai"]) == (True, False, None, None)
    repo_id = project["id"]
    assert api.get("/api/repos").json()[0]["git"] is False

    # What reads files works.
    project_map = api.get(f"/api/repos/{repo_id}/map").json()
    listed = {f["path"] for area in project_map["areas"] for f in area["file_list"]}
    assert listed == {"README.md", "src/main.py", "src/util.py"}  # no .env, no node_modules
    summary = api.get(f"/api/repos/{repo_id}/summary")
    assert summary.status_code == 200 and summary.json()["history"]["commits"] == 0
    kt = api.get(f"/api/repos/{repo_id}/kt").json()
    assert {f["path"] for f in kt["files"]} >= {"src/main.py", "src/util.py"}

    # Polling for changes sees a saved file.
    before = api.get(f"/api/repos/{repo_id}/state").json()["signature"]
    (folder / "src" / "util.py").write_text("def go():\n    return 2  # changed\n")
    assert api.get(f"/api/repos/{repo_id}/state").json()["signature"] != before

    # What needs git says so instead of failing.
    assert api.post(f"/api/repos/{repo_id}/sync", json={}).json() == {"synced_at": None, "error": None}
    pulls = api.get(f"/api/repos/{repo_id}/pulls").json()
    assert (pulls["host"], pulls["pulls"]) == (None, [])
    for url in ("branches", "flow", "compare"):
        response = api.get(f"/api/repos/{repo_id}/{url}")
        assert response.status_code == 409 and "needs git" in response.json()["detail"], url
    turned_on = api.put(f"/api/repos/{repo_id}/ai", json={"ai": "claude"})
    assert turned_on.status_code == 409 and "needs git" in turned_on.json()["detail"]
    steps = {step["id"]: step for step in api.get(f"/api/repos/{repo_id}/setup").json()["steps"]}
    assert steps["git"]["done"] is False and "ai" not in steps

    # Once it becomes a git repository, the same project gets its git features.
    git(folder, "init", "-q", "-b", "main")
    again = api.get("/api/repos").json()[0]
    assert (again["id"], again["git"], again["branch"]) == (repo_id, True, "main")

    assert api.request("DELETE", f"/api/repos/{repo_id}", json={}).status_code == 204
    assert folder.is_dir() and api.get("/api/repos").json() == []


def test_changes_only_from_this_machine_and_the_ui(repo):
    other_host = TestClient(create_app(), base_url="http://evil.example")
    assert other_host.get("/api/repos").status_code == 403

    api = client()
    from_site = api.post("/api/repos", json={"path": str(repo)}, headers={"origin": "https://evil.example"})
    assert from_site.status_code == 403
    as_form = api.post("/api/repos", content=f'{{"path": "{repo}"}}', headers={"content-type": "text/plain"})
    assert as_form.status_code == 415
    from_ui = api.post("/api/repos", json={"path": str(repo)}, headers={"origin": "http://localhost:5173"})
    assert from_ui.status_code == 201


def test_merge_and_delete_on_github_show_up_after_sync(repo, tmp_path):
    # The repo fixture plays GitHub; "local" is the developer's clone of it.
    git(repo, "checkout", "-q", "-b", "Dev")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(remote))
    local = tmp_path / "local"
    git(tmp_path, "clone", "-q", "-b", "Dev", str(remote), str(local))
    git(local, "config", "user.email", "dev@example.com")
    git(local, "config", "user.name", "Dev")
    git(local, "checkout", "-q", "-b", "feature/addLogin")
    (local / "login.py").write_text("def login():\n    pass\n")
    git(local, "add", ".")
    git(local, "commit", "-q", "-m", "added login")
    git(local, "push", "-q", "-u", "origin", "feature/addLogin")
    (local / "notes.txt").write_text("still editing\n")

    # On GitHub: merge the pull request into Dev and delete the branch.
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", "Dev", str(remote), str(other))
    git(other, "-c", "user.email=a@b", "-c", "user.name=A", "merge", "-q", "--no-ff", "-m", "Merge PR",
        "origin/feature/addLogin")
    git(other, "push", "-q", "origin", "Dev", ":feature/addLogin")

    init(local)
    api = client()
    repo_id = api.get("/api/repos").json()[0]["id"]
    before = api.get(f"/api/repos/{repo_id}/compare").json()
    assert [f["path"] for f in before["files"]] == ["login.py"]  # this machine hasn't heard yet

    synced = api.post(f"/api/repos/{repo_id}/sync", json={}).json()
    assert synced["error"] is None and synced["synced_at"]

    details = {b["name"]: b for b in api.get(f"/api/repos/{repo_id}/branches").json()["details"]}
    shown = details["feature/addLogin"]
    assert (shown["merged"], shown["gone"]) == (True, True) and shown["removes_at"]
    after = api.get(f"/api/repos/{repo_id}/compare").json()
    assert after["base_ref"] == "origin/Dev" and after["files"] == [] and after["ahead"] == 0

    with_edits = api.get(f"/api/repos/{repo_id}/compare", params={"uncommitted": True}).json()
    assert [(f["path"], f["uncommitted"]) for f in with_edits["files"]] == [("notes.txt", True)]


def test_merged_branch_is_marked_then_dropped_after_15_minutes(repo):
    from datetime import datetime, timedelta, timezone

    from looma.store import connect

    git(repo, "checkout", "-q", "-b", "done")
    (repo / "done.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "done")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge done", "done")
    git(repo, "checkout", "-q", "-b", "next")  # new, nothing committed: not merged
    init(repo)
    api = client()
    repo_id = api.get("/api/repos").json()[0]["id"]

    listed = api.get(f"/api/repos/{repo_id}/branches").json()
    details = {b["name"]: b for b in listed["details"]}
    assert details["done"]["merged"] and details["done"]["removes_at"]
    assert (details["next"]["merged"], details["next"]["removes_at"]) == (False, None)
    assert "done" in listed["branches"]

    # 16 minutes later.
    conn = connect()
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(timespec="seconds")
    conn.execute("UPDATE settings SET value = ? WHERE name = ?", (f'{{"done": "{earlier}"}}', f"retired-branches:{repo_id}"))
    conn.commit()
    conn.close()
    listed = api.get(f"/api/repos/{repo_id}/branches").json()
    assert sorted(listed["branches"]) == ["main", "next"]
    assert "done" not in {b["name"] for b in listed["details"]}
    assert "done" in git(repo, "branch", "--list", "done")  # still in git; only Looma stops showing it


def test_sync_reports_an_unreachable_remote(repo):
    git(repo, "remote", "add", "origin", "/nonexistent/remote.git")
    init(repo)
    api = client()
    repo_id = api.get("/api/repos").json()[0]["id"]
    result = api.post(f"/api/repos/{repo_id}/sync", json={}).json()
    assert result["error"] and result["synced_at"] is None


def test_remove_a_project_keeps_its_files_and_drops_the_hook(repo):
    api = client()
    repo_id = api.post("/api/repos", json={"path": str(repo), "ai": "claude"}).json()["id"]
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hook.exists()

    assert api.request("DELETE", f"/api/repos/{repo_id}", json={}).status_code == 204
    assert api.get("/api/repos").json() == []
    assert not hook.exists()
    assert (repo / "app.py").exists()
    assert api.request("DELETE", f"/api/repos/{repo_id}", json={}).status_code == 404


def test_remove_a_project_whose_folder_is_gone(repo, tmp_path):
    import shutil

    api = client()
    repo_id = api.post("/api/repos", json={"path": str(repo)}).json()["id"]
    shutil.rmtree(repo)
    assert api.request("DELETE", f"/api/repos/{repo_id}", json={}).status_code == 204
    assert api.get("/api/repos").json() == []


def test_flow_shows_branches_forking_and_merging(repo):
    init(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text("def hello():\n    return 'hello'\n")
    git(repo, "commit", "-q", "-am", "edit")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    git(repo, "checkout", "-q", "-b", "next")
    [listed] = client().get("/api/repos").json()

    result = client().get(f"/api/repos/{listed['id']}/flow").json()
    assert (result["current"], result["base"], result["truncated"]) == ("next", "main", False)
    subjects = [commit["subject"] for commit in result["commits"]]
    assert subjects == ["merge feature", "edit", "initial"]
    merge, edit, initial = result["commits"]
    assert merge["parents"] == [initial["sha"], edit["sha"]]
    assert sorted(merge["branches"]) == ["main", "next"] and edit["branches"] == ["feature"]

    limited = client().get(f"/api/repos/{listed['id']}/flow", params={"limit": 2}).json()
    assert len(limited["commits"]) == 2 and limited["truncated"] is True


def test_project_map_shows_parts_and_what_uses_what(repo):
    init(repo)
    files = {
        "src/api/client.ts": "export const getUser = () => fetch('/api/user');\n",
        "src/screens/Home.tsx": "import { getUser } from '../api/client';\nimport React from 'react';\n",
        "src/screens/Profile.tsx": "import { getUser } from \"../api/client\";\n",
        "src/styles/theme.css": "body {}\n",
        "server/db.py": "def connect():\n    pass\n",
        "server/app.py": "from .db import connect\n",
        "node_modules/react/index.js": "module.exports = {};\n",
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-f", ".")
    git(repo, "commit", "-q", "-m", "app")
    [listed] = client().get("/api/repos").json()

    result = client().get(f"/api/repos/{listed['id']}/map").json()
    areas = {area["id"]: area for area in result["areas"]}
    assert set(areas) == {"src/api", "src/screens", "src/styles", "server", ""}
    assert areas["src/screens"]["role"] == "Screens people see"
    assert areas["src/screens"]["languages"] == [{"name": "TypeScript", "files": 2}]
    listed = areas["src/screens"]["file_list"]
    assert len(listed) == 2 and all(f["path"].startswith("src/screens/") for f in listed)
    assert [f["bytes"] for f in listed] == sorted((f["bytes"] for f in listed), reverse=True)
    # npm packages and build folders aren't the project's own code.
    assert result["total_files"] == 7
    assert result["edges"] == [{"source": "src/screens", "target": "src/api", "imports": 2}]
    assert result["complete"] is True


def test_project_map_follows_python_imports_into_a_src_folder(repo):
    init(repo)
    files = {
        "backend/src/shop/__init__.py": "",
        "backend/src/shop/cart.py": "def total():\n    return 0\n",
        "backend/src/shop/api/__init__.py": "",
        "backend/src/shop/api/routes.py": "from shop.cart import total\n",
        "backend/tests/test_cart.py": "from shop.cart import total\n",
        "backend/tests/test_routes.py": "import shop.api.routes\n",
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "shop")
    [listed] = client().get("/api/repos").json()

    result = client().get(f"/api/repos/{listed['id']}/map").json()
    sources = {(edge["source"], edge["target"]) for edge in result["edges"]}
    tests = next(area["id"] for area in result["areas"] if area["name"] == "tests")
    assert any(source == tests for source, _ in sources)
