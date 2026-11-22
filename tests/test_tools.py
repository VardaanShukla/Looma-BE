"""Recording Codex and Antigravity, which hook in like Claude Code with payloads of their own."""

import io
import json

from conftest import git
from fastapi.testclient import TestClient

from looma.api.app import create_app
from looma.capture.install import (antigravity_hooks_path, claude_settings_path, codex_hooks_path,
                                     connect_tool, record_command, remove_all_hooks)
from looma.capture.record import run_hook
from looma.store import connect


def codex(event, repo, **payload):
    payload = {"session_id": "codex-1", "cwd": str(repo), "hook_event_name": event, "model": "gpt", **payload}
    assert run_hook(event, io.StringIO(json.dumps(payload)), tool="codex") == 0


def antigravity(event, repo, transcript, **payload):
    payload = {"conversationId": "conv-1", "workspacePaths": [str(repo)], "transcriptPath": str(transcript),
               "modelName": "auto", **payload}
    out = io.StringIO()
    assert run_hook(event, io.StringIO(json.dumps(payload)), tool="antigravity", stdout=out) == 0
    return json.loads(out.getvalue())


def test_picking_codex_then_antigravity_moves_the_hooks(repo):
    api = TestClient(create_app(), base_url="http://127.0.0.1")
    added = api.post("/api/repos", json={"path": str(repo), "ai": "codex"})
    assert added.status_code == 201 and added.json()["ai"] == "codex" and added.json()["claude"] is True
    hooks = json.loads(codex_hooks_path().read_text())["hooks"]
    assert sorted(hooks) == ["PreToolUse", "Stop", "UserPromptSubmit"]
    assert hooks["PreToolUse"][0]["matcher"] == "apply_patch"
    assert hooks["Stop"][0]["hooks"][0]["command"].endswith("record stop --tool codex")
    assert not claude_settings_path().exists()
    steps = {s["id"]: s for s in api.get(f"/api/repos/{added.json()['id']}/setup").json()["steps"]}
    assert steps["codex-hooks"]["done"] is True and steps["git-hook"]["done"] is True

    repo_id = added.json()["id"]
    assert api.put(f"/api/repos/{repo_id}/ai", json={"ai": "antigravity"}).json()["ai"] == "antigravity"
    assert not codex_hooks_path().exists()  # no project records Codex now
    config = json.loads(antigravity_hooks_path().read_text())
    assert sorted(config["looma"]) == ["PreInvocation", "Stop"]
    assert config["looma"]["Stop"][0]["command"].endswith("record stop --tool antigravity || echo {}")
    steps = {s["id"]: s for s in api.get(f"/api/repos/{repo_id}/setup").json()["steps"]}
    assert steps["antigravity-hooks"]["done"] is True

    assert api.put(f"/api/repos/{repo_id}/ai", json={"ai": None}).json()["ai"] is None
    assert not antigravity_hooks_path().exists()


def test_other_hooks_in_the_tools_files_are_kept(repo):
    codex_hooks_path().parent.mkdir()
    codex_hooks_path().write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "ding"}]}]}}))
    antigravity_hooks_path().parent.mkdir()
    antigravity_hooks_path().write_text(json.dumps({"lint": {"PostToolUse": []}}))
    connect_tool(repo, "codex")
    connect_tool(repo, "antigravity")
    assert json.loads(codex_hooks_path().read_text()) == {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "ding"}]}]}}
    assert set(json.loads(antigravity_hooks_path().read_text())) == {"lint", "looma"}


def test_codex_request_and_patch_are_recorded(repo, looma_home):
    connect_tool(repo, "codex")
    (repo / "notes.md").write_text("mine\n")
    codex("user-prompt-submit", repo, turn_id="t1", prompt="make hello louder")
    patch = ("*** Begin Patch\n*** Update File: app.py\n@@\n-    return 'hi'\n+    return 'HI'\n"
             "*** Add File: shout.py\n+LOUD = True\n*** End Patch\n")
    codex("pre-tool-use", repo, turn_id="t1", tool_name="apply_patch", tool_use_id="u1",
          tool_input={"command": patch})
    (repo / "app.py").write_text("def hello():\n    return 'HI'\n")
    (repo / "shout.py").write_text("LOUD = True\n")
    codex("stop", repo, turn_id="t1", stop_hook_active=False, last_assistant_message="Done.")

    conn = connect()
    turn = conn.execute("SELECT t.prompt, t.ended_at, s.tool FROM turns t JOIN sessions s ON s.id = t.session_id").fetchone()
    assert (turn["prompt"], turn["tool"]) == ("make hello louder", "codex") and turn["ended_at"]
    changes = conn.execute("SELECT file_path, via FROM changes WHERE turn_id IS NOT NULL ORDER BY file_path").fetchall()
    assert [(c["file_path"], c["via"]) for c in changes] == [("app.py", "edit"), ("shout.py", "edit")]
    assert not (looma_home / "errors.log").exists()


def test_antigravity_request_is_recorded_from_its_transcript(repo, tmp_path, looma_home):
    connect_tool(repo, "antigravity")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(line) for line in [
        {"type": "USER_INPUT", "userInput": {"userResponse": "an older request"}},
        {"type": "PLANNER_RESPONSE", "plannerResponse": {"response": "ok"}},
        {"type": "USER_INPUT", "userInput": {"userResponse": "make hello louder"}},
    ]) + "\n")

    assert antigravity("user-prompt-submit", repo, transcript, invocationNum=0, initialNumSteps=3) == {}
    (repo / "app.py").write_text("def hello():\n    return 'HI'\n")
    # Later model calls of the same request don't start another.
    assert antigravity("user-prompt-submit", repo, transcript, invocationNum=1, initialNumSteps=3) == {}
    answer = antigravity("stop", repo, transcript, executionNum=1, terminationReason="model_stop", fullyIdle=True)
    assert answer["decision"] != "continue"

    conn = connect()
    turns = conn.execute("SELECT t.prompt, t.ended_at, s.tool FROM turns t JOIN sessions s ON s.id = t.session_id").fetchall()
    assert [(t["prompt"], t["tool"]) for t in turns] == [("make hello louder", "antigravity")] and turns[0]["ended_at"]
    changes = conn.execute("SELECT file_path, diff FROM changes WHERE turn_id IS NOT NULL").fetchall()
    assert [c["file_path"] for c in changes] == ["app.py"] and "+    return 'HI'" in changes[0]["diff"]
    assert not (looma_home / "errors.log").exists()


def test_antigravity_always_gets_its_json_answer(tmp_path):
    out = io.StringIO()
    assert run_hook("stop", io.StringIO("not json"), tool="antigravity", stdout=out) == 0
    assert json.loads(out.getvalue())["decision"] != "continue"


def test_hooks_name_their_tool():
    assert record_command("stop", "codex").endswith("record stop --tool codex")
    assert record_command("stop").endswith("record stop")


def test_remove_all_hooks_takes_looma_out_of_every_project_and_tool(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    hook = other / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")
    connect_tool(repo, "claude")
    connect_tool(other.resolve(), "codex")

    assert set(remove_all_hooks()) == {repo, other.resolve()}
    assert not (repo / ".git" / "hooks" / "post-commit").exists()
    assert hook.read_text() == "#!/bin/sh\necho mine\n"  # the project's own hook stays
    assert "hooks" not in json.loads(claude_settings_path().read_text())
    assert not codex_hooks_path().exists()
