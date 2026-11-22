import io
import json
import sqlite3
import subprocess

from conftest import connect_claude, git
from looma.capture.install import claude_settings_path, disconnect, init, record_command
from looma.capture.record import run_hook
from looma.store import connect
from looma.store.db import MIGRATIONS


def send(event, repo, **payload):
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": event, **payload}
    assert run_hook(event, io.StringIO(json.dumps(payload))) == 0


def edit(repo, path):
    send("pre-tool-use", repo, tool_name="Edit", tool_input={"file_path": str(repo / path)})


def user_settings():
    return json.loads(claude_settings_path().read_text())


def test_init_installs_hooks_once(repo, tmp_path):
    connect_claude(repo)
    second = tmp_path / "second"
    second.mkdir()
    git(second, "init", "-q")
    connect_claude(second)

    hooks = user_settings()["hooks"]
    assert {e: len(groups) for e, groups in hooks.items()} == {
        "UserPromptSubmit": 1, "PreToolUse": 1, "PostToolUse": 1, "Stop": 1, "SessionEnd": 1,
    }
    assert hooks["PreToolUse"][0]["matcher"] == "Edit|MultiEdit|Write|NotebookEdit"
    hook = (repo / ".git" / "hooks" / "post-commit").read_text()
    assert hook.count("looma record post-commit") == 1
    assert connect().execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2
    assert git(repo, "status", "--porcelain") == ""  # nothing added to the repo


def test_init_keeps_other_hooks_and_moves_old_repo_hooks(repo):
    claude_settings_path().parent.mkdir()
    claude_settings_path().write_text(json.dumps({
        "model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify"}]}]},
    }))
    old = repo / ".claude" / "settings.local.json"
    old.parent.mkdir()
    old.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": record_command("stop")}]}]},
    }))
    existing = repo / ".git" / "hooks" / "post-commit"
    existing.write_text("#!/bin/sh\necho mine\n")

    connect_claude(repo)

    data = user_settings()
    assert data["model"] == "opus"
    assert [g["hooks"][0]["command"] for g in data["hooks"]["Stop"]] == ["notify", record_command("stop")]
    assert json.loads(old.read_text()) == {"permissions": {"allow": ["Bash(ls)"]}}
    assert existing.read_text().startswith("#!/bin/sh\necho mine\n")


def test_session_started_in_parent_folder_records_each_repo(tmp_path):
    parent = tmp_path / "Sankatmochan"
    ui, be = parent / "UI", parent / "BE"
    for path in (ui, be):
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "master")
        (path / "index.ts").write_text("export const a = 1\n")
        connect_claude(path)

    send("user-prompt-submit", parent, prompt="bump both")
    send("pre-tool-use", parent, tool_name="Edit", tool_input={"file_path": "UI/index.ts"})
    send("pre-tool-use", parent, tool_name="Edit", tool_input={"file_path": str(be / "index.ts")})
    (ui / "index.ts").write_text("export const a = 2\n")
    (be / "index.ts").write_text("export const a = 3\n")
    send("stop", parent)

    conn = connect()
    turn = conn.execute("SELECT prompt, branch, ended_at FROM turns").fetchone()
    assert (turn["prompt"], turn["branch"]) == ("bump both", "master")
    rows = conn.execute(
        "SELECT r.name, c.file_path FROM changes c JOIN repos r ON r.id = c.repo_id ORDER BY r.name"
    ).fetchall()
    assert [(r["name"], r["file_path"]) for r in rows] == [("BE", "index.ts"), ("UI", "index.ts")]


def test_session_outside_tracked_repos_leaves_no_trace(repo, tmp_path):
    connect_claude(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    send("user-prompt-submit", elsewhere, prompt="private question", session_id="other")
    send("pre-tool-use", elsewhere, tool_name="Write", tool_input={"file_path": "x.py"}, session_id="other")
    send("stop", elsewhere, session_id="other")

    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_upgrades_a_version_1_database(looma_home, repo):
    looma_home.mkdir()
    old = sqlite3.connect(looma_home / "looma.db")
    old.executescript(MIGRATIONS[0] + """
        PRAGMA user_version=1;
        INSERT INTO repos VALUES (1, '/r', 'r', 't');
        INSERT INTO sessions VALUES ('s', 1, 'claude-code', 't');
        INSERT INTO turns VALUES (1, 's', 'main', 'old prompt', 't', NULL);
        INSERT INTO turn_files VALUES (1, 'a.py', 'x');
    """)
    old.close()

    conn = connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert tuple(conn.execute("SELECT turn_id, repo_id, file_path FROM turn_files").fetchone()) == (1, 1, "a.py")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT claude FROM repos").fetchone()["claude"] == 1  # it had hooks
    conn.execute("INSERT INTO sessions (id, repo_id, started_at) VALUES ('new', NULL, 't')")


def test_turn_diff_excludes_manual_edits(repo):
    connect_claude(repo)
    (repo / "notes.md").write_text("draft\n")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-q", "-m", "notes")
    (repo / "app.py").write_text("def hello():\n    return 'hi'\n# my own uncommitted tweak\n")

    send("user-prompt-submit", repo, prompt="make hello louder")
    edit(repo, "app.py")
    (repo / "app.py").write_text("def hello():\n    return 'HI'\n# my own uncommitted tweak\n")
    (repo / "notes.md").write_text("draft\nedited by hand\n")  # never touched by the AI
    send("stop", repo)

    conn = connect()
    turn = conn.execute("SELECT * FROM turns").fetchone()
    assert turn["prompt"] == "make hello louder"
    assert turn["branch"] == "main"
    assert turn["ended_at"] is not None
    changes = conn.execute("SELECT * FROM changes WHERE turn_id IS NOT NULL ORDER BY file_path").fetchall()
    # An edit made during the prompt counts too, marked as not seen through Edit or Write.
    assert [(c["file_path"], c["source"], c["via"]) for c in changes] == [("app.py", "ai", "edit"),
                                                                          ("notes.md", "ai", "watch")]
    diff = changes[0]["diff"]
    assert "-    return 'hi'\n+    return 'HI'\n" in diff
    # Made before the prompt started, so not part of it.
    assert "+# my own uncommitted tweak" not in diff


def test_commit_links_ai_change_and_records_human_change(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="add a goodbye")
    send("pre-tool-use", repo, tool_name="Write", tool_input={"file_path": "bye.py"})
    (repo / "bye.py").write_text("def bye():\n    return 'bye'\n")
    send("stop", repo)
    (repo / "README.md").write_text("# Project\n")

    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "goodbye and readme")  # fires the installed post-commit hook

    conn = connect()
    sha = git(repo, "rev-parse", "HEAD").strip()
    commit = conn.execute("SELECT * FROM commits").fetchone()
    assert (commit["sha"], commit["message"], commit["branch"]) == (sha, "goodbye and readme", "main")
    rows = conn.execute("SELECT file_path, source, commit_sha, diff FROM changes ORDER BY file_path").fetchall()
    assert [(r["file_path"], r["source"], r["commit_sha"]) for r in rows] == [
        ("README.md", "human", sha), ("bye.py", "ai", sha),
    ]
    assert rows[1]["diff"].startswith("--- /dev/null\n+++ b/bye.py\n")


def test_new_prompt_closes_turn_whose_stop_never_fired(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="first")
    edit(repo, "app.py")
    (repo / "app.py").write_text("changed\n")
    send("user-prompt-submit", repo, prompt="second")

    conn = connect()
    turns = conn.execute("SELECT prompt, ended_at FROM turns ORDER BY id").fetchall()
    assert [(t["prompt"], t["ended_at"] is None) for t in turns] == [("first", False), ("second", True)]
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1


def test_ignores_internal_runs_unregistered_repos_and_lockfiles(repo, monkeypatch, tmp_path):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="update deps")
    edit(repo, "package-lock.json")
    send("pre-tool-use", repo, tool_name="Edit", tool_input={"file_path": str(tmp_path / "outside.py")})
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    send("user-prompt-submit", other, prompt="not tracked", session_id="s2")
    send("stop", other, session_id="s2")
    monkeypatch.setenv("LOOMA_INTERNAL", "1")
    send("user-prompt-submit", repo, prompt="summary run", session_id="s3")

    conn = connect()
    assert [r["prompt"] for r in conn.execute("SELECT prompt FROM turns")] == ["update deps"]
    assert conn.execute("SELECT COUNT(*) FROM turn_files").fetchone()[0] == 0


def test_bad_payload_never_fails_the_hook(repo, looma_home):
    connect_claude(repo)
    assert run_hook("stop", io.StringIO("not json")) == 0
    assert "JSONDecodeError" in (looma_home / "errors.log").read_text()


def test_installed_command_runs_end_to_end(repo):
    connect_claude(repo)
    payload = json.dumps({"session_id": "e2e", "cwd": str(repo), "prompt": "via the real command"})
    result = subprocess.run(record_command("user-prompt-submit"), shell=True, input=payload,
                            capture_output=True, text=True, cwd=repo)
    assert result.returncode == 0
    assert result.stdout == ""  # Claude Code would add any output to the chat
    assert connect().execute("SELECT prompt FROM turns").fetchone()["prompt"] == "via the real command"


def test_commit_with_ai_coauthor_counts_as_ai(repo, looma_home):
    connect_claude(repo)
    (repo / "app.py").write_text("def hello():\n    return 'hey'\n")
    git(repo, "commit", "-q", "-am", "Tweak hello\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    (repo / "app.py").write_text("def hello():\n    return 'yo'\n")
    git(repo, "commit", "-q", "-am", "My own tweak")

    rows = connect().execute(
        "SELECT m.message, c.source, c.turn_id FROM changes c JOIN commits m ON m.sha = c.commit_sha ORDER BY c.id"
    ).fetchall()
    assert [(r["message"].splitlines()[0], r["source"], r["turn_id"]) for r in rows] == [
        ("Tweak hello", "ai", None), ("My own tweak", "human", None),
    ]
    assert "post-commit" in (looma_home / "hooks.log").read_text()


def test_hook_log_names_the_tool_without_the_prompt(repo, looma_home):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="secret plan")
    send("pre-tool-use", repo, tool_name="Bash", tool_input={"command": "echo hi > a.txt"})
    log = (looma_home / "hooks.log").read_text()
    assert "tool=Bash" in log
    assert "secret plan" not in log


def test_prompt_file_becomes_the_commits_turn(repo):
    connect_claude(repo)
    assert "/.looma/" in (repo / ".git" / "info" / "exclude").read_text()
    (repo / ".looma").mkdir()
    (repo / ".looma" / "prompt.md").write_text("make hello shout\n---\nand add a docstring\n")
    (repo / "app.py").write_text('def hello():\n    """Greets."""\n    return "HI"\n')
    git(repo, "add", "-A")
    assert "prompt.md" not in git(repo, "status", "--porcelain")  # never committed
    git(repo, "commit", "-q", "-m", "Shout")

    conn = connect()
    turn = conn.execute("SELECT t.prompt, s.tool FROM turns t JOIN sessions s ON s.id = t.session_id").fetchone()
    assert (turn["prompt"], turn["tool"]) == ("make hello shout\n---\nand add a docstring", "git")
    change = conn.execute("SELECT file_path, source, turn_id FROM changes").fetchone()
    assert (change["file_path"], change["source"]) == ("app.py", "ai")
    assert change["turn_id"] is not None
    assert not (repo / ".looma" / "prompt.md").exists()

    (repo / "app.py").write_text("x = 1\n")
    git(repo, "commit", "-q", "-am", "next")  # no prompt file this time
    assert conn.execute("SELECT source FROM changes ORDER BY id DESC").fetchone()["source"] == "human"


def test_init_alone_installs_nothing(repo):
    init(repo)
    assert not claude_settings_path().exists()
    assert not (repo / ".git" / "hooks" / "post-commit").exists()
    assert connect().execute("SELECT claude FROM repos").fetchone()["claude"] == 0
    send("user-prompt-submit", repo, prompt="not recorded")
    edit(repo, "app.py")
    (repo / "app.py").write_text("changed\n")
    send("stop", repo)
    assert connect().execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_disconnect_removes_hooks_but_keeps_history(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")
    connect_claude(repo)
    connect_claude(other)
    send("user-prompt-submit", repo, prompt="keep me")
    edit(repo, "app.py")
    (repo / "app.py").write_text("changed\n")
    send("stop", repo)

    disconnect(repo)
    assert hook.read_text() == "#!/bin/sh\necho mine\n"
    assert "hooks" in json.loads(claude_settings_path().read_text())  # other repo still connected
    disconnect(other)
    assert "hooks" not in json.loads(claude_settings_path().read_text())
    assert not (other / ".git" / "hooks" / "post-commit").exists()
    assert connect().execute("SELECT prompt FROM turns").fetchone()["prompt"] == "keep me"


def test_a_prompt_still_records_when_its_session_was_deleted_meanwhile(repo):
    # Removing a project detaches its sessions; closing their stale turn then deletes the
    # session, and the next prompt in it must bring the session back, not fail.
    import io
    import json

    from looma.capture.install import remove
    from looma.capture.record import run_hook
    from looma.paths import error_log
    from looma.store import connect

    other = repo.parent / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    connect_claude(repo)
    connect_claude(other)

    def send(event, **payload):
        body = {"session_id": "s1", "cwd": str(repo), "hook_event_name": event, **payload}
        run_hook(event, io.StringIO(json.dumps(body)))

    send("user-prompt-submit", prompt="first")
    [row] = connect().execute("SELECT id FROM repos WHERE path = ?", (str(repo),)).fetchall()
    remove(row["id"])
    send("user-prompt-submit", prompt="second", cwd=str(other))
    assert not error_log().exists() or "IntegrityError" not in error_log().read_text()
    prompts = [r["prompt"] for r in connect().execute("SELECT prompt FROM turns")]
    assert prompts == ["second"]


def turn_changes():
    rows = connect().execute("SELECT file_path, via, diff, commit_sha FROM changes WHERE turn_id IS NOT NULL"
                             " ORDER BY file_path").fetchall()
    return {r["file_path"]: r for r in rows}


def test_files_changed_by_a_shell_command_count_for_the_prompt(repo):
    connect_claude(repo)
    (repo / "old.py").write_text("x = 1\n")
    git(repo, "add", "old.py")
    git(repo, "commit", "-q", "-m", "old")
    (repo / "draft.md").write_text("mine\n")  # untracked before the prompt: not Claude's
    (repo / "app.py").write_text("def hello():\n    return 'hi'\n# mine\n")  # already edited by hand

    send("user-prompt-submit", repo, prompt="tidy up")
    # Claude works through Bash: no Edit or Write hook fires.
    (repo / "app.py").write_text("def hello():\n    return 'HI'\n# mine\n")
    (repo / "new.py").write_text("y = 2\n")
    (repo / "old.py").unlink()
    (repo / "pkg.lock").write_text("ignored\n")
    send("stop", repo)

    changes = turn_changes()
    assert set(changes) == {"app.py", "new.py", "old.py"}
    assert all(c["via"] == "watch" for c in changes.values())
    # The before copy is the file as the prompt found it, hand edits included.
    assert "-    return 'hi'\n+    return 'HI'\n" in changes["app.py"]["diff"]
    assert "+# mine" not in changes["app.py"]["diff"]
    assert changes["new.py"]["diff"].startswith("--- /dev/null")
    assert "+++ /dev/null" in changes["old.py"]["diff"]


def test_a_commit_made_during_the_prompt_is_counted_once(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="add bye and commit")
    (repo / "bye.py").write_text("def bye():\n    return 'bye'\n")
    git(repo, "add", "bye.py")
    git(repo, "commit", "-q", "-m", "Add bye")  # the post-commit hook runs mid-prompt
    send("stop", repo)

    conn = connect()
    rows = conn.execute("SELECT file_path, turn_id, source, commit_sha FROM changes").fetchall()
    sha = git(repo, "rev-parse", "HEAD").strip()
    assert [(r["file_path"], r["turn_id"] is not None, r["commit_sha"]) for r in rows] == [("bye.py", True, sha)]


def test_large_files_and_files_from_another_project_are_left_out(repo, tmp_path):
    from looma.capture.record import MAX_FILE_BYTES

    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    connect_claude(repo)
    connect_claude(other)
    (repo / "big.txt").write_text("a\n" * MAX_FILE_BYTES)  # too large to keep as a before copy
    send("user-prompt-submit", repo, prompt="go")
    (repo / "big.txt").write_text("b\n")
    (other / "theirs.py").write_text("z = 3\n")  # a session elsewhere; this one started in repo
    (repo / "app.py").write_text("def hello():\n    return 'yo'\n")
    send("stop", repo)
    assert set(turn_changes()) == {"app.py"}


def test_a_session_started_elsewhere_is_attributed_by_the_files_it_edits(repo, tmp_path):
    from looma.capture import record

    connect_claude(repo)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    # Started outside the project (another folder, an editor, the desktop app).
    send("user-prompt-submit", elsewhere, prompt="Fix the greeting")
    send("pre-tool-use", elsewhere, tool_name="Edit", tool_input={"file_path": str(repo / "app.py")})
    (repo / "app.py").write_text("def hello():\n    return 'hello'\n")
    # A shell command in the same prompt also changes a file there: watched from the first edit.
    (repo / "notes.md").write_text("made by a script\n")
    send("stop", elsewhere)
    files = {row[0] for row in connect().execute("SELECT file_path FROM changes WHERE source = 'ai'")}
    assert files == {"app.py", "notes.md"}
    assert record.health()["errors"] == 0


def test_hook_failures_are_counted_with_their_reason(repo, looma_home):
    from looma.capture import record

    connect_claude(repo)
    assert run_hook("user-prompt-submit", io.StringIO("{not json")) == 0
    health = record.health()
    assert health["errors"] == 1 and "JSONDecodeError" in health["last_error"]["detail"]
    assert "ERROR JSONDecodeError" in (looma_home / "hooks.log").read_text()
    send("user-prompt-submit", repo, prompt="hi")
    assert "user-prompt-submit" in record.health()["last_call"]


def test_hook_commands_quote_a_windows_path_with_a_space_for_sh_and_cmd(monkeypatch):
    from looma.capture import install

    monkeypatch.setattr(install.os, "name", "nt")
    assert install._quote("C:/Users/Jo Smith/.local/pipx/venvs/looma/Scripts/python.exe") == \
        '"C:/Users/Jo Smith/.local/pipx/venvs/looma/Scripts/python.exe"'
    assert install._quote("C:/Users/jo/python.exe") == "C:/Users/jo/python.exe"


def test_a_folder_of_projects_is_remembered_and_offered_without_adding_any(tmp_path, capsys):
    from fastapi.testclient import TestClient

    from looma import cli
    from looma.api.app import create_app

    work = tmp_path / "work"
    for name in ("alpha", "beta", "node_modules/dep"):
        (work / name).mkdir(parents=True)
        git(work / name, "init", "-q")
    assert cli.main(["init", str(work)]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out and "dep" not in out
    assert connect().execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0  # nothing added
    assert not (work / "alpha" / ".git" / "hooks" / "post-commit").exists()

    api = TestClient(create_app(), base_url="http://127.0.0.1")
    assert [c["name"] for c in api.get("/api/repos/candidates").json()] == ["alpha", "beta"]
    assert api.post("/api/repos", json={"path": str(work / "alpha")}).status_code == 201
    assert [c["name"] for c in api.get("/api/repos/candidates").json()] == ["beta"]
    # Typing the folder itself says which projects are inside.
    folder = api.post("/api/repos", json={"path": str(work)})
    assert folder.status_code == 400 and "beta" in folder.json()["detail"]


def test_health_names_hooks_that_cannot_record_and_repair_fixes_them(repo):
    from fastapi.testclient import TestClient

    from looma.api.app import create_app
    from looma.capture import record

    connect_claude(repo)
    assert record.health()["problems"] == []
    # A reinstall moved Python: the hooks still name the old one, which fails before Looma runs.
    settings = user_settings()
    settings["hooks"]["Stop"][0]["hooks"][0]["command"] = "/gone/python -I -m looma record stop"
    del settings["hooks"]["SessionEnd"]
    claude_settings_path().write_text(json.dumps(settings))
    [problem] = record.health()["problems"]
    assert "missing Looma's SessionEnd hook" in problem

    settings["hooks"]["SessionEnd"] = [{"hooks": [{"type": "command", "command": record_command("session-end")}]}]
    claude_settings_path().write_text(json.dumps(settings))
    [problem] = record.health()["problems"]
    assert "older Looma" in problem and "Stop" in problem

    api = TestClient(create_app(), base_url="http://127.0.0.1")
    repaired = api.post("/api/recording/repair", json={})
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["problems"] == []


def test_prompts_left_open_by_a_dead_session_are_closed_with_what_was_seen(repo):
    from looma.capture import record

    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="long ago")
    conn = connect()
    with conn:
        conn.execute("UPDATE turns SET started_at = '2020-01-01T00:00:00+00:00'")
    edit(repo, "app.py")
    (repo / "app.py").write_text("def hello():\n    return 'late'\n")
    health = record.health()
    assert health["stuck"] == 1 and health["problems"] == [] and not health["repairable"]
    row = connect().execute("SELECT t.ended_at, c.file_path FROM turns t JOIN changes c ON c.turn_id = t.id").fetchone()
    assert row["ended_at"] and row["file_path"] == "app.py"
    assert record.health()["stuck"] == 0
