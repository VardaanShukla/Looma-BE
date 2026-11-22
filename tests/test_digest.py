import io
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from conftest import connect_claude, git
from looma.api.app import create_app
from looma.capture.record import run_hook


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def send(event, repo, **payload):
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": event, **payload}
    assert run_hook(event, io.StringIO(json.dumps(payload))) == 0


def claude_edits(repo, prompt, files):
    send("user-prompt-submit", repo, prompt=prompt)
    for path, content in files.items():
        send("pre-tool-use", repo, tool_name="Edit", tool_input={"file_path": str(repo / path)})
        if content is None:
            (repo / path).unlink()
        else:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(content)
    send("stop", repo)


def test_digest_lists_claudes_prompts_with_functions_and_flags(repo):
    (repo / "auth").mkdir()
    (repo / "auth" / "login.py").write_text("def check(user, password):\n    return user.password == password\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "def test_hello():\n    assert True\n\n\ndef test_bye():\n    assert True\n")
    (repo / "app.py").write_text("def hello():\n    return 'hi'\n\n\ndef bye():\n    return 'bye'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "setup")
    connect_claude(repo)

    claude_edits(repo, "Say hello louder", {
        "app.py": "def hello():\n    return 'HI'\n\n\ndef bye():\n    return 'bye'\n\n\ndef wave():\n    return 'o/'\n",
    })
    git(repo, "commit", "-q", "-am", "Louder hello")  # the hook links Claude's change to it
    claude_edits(repo, "Make login faster and drop the slow test", {
        "auth/login.py": "def check(user, password):\n    return hmac.compare_digest(user.hash, hash(password))\n",
        "tests/test_app.py": "def test_hello():\n    assert True\n",
        "config.py": "API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n",
        "auth/keys.py": "LOGIN_PASSWORD = 'hunter2hunter2'\n",
    })
    # A change of your own, committed.
    (repo / "README.md").write_text("Hello\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "Readme")

    result = client().get("/api/digest").json()
    assert result["date"] == date.today().isoformat()
    assert result["claude"] == {"prompts": 2, "files": 5, "functions": 4, "additions": 8, "deletions": 6,
                                     "risky": 1, "uncommitted": 1, "undone": 0, "watched_files": 0}
    assert result["you"] == {"files": 1, "additions": 1, "deletions": 0, "commits": 1}

    newest, older = result["changes"]
    assert newest["prompt"] == "Make login faster and drop the slow test" and newest["commit"] is None
    assert newest["risky"] is True
    assert {f["kind"] for f in newest["flags"]} == {"auth", "tests", "secret"}
    by_path = {f["path"]: f for f in newest["files"]}
    assert by_path["auth/login.py"]["functions"] == [{"name": "check", "change": "modified"}]
    assert by_path["tests/test_app.py"]["flags"][0]["reason"] == "1 test deleted"
    # Blank lines between functions don't count as changing the one above them.
    assert by_path["tests/test_app.py"]["functions"] == [{"name": "test_bye", "change": "deleted"}]
    assert by_path["config.py"]["status"] == "added"
    [secret] = by_path["config.py"]["flags"]
    assert secret["line"] == "API_KEY = 'sk" + "\u2022" * 8 + "'"
    # A line shown for another reason is masked too.
    assert [f["line"] for f in by_path["auth/keys.py"]["flags"]] == ["LOGIN_PASSWORD = 'hu" + "\u2022" * 8 + "'"] * 2
    # Flagged for its folder, with the changed line that mentions a password.
    assert by_path["auth/login.py"]["flags"][0]["line"].startswith("return hmac.compare_digest")

    assert older["prompt"] == "Say hello louder" and older["commit"]["message"] == "Louder hello"
    assert older["risky"] is False and older["flags"] == []
    assert {(f["name"], f["change"]) for f in older["files"][0]["functions"]} == {("hello", "modified"),
                                                                               ("wave", "added")}


def test_other_days_and_bad_days(repo):
    connect_claude(repo)
    claude_edits(repo, "tweak", {"app.py": "def hello():\n    return 'yo'\n"})
    api = client()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    empty = api.get(f"/api/digest?day={yesterday}").json()
    assert empty["changes"] == [] and empty["claude"]["prompts"] == 0
    assert api.get("/api/digest?day=18-10-2026").status_code == 400


def test_ai_coauthored_commit_without_a_prompt_uses_its_message(repo):
    connect_claude(repo)
    (repo / "db").mkdir()
    (repo / "db" / "migration_002.sql").write_text("ALTER TABLE users ADD COLUMN age INTEGER;\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Add age\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    [change] = client().get("/api/digest").json()["changes"]
    assert change["prompt"] == "Add age" and change["prompt_recorded"] is False
    assert [f["kind"] for f in change["flags"]] == ["database"]


def test_undo_and_put_back_one_change(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n", "notes.md": "new file\n"})
    claude_edits(repo, "Add bye", {"bye.py": "def bye():\n    return 'bye'\n"})
    api = client()
    shout = next(c for c in api.get("/api/digest").json()["changes"] if c["prompt"] == "Shout")
    assert shout["undone_at"] is None

    done = api.post("/api/digest/undo", json={"change": shout["id"]})
    assert done.status_code == 200 and done.json()["undone_at"]
    # Only that prompt's edits come off; the later one stays.
    assert (repo / "app.py").read_text() == "def hello():\n    return 'hi'\n"
    assert not (repo / "notes.md").exists() and (repo / "bye.py").exists()
    assert api.post("/api/digest/undo", json={"change": shout["id"]}).status_code == 409
    listed = next(c for c in api.get("/api/digest").json()["changes"] if c["id"] == shout["id"])
    assert listed["undone_at"] == done.json()["undone_at"]

    assert api.post("/api/digest/put-back", json={"change": shout["id"]}).status_code == 200
    assert (repo / "app.py").read_text() == "def hello():\n    return 'HI'\n"
    assert (repo / "notes.md").read_text() == "new file\n"
    assert api.post("/api/digest/undo", json={"change": "turn-999-1"}).status_code == 404


def test_undo_refuses_when_a_file_changed_since(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n", "other.py": "x = 1\n"})
    (repo / "app.py").write_text("def hello():\n    return 'Hey'\n")  # your own edit afterwards
    api = client()
    [change] = api.get("/api/digest").json()["changes"]
    refused = api.post("/api/digest/undo", json={"change": change["id"]})
    assert refused.status_code == 409 and "app.py" in refused.json()["detail"]
    # All or nothing: the file that would have fitted is untouched too.
    assert (repo / "other.py").read_text() == "x = 1\n"
    assert (repo / "app.py").read_text() == "def hello():\n    return 'Hey'\n"


def test_undoing_a_committed_change_leaves_the_undo_uncommitted(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n"})
    git(repo, "commit", "-q", "-am", "Shout")
    api = client()
    [change] = api.get("/api/digest").json()["changes"]
    assert change["commit"]["message"] == "Shout"
    assert api.post("/api/digest/undo", json={"change": change["id"]}).status_code == 200
    assert (repo / "app.py").read_text() == "def hello():\n    return 'hi'\n"
    assert git(repo, "log", "-1", "--format=%s").strip() == "Shout"  # nothing committed
    assert git(repo, "status", "--porcelain").strip() == "M app.py"
    # Removing the project forgets the undo with everything else.
    [listed] = api.get("/api/repos").json()
    assert api.request("DELETE", f"/api/repos/{listed['id']}", json={}).status_code == 204
    assert api.get("/api/digest").json()["changes"] == []


CLAUDE = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>"


def pushed_from_the_cloud(repo, tmp_path, branch, files, message):
    """A cloud session's work: committed in another clone and pushed, then fetched here."""
    remote = tmp_path / "remote.git"
    if not remote.exists():
        git(tmp_path, "init", "-q", "--bare", str(remote))
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "origin", "main")
    cloud = tmp_path / f"cloud-{branch}"
    git(tmp_path, "clone", "-q", str(remote), str(cloud))
    git(cloud, "config", "user.email", "claude@example.com")
    git(cloud, "config", "user.name", "Claude")
    git(cloud, "checkout", "-q", "-b", branch)
    for path, content in files.items():
        (cloud / path).write_text(content)
    git(cloud, "add", ".")
    git(cloud, "commit", "-q", "-m", message)
    git(cloud, "push", "-q", "origin", branch)
    git(repo, "fetch", "-q", "origin")
    return git(cloud, "rev-parse", "HEAD").strip()


def test_a_cloud_sessions_pushed_commit_shows_as_its_own_change(repo, tmp_path):
    from looma.capture.install import init
    init(repo)  # not connected to Claude: no hooks at all
    sha = pushed_from_the_cloud(repo, tmp_path, "claude/tree", {"tree.py": "def grow():\n    return 1\n"},
                                "Grow the tree" + CLAUDE)
    api = client()
    [change] = api.get("/api/digest").json()["changes"]
    assert change["origin"] == "cloud" and change["prompt"] == "Grow the tree"
    assert change["prompt_recorded"] is False and change["branch"] == "claude/tree"
    assert change["commit"]["sha"] == sha
    assert [(f["path"], f["status"]) for f in change["files"]] == [("tree.py", "added")]
    # Looking again doesn't count it twice.
    assert len(api.get("/api/digest").json()["changes"]) == 1
    # Not on the checked-out branch, so undo explains instead of failing on the files.
    refused = api.post("/api/digest/undo", json={"change": change["id"]})
    assert refused.status_code == 409 and "isn't on the branch" in refused.json()["detail"]


def test_commits_without_a_claude_trailer_or_copies_are_not_counted(repo, tmp_path):
    from looma.capture.install import init
    init(repo)
    pushed_from_the_cloud(repo, tmp_path, "mine", {"a.py": "x = 1\n"}, "My own work")
    pushed_from_the_cloud(repo, tmp_path, "claude", {"b.py": "y = 2\n"}, "Claude's work" + CLAUDE)
    # The same commit rebased onto another branch keeps its author time and message.
    git(repo, "checkout", "-q", "-b", "copy")
    git(repo, "cherry-pick", "origin/claude")
    changes = client().get("/api/digest").json()["changes"]
    assert [c["prompt"] for c in changes] == ["Claude's work"]


def test_a_hook_recorded_commit_is_not_counted_again(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n"})
    git(repo, "commit", "-q", "-am", "Shout" + CLAUDE)
    [change] = client().get("/api/digest").json()["changes"]
    assert change["origin"] == "prompt" and change["prompt"] == "Shout"
    assert change["commit"]["message"] == "Shout"


def test_a_commit_the_post_commit_hook_missed_links_to_its_prompt(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n"})
    # Committed with hooks skipped, so only the scan sees the commit.
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-am", "Shout" + CLAUDE)
    [change] = client().get("/api/digest").json()["changes"]
    assert change["origin"] == "prompt" and change["commit"]["message"] == "Shout"


def test_files_changed_without_edit_or_write_are_marked(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="Shout and add notes")
    send("pre-tool-use", repo, tool_name="Edit", tool_input={"file_path": str(repo / "app.py")})
    (repo / "app.py").write_text("def hello():\n    return 'HI'\n")
    (repo / "notes.md").write_text("written by a shell command\n")
    send("stop", repo)
    [change] = client().get("/api/digest").json()["changes"]
    assert {f["path"]: f["watched"] for f in change["files"]} == {"app.py": False, "notes.md": True}


def test_words_in_text_and_comments_are_not_payment_or_sign_in_code(repo):
    connect_claude(repo)
    claude_edits(repo, "Explain the digest", {
        "help.ts": 'const GUIDE = "Changes touching sign-in, payments or the database come first.";\n'
                   "// remember the login page\n",
        "README.md": "Payments and passwords are flagged.\n",
        "billing.ts": "const total = stripe.charge(amount);\n",
    })
    [change] = client().get("/api/digest").json()["changes"]
    flags = {f["path"]: [flag["kind"] for flag in f["flags"]] for f in change["files"]}
    assert flags == {"billing.ts": ["payments"], "help.ts": [], "README.md": []}


# The numbers the dashboard shows, checked against git itself.


def numstat(repo):
    """git's own count of lines added and removed in the working tree, new files included."""
    git(repo, "add", "-N", ".")
    out = git(repo, "diff", "--numstat", "HEAD")
    git(repo, "reset", "-q")
    rows = [line.split("\t") for line in out.splitlines()]
    return {path: (int(add), int(rm)) for add, rm, path in rows}


def test_digest_line_counts_match_git_exactly(repo):
    connect_claude(repo)
    (repo / "keep.txt").write_text("one\ntwo\nthree\n")
    (repo / "gone.py").write_text("a = 1\nb = 2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "more")
    claude_edits(repo, "Many kinds of change", {
        "app.py": "def hello():\n    return 'hello'\n\n\ndef bye():\n    return 'bye'",  # no final newline
        "keep.txt": "one\n2\nthree\nfour\n",
        "gone.py": None,
        "pkg/new.py": "x = 1\ny = 2\nz = 3\n",
        "empty.txt": "",
    })
    truth = numstat(repo)
    digest = client().get("/api/digest").json()
    [change] = digest["changes"]
    files = {f["path"]: (f["additions"], f["deletions"]) for f in change["files"]}
    assert files == {path: counts for path, counts in truth.items() if counts != (0, 0)}
    assert digest["claude"]["additions"] == sum(a for a, _ in truth.values())
    assert digest["claude"]["deletions"] == sum(d for _, d in truth.values())
    assert digest["claude"]["files"] == len(files)
    assert digest["claude"]["prompts"] == 1


def test_one_prompt_across_two_projects_is_one_prompt(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    connect_claude(repo)
    connect_claude(other)
    send("user-prompt-submit", tmp_path, prompt="touch both")
    for path in (repo / "app.py", other / "b.py"):
        send("pre-tool-use", tmp_path, tool_name="Write", tool_input={"file_path": str(path)})
        path.write_text("v = 2\n")
    send("stop", tmp_path)
    claude = client().get("/api/digest").json()["claude"]
    assert claude["prompts"] == 1 and claude["files"] == 2


def test_two_sessions_at_once_do_not_count_each_others_edits(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="slow one", session_id="a")
    send("user-prompt-submit", repo, prompt="quick one", session_id="b")
    send("pre-tool-use", repo, tool_name="Edit", tool_input={"file_path": str(repo / "app.py")}, session_id="b")
    (repo / "app.py").write_text("def hello():\n    return 'from b'\n")
    send("stop", repo, session_id="b")
    send("pre-tool-use", repo, tool_name="Write", tool_input={"file_path": str(repo / "a.py")}, session_id="a")
    (repo / "a.py").write_text("a = 1\n")
    send("stop", repo, session_id="a")

    digest = client().get("/api/digest").json()
    by_prompt = {c["prompt"]: sorted(f["path"] for f in c["files"]) for c in digest["changes"]}
    assert by_prompt == {"quick one": ["app.py"], "slow one": ["a.py"]}
    truth = numstat(repo)
    assert digest["claude"]["additions"] == sum(a for a, _ in truth.values())


def test_an_undone_change_leaves_the_totals(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n"})
    claude_edits(repo, "Add bye", {"bye.py": "def bye():\n    return 'bye'\n"})
    api = client()
    shout = next(c for c in api.get("/api/digest").json()["changes"] if c["prompt"] == "Shout")
    api.post("/api/digest/undo", json={"change": shout["id"]})
    claude = api.get("/api/digest").json()["claude"]
    assert (claude["prompts"], claude["files"], claude["additions"], claude["deletions"]) == (1, 1, 2, 0)
    assert claude["undone"] == 1
    assert claude["additions"] == sum(a for a, _ in numstat(repo).values())


def test_an_interrupted_prompt_keeps_what_its_shell_commands_changed(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="run the formatter")
    (repo / "app.py").write_text("def hello():\n    return \"hi\"\n")  # changed by a Bash command
    send("post-tool-use", repo, tool_name="Bash", tool_input={"command": "black ."})
    # Esc: Claude Code sends no Stop. Later the person edits by hand, then asks again.
    (repo / "mine.py").write_text("by hand\n")
    send("user-prompt-submit", repo, prompt="next thing")
    send("stop", repo)
    by_prompt = {c["prompt"]: [f["path"] for f in c["files"]] for c in client().get("/api/digest").json()["changes"]}
    assert by_prompt["run the formatter"] == ["app.py"]
    assert "mine.py" not in by_prompt["run the formatter"]


def test_ending_the_session_closes_its_prompt(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="quit halfway")
    (repo / "app.py").write_text("def hello():\n    return 'bye'\n")
    send("session-end", repo, reason="prompt_input_exit")
    [change] = client().get("/api/digest").json()["changes"]
    assert change["prompt"] == "quit halfway" and [f["path"] for f in change["files"]] == ["app.py"]


def test_a_prompts_change_opens_with_its_lines_and_each_function_explains(repo, monkeypatch):
    (repo / "app.py").write_text("def hello():\n    return 'hi'\n\n\ndef bye():\n    return 'bye'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "setup")
    connect_claude(repo)
    claude_edits(repo, "Shout and wave", {
        "app.py": "def hello():\n    return 'HI'\n\n\ndef bye():\n    return 'bye'\n\n\ndef wave():\n    return 'o/'\n",
        "style.css": "body { color: red; }\n",
    })
    change_id = client().get("/api/digest").json()["changes"][0]["id"]

    found = client().get("/api/digest/change", params={"id": change_id}).json()
    assert found["prompt"] == "Shout and wave"
    files = {f["path"]: f for f in found["files"]}
    assert files["app.py"]["hunks"]
    units = {u["name"]: u for u in files["app.py"]["units"]}
    assert set(units) == {"hello", "wave"}
    assert "+    return 'HI'" in units["hello"]["diff"] and "o/" not in units["hello"]["diff"]
    assert units["wave"]["change"] == "added" and units["wave"]["explanation"] is None
    assert [u["kind"] for u in files["style.css"]["units"]] == ["file"]

    asked = []
    monkeypatch.setattr("looma.explain._run", lambda prompt: asked.append(prompt) or json.dumps(
        {"summary": "Says hello loudly.", "change": "Now shouts.", "improves": [], "tradeoffs": [], "check": None}))
    answer = client().post("/api/digest/explain", json={"change": change_id, "path": "app.py", "name": "hello"}).json()
    assert answer["summary"] == "Says hello loudly."
    assert "Shout and wave" in asked[0] and "One prompt" in asked[0]
    # Saved, so opening the change again shows it without asking.
    again = client().get("/api/digest/change", params={"id": change_id}).json()
    hello = next(u for f in again["files"] for u in f["units"] if u["name"] == "hello")
    assert hello["explanation"]["summary"] == "Says hello loudly."
    assert client().get("/api/digest/change", params={"id": "turn-999-1"}).status_code == 404


def test_digest_can_show_one_projects_prompts(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    connect_claude(repo)
    connect_claude(other)
    claude_edits(repo, "change this one", {"app.py": "v = 1\n"})
    claude_edits(other, "change the other", {"b.py": "v = 2\n"})
    ids = {r["name"]: r["id"] for r in client().get("/api/repos").json()}
    every = client().get("/api/digest").json()
    assert {c["prompt"] for c in every["changes"]} == {"change this one", "change the other"}
    mine = client().get(f"/api/digest?repo={ids[repo.name]}").json()
    assert [c["prompt"] for c in mine["changes"]] == ["change this one"]
    assert mine["claude"]["prompts"] == 1


def test_built_files_and_minified_lines_are_not_recorded(repo):
    connect_claude(repo)
    claude_edits(repo, "rebuild the ui", {
        "src/pkg/web/assets/index-abc.js": "var a=1;\n",
        "app.min2.js": "x" * 3000 + "\n",
        "app.py": "v = 3\n",
    })
    files = [f["path"] for c in client().get("/api/digest").json()["changes"] for f in c["files"]]
    assert files == ["app.py"]


def test_old_lines_are_cleared_but_the_prompt_and_counts_stay(repo):
    from looma import retention
    from looma.store import connect

    connect_claude(repo)
    claude_edits(repo, "old work", {"app.py": "a = 1\nb = 2\n"})
    conn = connect()
    with conn:
        conn.execute("UPDATE changes SET created_at = '2020-01-01T00:00:00+00:00'")
        conn.execute("INSERT INTO changes (repo_id, file_path, diff, source, created_at)"
                     " SELECT repo_id, 'web/assets/index-x.js', 'big', 'human', created_at FROM changes")
    conn.close()
    before = client().get("/api/digest?day=2020-01-01").json()

    assert retention.prune(10)["cleared"] == 1
    after = client().get("/api/digest?day=2020-01-01").json()
    assert after["changes"][0]["prompt"] == "old work"
    assert after["claude"]["additions"] == before["claude"]["additions"] > 0
    assert [f["path"] for f in after["changes"][0]["files"]] == ["app.py"]
    assert client().post("/api/digest/undo", json={"change": after["changes"][0]["id"]}).status_code == 409
    conn = connect()
    assert conn.execute("SELECT count(*) FROM changes WHERE file_path LIKE 'web/assets/%'").fetchone()[0] == 0
    conn.close()


def test_zero_days_keeps_lines_for_good(repo):
    from looma import retention

    connect_claude(repo)
    claude_edits(repo, "old work", {"app.py": "a = 1\n"})
    assert retention.prune(0)["cleared"] == 0


def test_an_app_chats_note_records_its_change_without_a_commit(repo):
    connect_claude(repo)
    (repo / "app.py").write_text("v = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    (repo / "app.py").write_text("v = 2\n")
    (repo / "new.py").write_text("x = 1\n")
    inbox = repo / ".looma" / "turns"
    inbox.mkdir(parents=True)
    (inbox / "20261031T183000Z.json").write_text(json.dumps({
        "prompt": "bump v and add x",
        "files": [{"path": "app.py", "before": "v = 1\n"}, {"path": "new.py", "before": None},
                  {"path": "../outside.py", "before": None}],
    }))
    (inbox / "broken.json").write_text("{")

    changes = client().get("/api/digest").json()["changes"]
    assert [c["prompt"] for c in changes] == ["bump v and add x"]
    assert sorted(f["path"] for f in changes[0]["files"]) == ["app.py", "new.py"]
    assert changes[0]["tool"] == "Claude"
    assert not (inbox / "20261031T183000Z.json").exists() and (inbox / "broken.json.bad").exists()
    # Read once: loading the page again doesn't record it twice.
    assert len(client().get("/api/digest").json()["changes"]) == 1


def test_an_app_chats_note_keeps_the_time_of_its_request(repo):
    connect_claude(repo)
    (repo / "app.py").write_text("v = 2\n")
    inbox = repo / ".looma" / "turns"
    inbox.mkdir(parents=True)
    (inbox / "a.json").write_text(json.dumps({"prompt": "yesterday's work", "at": "2020-01-01T10:00:00Z",
                                              "files": [{"path": "app.py", "before": "v = 1\n"}]}))
    assert client().get("/api/digest").json()["changes"] == []  # read now, but made in 2020
    old = client().get("/api/digest?day=2020-01-01").json()["changes"]
    assert [c["prompt"] for c in old] == ["yesterday's work"]


def test_a_module_constant_below_a_function_is_not_that_functions_change():
    from looma import compare

    hunks = compare.parse_patch(
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,4 +1,4 @@\n"
        " def helper():\n     return 1\n \n-NAMES = {}\n+NAMES = {'a': 1}\n")[0]["hunks"]
    assert compare._functions(hunks) == []
