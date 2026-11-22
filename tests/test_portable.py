"""Looma works wherever a project is and on any system: moved or re-cloned projects keep
their history, pasted paths are understood, and the page comes with `looma serve`."""

import shutil

from fastapi.testclient import TestClient

from conftest import connect_claude, git
from looma.api import app as app_module
from looma.api.app import create_app
from looma.capture import install
from looma.capture.install import init
from looma.store import connect
from test_capture import send


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def repos():
    return [dict(row) for row in connect().execute("SELECT id, path, name, claude, root_commit FROM repos")]


def test_a_moved_project_keeps_its_history_and_claude_setting(repo, tmp_path):
    connect_claude(repo)
    [before] = repos()
    moved = tmp_path / "Renamed Folder"
    shutil.move(repo, moved)

    init(moved)

    [after] = repos()
    assert after["id"] == before["id"] and after["path"] == str(moved.resolve())
    assert after["name"] == "Renamed Folder" and after["claude"] == 1
    assert "looma record post-commit" in (moved / ".git" / "hooks" / "post-commit").read_text()


def test_a_fresh_clone_takes_the_place_of_a_deleted_folder(repo, tmp_path):
    init(repo)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    # Both folders exist: two projects.
    init(clone)
    assert len(repos()) == 2

    shutil.rmtree(repo)
    again = tmp_path / "again"
    git(tmp_path, "clone", "-q", str(clone), str(again))
    init(again)
    assert sorted(r["path"] for r in repos()) == sorted([str(clone.resolve()), str(again.resolve())])


def test_a_project_tracked_before_first_commits_were_stored_matches_by_name(repo, tmp_path):
    init(repo)
    conn = connect()
    with conn:
        conn.execute("UPDATE repos SET root_commit = NULL, path = ?", (str(tmp_path / "gone" / "PROJECT"),))
    conn.close()

    init(repo)

    [row] = repos()
    assert row["path"] == str(repo) and row["root_commit"]


def test_a_commit_in_a_moved_folder_is_still_recorded(repo, tmp_path):
    connect_claude(repo)
    moved = tmp_path / "moved"
    shutil.move(repo, moved)

    send("user-prompt-submit", moved, prompt="Rename hello")
    send("pre-tool-use", moved, tool_name="Edit", tool_input={"file_path": str(moved / "app.py")})
    (moved / "app.py").write_text("def hi():\n    return 'hi'\n")
    send("stop", moved)

    [row] = repos()
    assert row["path"] == str(moved.resolve())
    assert connect().execute("SELECT file_path FROM changes").fetchone()["file_path"] == "app.py"


def test_pasted_paths_in_any_form(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PROJECTS", str(repo.parent))
    api = client()
    for typed in (f'"{repo}"', f"  '{repo}'  ", "~/project", "project", "$PROJECTS/project"):
        response = api.post("/api/repos", json={"path": typed})
        assert response.status_code == 201, (typed, response.json())
    assert len(repos()) == 1


def test_the_hook_runs_under_sh_with_unix_line_endings(repo):
    connect_claude(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    assert b"\r\n" not in hook.read_bytes()
    assert "\\" not in hook.read_text()


def test_serving_refreshes_hooks_that_name_an_old_python(repo):
    connect_claude(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(hook.read_text().replace(install.record_command("post-commit"),
                                             "/old/python -I -m looma record post-commit"))
    conn = connect()
    with conn:
        conn.execute("UPDATE repos SET root_commit = NULL")
    conn.close()

    assert install.refresh() == []

    assert install.record_command("post-commit") in hook.read_text() and "/old/python" not in hook.read_text()
    assert repos()[0]["root_commit"]


def test_the_page_comes_with_the_api_and_signing_in_still_guards_the_data(tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>Looma</title>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(app_module, "WEB", web)

    me = client()
    assert me.put("/api/auth/password", json={"username": "me", "password": "correct horse"}).status_code == 200
    stranger = client()
    assert "Looma" in stranger.get("/").text
    assert stranger.get("/assets/app.js").status_code == 200
    assert stranger.get("/api/repos").status_code == 401
    assert stranger.get("/docs").status_code == 401
    assert stranger.get("/api/nope").status_code == 401
    assert me.get("/api/repos").status_code == 200


def test_the_built_page_is_in_the_package():
    assert (app_module.WEB / "index.html").is_file()

