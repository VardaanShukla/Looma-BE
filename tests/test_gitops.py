from conftest import git
from fastapi.testclient import TestClient

from looma import explain
from looma.api.app import create_app
from looma.capture.install import init


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def setup(repo):
    init(repo)
    api = client()
    return api, api.get("/api/repos").json()[0]["id"]


def act(api, repo_id, action, **body):
    response = api.post(f"/api/repos/{repo_id}/git/{action}", json=body)
    assert response.status_code == 200, response.json()
    return response.json()


def refused(api, repo_id, action, **body):
    response = api.post(f"/api/repos/{repo_id}/git/{action}", json=body)
    assert response.status_code == 409, response.json()
    return response.json()["detail"]


def status(api, repo_id):
    return api.get(f"/api/repos/{repo_id}/git").json()


def cloned(repo, tmp_path):
    """The repo fixture plays the remote; "local" is a clone of it with a dev branch."""
    git(repo, "branch", "dev")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(remote))
    local = tmp_path / "local"
    git(tmp_path, "clone", "-q", "-b", "dev", str(remote), str(local))
    git(local, "config", "user.email", "dev@example.com")
    git(local, "config", "user.name", "Dev")
    return remote, local.resolve()


def push_from_elsewhere(remote, tmp_path, branch, name, text):
    other = tmp_path / f"other-{name}"
    git(tmp_path, "clone", "-q", "-b", branch, str(remote), str(other))
    (other / name).write_text(text)
    git(other, "add", ".")
    git(other, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "-m", f"add {name}")
    git(other, "push", "-q", "origin", branch)


def test_status_lists_files_branches_and_nothing_midway(repo):
    (repo / "app.py").write_text("def hello():\n    return 'hello'\n")
    (repo / "new.py").write_text("x = 1\n")
    git(repo, "add", "app.py")
    api, repo_id = setup(repo)
    found = status(api, repo_id)
    assert (found["branch"], found["base"], found["remote"], found["midway"]) == ("main", "main", None, None)
    files = {f["path"]: (f["staged"], f["unstaged"]) for f in found["files"]}
    assert files == {"app.py": ("modified", None), "new.py": (None, "new")}
    assert [b["name"] for b in found["branches"]] == ["main"] and found["last"] is None


def test_preview_says_the_commands_and_runs_nothing(repo):
    api, repo_id = setup(repo)
    shown = act(api, repo_id, "branch-create", name="feature/login", preview=True)
    assert shown["commands"] == ["git switch -c feature/login"] and shown["done"] is False
    assert status(api, repo_id)["branch"] == "main"


def test_add_commit_then_undo_puts_the_changes_back_added(repo):
    api, repo_id = setup(repo)
    (repo / "a.py").write_text("a = 1\n")
    (repo / "b.py").write_text("b = 1\n")
    assert "Nothing is added" in refused(api, repo_id, "commit", message="nothing yet")
    act(api, repo_id, "stage", paths=["a.py"])
    staged = {f["path"]: f["staged"] for f in status(api, repo_id)["files"]}
    assert staged == {"a.py": "added", "b.py": None}

    done = act(api, repo_id, "commit", message="Add a")
    assert done["done"] and done["commands"] == ["git commit -m 'Add a'"]
    assert git(repo, "log", "-1", "--format=%s").strip() == "Add a"

    last = status(api, repo_id)["last"]
    assert last["summary"] == "Committed “Add a”" and last["undoable"]
    act(api, repo_id, "undo")
    assert git(repo, "log", "-1", "--format=%s").strip() == "initial"
    assert {f["path"]: f["staged"] for f in status(api, repo_id)["files"]}["a.py"] == "added"
    assert status(api, repo_id)["last"] is None  # nothing left to undo


def test_add_all_and_take_back_out(repo):
    api, repo_id = setup(repo)
    (repo / "a.py").write_text("a = 1\n")
    (repo / "app.py").write_text("changed\n")
    assert act(api, repo_id, "stage")["commands"] == ["git add ."]
    assert all(f["staged"] for f in status(api, repo_id)["files"])
    act(api, repo_id, "unstage", paths=["app.py"])
    assert {f["path"]: f["staged"] for f in status(api, repo_id)["files"]} == {"a.py": "added", "app.py": None}


def test_rename_the_last_commit_message_and_undo(repo):
    api, repo_id = setup(repo)
    (repo / "a.py").write_text("a = 1\n")
    act(api, repo_id, "stage")
    act(api, repo_id, "commit", message="tpyo")
    (repo / "b.py").write_text("b = 1\n")
    act(api, repo_id, "stage", paths=["b.py"])
    act(api, repo_id, "amend", message="Add a")
    assert git(repo, "log", "-1", "--format=%s").strip() == "Add a"
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["a.py"]  # b.py stays for the next one
    act(api, repo_id, "undo")
    assert git(repo, "log", "-1", "--format=%s").strip() == "tpyo"


def test_branches_create_from_base_rename_delete_and_undo(repo):
    api, repo_id = setup(repo)
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.py").write_text("s = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "side work")

    act(api, repo_id, "branch-create", name="feature", start="base")
    assert status(api, repo_id)["branch"] == "feature"
    assert not (repo / "side.py").exists()  # made from main, not from side
    act(api, repo_id, "undo")
    assert status(api, repo_id)["branch"] == "side" and "feature" not in git(repo, "branch")

    assert "isn't a name git allows" in refused(api, repo_id, "branch-create", name="has space")
    assert "already a branch" in refused(api, repo_id, "branch-create", name="main")

    act(api, repo_id, "switch", name="main")
    act(api, repo_id, "branch-rename", name="side", new_name="side-2")
    assert "side-2" in git(repo, "branch")
    act(api, repo_id, "undo")
    assert "side-2" not in git(repo, "branch")

    side = next(b for b in status(api, repo_id)["branches"] if b["name"] == "side")
    assert side["unmerged"] == 1
    assert "Tick Delete anyway" in refused(api, repo_id, "branch-delete", name="side")
    act(api, repo_id, "branch-delete", name="side", force=True)
    assert "side" not in git(repo, "branch")
    act(api, repo_id, "undo")
    assert git(repo, "log", "-1", "--format=%s", "side").strip() == "side work"
    assert "the branch you're on" in refused(api, repo_id, "branch-delete", name="main")


def test_update_from_base_merges_origin_dev(repo, tmp_path):
    remote, local = cloned(repo, tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    (local / "mine.py").write_text("m = 1\n")
    git(local, "add", ".")
    git(local, "commit", "-q", "-m", "mine")
    push_from_elsewhere(remote, tmp_path, "dev", "theirs.py", "t = 1\n")
    api, repo_id = setup(local)

    shown = act(api, repo_id, "update", base="dev", preview=True)
    assert shown["commands"] == ["git fetch origin", "git merge --no-edit --autostash origin/dev"]
    act(api, repo_id, "update", base="dev")
    assert (local / "theirs.py").exists() and (local / "mine.py").exists()
    act(api, repo_id, "undo")
    assert not (local / "theirs.py").exists()


def test_update_by_rebase(repo, tmp_path):
    remote, local = cloned(repo, tmp_path)
    git(local, "checkout", "-q", "-b", "feature")
    (local / "mine.py").write_text("m = 1\n")
    git(local, "add", ".")
    git(local, "commit", "-q", "-m", "mine")
    push_from_elsewhere(remote, tmp_path, "dev", "theirs.py", "t = 1\n")
    (local / "app.py").write_text("uncommitted\n")  # --autostash keeps it
    api, repo_id = setup(local)
    act(api, repo_id, "update", base="dev", how="rebase")
    assert git(local, "log", "--format=%s").split("\n")[:2] == ["mine", "add theirs.py"]
    assert (local / "app.py").read_text() == "uncommitted\n"


def test_conflict_stops_midway_then_abort_or_fix_and_continue(repo):
    api, repo_id = setup(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text("feature\n")
    git(repo, "commit", "-q", "-am", "feature edit")
    git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("main\n")
    git(repo, "commit", "-q", "-am", "main edit")
    git(repo, "checkout", "-q", "feature")

    stopped = act(api, repo_id, "update", base="main")
    assert not stopped["done"] and stopped["conflicts"] == ["app.py"]
    found = status(api, repo_id)
    assert (found["midway"], found["conflicts"]) == ("merge", ["app.py"])
    assert "stopped halfway" in refused(api, repo_id, "commit", message="no")
    assert "still has conflicts" in refused(api, repo_id, "continue")
    act(api, repo_id, "abort")
    assert status(api, repo_id)["midway"] is None and (repo / "app.py").read_text() == "feature\n"

    act(api, repo_id, "update", base="main")
    (repo / "app.py").write_text("both\n")
    act(api, repo_id, "stage", paths=["app.py"])
    assert act(api, repo_id, "continue")["done"]
    assert status(api, repo_id)["midway"] is None
    assert git(repo, "log", "-1", "--format=%s").startswith("Merge branch 'main'")
    assert status(api, repo_id)["last"]["undoable"]


def test_stash_apply_pop_drop_and_bring_a_dropped_one_back(repo):
    api, repo_id = setup(repo)
    assert "nothing to stash" in refused(api, repo_id, "stash")
    (repo / "app.py").write_text("work\n")
    (repo / "new.py").write_text("n = 1\n")
    act(api, repo_id, "stash", message="half done")
    assert status(api, repo_id)["files"] == [] and not (repo / "new.py").exists()
    [entry] = status(api, repo_id)["stashes"]
    assert entry["message"].endswith("half done")

    act(api, repo_id, "stash-apply", sha=entry["sha"])
    assert (repo / "new.py").exists() and len(status(api, repo_id)["stashes"]) == 1
    git(repo, "checkout", "--", "app.py")
    (repo / "new.py").unlink()
    act(api, repo_id, "stash-drop", sha=entry["sha"])
    assert status(api, repo_id)["stashes"] == []
    act(api, repo_id, "undo")
    [back] = status(api, repo_id)["stashes"]
    assert back["sha"] == entry["sha"]
    act(api, repo_id, "stash-pop", sha=back["sha"])
    assert status(api, repo_id)["stashes"] == [] and (repo / "app.py").read_text() == "work\n"
    assert "isn't there any more" in refused(api, repo_id, "stash-drop", sha=back["sha"])


def test_push_a_new_branch_then_pull_what_others_pushed(repo, tmp_path):
    remote, local = cloned(repo, tmp_path)
    api, repo_id = setup(local)
    act(api, repo_id, "branch-create", name="feature")
    assert "isn't on the remote yet" in refused(api, repo_id, "pull")
    (local / "f.py").write_text("f = 1\n")
    act(api, repo_id, "stage")
    act(api, repo_id, "commit", message="Add f")

    shown = act(api, repo_id, "push", preview=True)
    assert shown["commands"] == ["git push --set-upstream origin feature"] and shown["notes"]
    act(api, repo_id, "push")
    found = status(api, repo_id)
    assert (found["upstream"], found["ahead"], found["pushed"]) == ("origin/feature", 0, True)
    assert "already pushed" in refused(api, repo_id, "amend", message="new words")
    assert not status(api, repo_id)["last"]["undoable"]  # the commit is on the remote now

    push_from_elsewhere(remote, tmp_path, "feature", "g.py", "g = 1\n")
    (local / "h.py").write_text("h = 1\n")
    act(api, repo_id, "stage")
    act(api, repo_id, "commit", message="Add h")
    assert "Pull first" in refused(api, repo_id, "push")
    act(api, repo_id, "pull")
    assert (local / "g.py").exists()
    act(api, repo_id, "push")
    assert status(api, repo_id)["ahead"] == 0


def test_delete_a_branch_on_the_remote_too(repo, tmp_path):
    remote, local = cloned(repo, tmp_path)
    api, repo_id = setup(local)
    act(api, repo_id, "branch-create", name="old")
    act(api, repo_id, "push")
    act(api, repo_id, "switch", name="dev")
    shown = act(api, repo_id, "branch-delete", name="old", remote=True, preview=True)
    assert shown["commands"] == ["git branch -d old", "git push origin --delete old"]
    act(api, repo_id, "branch-delete", name="old", remote=True)
    assert "old" not in git(remote, "branch")


def test_switch_to_a_branch_only_on_the_remote(repo, tmp_path):
    git(repo, "branch", "theirs")
    _, local = cloned(repo, tmp_path)
    api, repo_id = setup(local)
    assert "theirs" in status(api, repo_id)["remote_branches"]
    act(api, repo_id, "switch", name="theirs")
    found = status(api, repo_id)
    assert (found["branch"], found["upstream"]) == ("theirs", "origin/theirs")


def test_suggest_a_commit_message(repo, monkeypatch):
    api, repo_id = setup(repo)
    response = api.post(f"/api/repos/{repo_id}/git/suggest-message", json={})
    assert response.status_code == 409
    (repo / "app.py").write_text("def hello():\n    return 'hello'\n")
    act(api, repo_id, "stage")
    prompts = []
    monkeypatch.setattr(explain, "_run", lambda prompt: prompts.append(prompt) or '{"message": "Say hello in full"}')
    response = api.post(f"/api/repos/{repo_id}/git/suggest-message", json={})
    assert response.json() == {"message": "Say hello in full"}
    assert "return 'hello'" in prompts[0]


def test_unknown_action_and_one_without_git(repo, tmp_path):
    api, repo_id = setup(repo)
    assert "no git action" in refused(api, repo_id, "rm-rf")
    folder = tmp_path / "plain"
    folder.mkdir()
    added = api.post("/api/repos", json={"path": str(folder)}).json()
    assert api.get(f"/api/repos/{added['id']}/git").status_code == 409
