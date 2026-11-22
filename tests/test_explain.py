import json
import os

from fastapi.testclient import TestClient

from conftest import git
from looma.api.app import create_app
from looma.capture.install import init
from looma import explain as explain_module
from looma import summary as summary_module
from looma.functions import units


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def branch_with_changes(repo):
    init(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text(
        "def hello(name):\n"
        "    \"\"\"Greets someone.\"\"\"\n"
        "    if not name:\n"
        "        return 'hi'\n"
        "    return shout(name)\n"
        "\n"
        "\n"
        "def shout(text):\n"
        "    return text.upper()\n"
    )
    git(repo, "commit", "-q", "-am", "Greet people by name")
    [listed] = client().get("/api/repos").json()
    return listed["id"]


def test_functions_before_and_after_with_their_facts(repo):
    repo_id = branch_with_changes(repo)
    result = client().get(f"/api/repos/{repo_id}/functions", params={"path": "app.py"}).json()
    functions = {item["name"]: item for item in result["functions"]}
    assert set(functions) == {"hello", "shout"}

    hello = functions["hello"]
    assert hello["change"] == "modified"
    assert hello["before"]["code"] == "def hello():\n    return 'hi'"
    assert hello["after"]["doc"] == "Greets someone."
    assert hello["facts"]["params_added"] == ["name"]
    assert hello["facts"]["calls_added"] == ["shout"]
    assert hello["facts"]["decisions"] == [0, 1]
    assert hello["explanation"] is None

    shout = functions["shout"]
    assert shout["change"] == "added" and shout["before"] is None and shout["facts"] is None
    # A new function shows where it's used.
    assert [(use["path"], use["line"]) for use in shout["used_by"]] == [("app.py", 5)]


def test_functions_in_typescript():
    text = (
        "// Adds two numbers.\n"
        "export function add(a: number, b: number): number {\n"
        "  if (a < 0) { return b; }\n"
        "  return a + b;\n"
        "}\n"
        "\n"
        "export const Card = ({ title, onOpen }: Props) => {\n"
        "  const click = () => onOpen(title);\n"
        "  return <button onClick={click}>{title}</button>;\n"
        "};\n"
        "\n"
        "type Props = { title: string; onOpen: (t: string) => void };\n"
        "\n"
        "class Store {\n"
        "  load(id: string) {\n"
        "    return fetch(`/api/${id}`);\n"
        "  }\n"
        "}\n"
    )
    found = {unit["name"]: unit for unit in units("src/Card.tsx", text)}
    assert set(found) == {"add", "Card", "Props", "Store.load"}
    assert (found["add"]["start"], found["add"]["end"], found["add"]["doc"]) == (2, 5, "Adds two numbers.")
    assert found["add"]["params"] == ["a", "b"] and found["add"]["decisions"] == 1
    # The inner arrow function belongs to the component, not a unit of its own.
    assert found["Card"]["kind"] == "component" and (found["Card"]["start"], found["Card"]["end"]) == (7, 10)
    assert found["Card"]["params"] == ["title", "onOpen"]
    assert found["Props"]["kind"] == "type" and found["Store.load"]["kind"] == "method"
    assert found["Store.load"]["calls"] == ["fetch"]


def test_project_summary_from_its_files(repo):
    init(repo)
    (repo / "README.md").write_text(
        "# Shop\n\n[![build](x.svg)](ci)\n\nSells plants online. Orders ship next day.\n\n## Setup\n\nRun it.\n"
    )
    (repo / "package.json").write_text(json.dumps({
        "name": "shop", "scripts": {"dev": "vite"},
        "dependencies": {"react": "^18", "left-pad": "1"}, "devDependencies": {"vite": "^5"},
    }))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "readme")
    [listed] = client().get("/api/repos").json()

    summary = client().get(f"/api/repos/{listed['id']}/summary").json()
    assert summary["name"] == "Shop"
    assert summary["description"] == "Sells plants online."
    assert summary["readme"]["sections"] == ["Setup"]
    assert [t["name"] for t in summary["tech"]] == ["react", "vite"]
    assert summary["other_dependencies"] == 1
    assert summary["run"] == [{"where": "package.json", "name": "dev", "command": "vite"}]
    assert summary["history"]["commits"] == 2 and summary["history"]["weeks"][-1] == 2
    assert summary["explanation"] is None


def fake_claude(tmp_path, monkeypatch, answer: dict, is_error=False, error=None):
    """A `claude` command that answers with `answer` and counts its calls. Its sign-in
    prints a page address and waits for the code from that page."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    reply = json.dumps({"type": "result", "is_error": is_error,
                        "result": error or "Here you go:\n```json\n" + json.dumps(answer) + "\n```"})
    script = folder / "claude"
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = 'auth status' ]; then echo '{\"loggedIn\": true}'; exit 0; fi\n"
        "if [ \"$1 $2\" = 'auth login' ]; then\n"
        "  echo 'If the browser didn'\\''t open, visit: https://claude.example/sign-in?x=1'\n"
        f"  read code; echo \"code $code\" >> {calls}; exit 0\n"
        "fi\n"
        f"cat > /dev/null\necho \"$LOOMA_INTERNAL $*\" >> {calls}\n"
        f"cat <<'EOF'\n{reply}\nEOF\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return calls


def test_explaining_a_function_asks_claude_and_a_review_once(repo, tmp_path, monkeypatch):
    repo_id = branch_with_changes(repo)
    calls = fake_claude(tmp_path, monkeypatch, {
        "summary": "Says hello.", "change": "Now uses the name.", "improves": ["Friendlier"],
        "tradeoffs": ["Shouts"], "check": None, "extra": "dropped",
    })
    status = client().get("/api/ai").json()
    assert {k: status[k] for k in ("provider", "tool", "available", "signed_in", "account", "model", "install")} == {
        "provider": "claude", "tool": "claude", "available": True, "signed_in": True, "account": None,
        "model": "haiku", "install": []}
    assert [m["id"] for m in status["models"]] == ["haiku", "sonnet", "opus"]

    request = {"path": "app.py", "name": "hello"}
    first = client().post(f"/api/repos/{repo_id}/explain/function", json=request)
    second = client().post(f"/api/repos/{repo_id}/explain/function", json=request)
    assert first.status_code == 200 and first.json() == second.json()
    assert first.json()["improves"] == ["Friendlier"] and "extra" not in first.json()
    # Run once, then once more to review the answer, both with tools off and marked so
    # Looma doesn't record them; the second click is answered from the saved one.
    asked = calls.read_text().splitlines()
    assert len(asked) == 2
    assert all(call.startswith("1 -p") and "--tools" in call and "--model haiku" in call for call in asked)

    result = client().get(f"/api/repos/{repo_id}/functions", params={"path": "app.py"}).json()
    hello = next(item for item in result["functions"] if item["name"] == "hello")
    assert hello["explanation"]["summary"] == "Says hello."

    missing = client().post(f"/api/repos/{repo_id}/explain/function", json={"path": "app.py", "name": "nope"})
    assert missing.status_code == 404


def test_explaining_the_project_and_a_failed_answer(repo, tmp_path, monkeypatch):
    init(repo)
    [listed] = client().get("/api/repos").json()
    fake_claude(tmp_path, monkeypatch, {}, is_error=True)
    failed = client().post(f"/api/repos/{listed['id']}/explain/project", json={})
    assert failed.status_code == 502 and "Claude Code" in failed.json()["detail"]

    fake_claude(tmp_path, monkeypatch, {
        "overview": "A tiny app.", "how_it_works": ["It says hi."], "parts": [{"name": "Project root", "what": "All of it"}],
        "state": "Just started.",
    })
    made = client().post(f"/api/repos/{listed['id']}/explain/project", json={}).json()
    assert made["overview"] == "A tiny app."
    # A new commit keeps the description: it depends on what the project is, not its history.
    (repo / "notes.txt").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "notes")
    assert client().get(f"/api/repos/{listed['id']}/summary").json()["explanation"]["overview"] == "A tiny app."


def test_signing_in_from_the_dashboard(repo, tmp_path, monkeypatch):
    init(repo)
    [listed] = client().get("/api/repos").json()
    calls = fake_claude(tmp_path, monkeypatch, {}, is_error=True,
                        error="Failed to authenticate: OAuth session expired and could not be refreshed")
    failed = client().post(f"/api/repos/{listed['id']}/explain/project", json={})
    # Signed out: the page is told to offer signing in.
    assert failed.status_code == 424

    started = client().post("/api/ai/sign-in", json={}).json()
    assert started == {"state": "waiting", "url": "https://claude.example/sign-in?x=1", "detail": None}
    finished = client().post("/api/ai/sign-in/code", json={"code": " abc#123 "}).json()
    assert finished["state"] == "signed_in"
    assert "code abc#123" in calls.read_text().splitlines()


def test_without_claude_the_status_gives_the_official_installers(tmp_path, monkeypatch):
    from looma import explain

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    status = client().get("/api/ai").json()
    assert status["available"] is False and status["signed_in"] is None
    expected = explain.install_commands()
    assert [c["command"] for c in status["install"]] == [c["command"] for c in expected]
    assert explain.install_commands("Linux") == [{"shell": "Terminal", "command": "curl -fsSL https://claude.ai/install.sh | bash"}]
    assert [c["shell"] for c in explain.install_commands("Windows")] == ["PowerShell", "Command Prompt"]
    assert explain.install_commands("Windows")[0]["command"] == "irm https://claude.ai/install.ps1 | iex"


def test_a_fresh_native_install_is_found_off_the_path_and_names_the_account(tmp_path, monkeypatch):
    # The native installer puts claude in ~/.local/bin, which the PATH Looma started with
    # may not have yet.
    native = tmp_path / "home" / ".local" / "bin"
    native.mkdir(parents=True)
    (native / "claude").write_text(
        "#!/bin/sh\necho '{\"loggedIn\": true, \"email\": \"me@example.com\", \"orgName\": \"Mine\"}'\n")
    (native / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    status = client().get("/api/ai").json()
    assert status["available"] is True and status["signed_in"] is True
    assert status["account"] == "me@example.com" and status["install"] == []



def test_a_project_with_no_readme_or_commit_still_gets_a_summary_and_a_description(tmp_path, monkeypatch):
    project = tmp_path / "bare"
    (project / "ledger").mkdir(parents=True)
    git(project, "init", "-q")
    (project / "ledger" / "cash_ledger.py").write_text("def balance(entries):\n    return sum(entries)\n")
    (project / "main.py").write_text("from ledger.cash_ledger import balance\nprint(balance([1, 2]))\n")
    init(project)
    [listed] = client().get("/api/repos").json()
    summary = client().get(f"/api/repos/{listed['id']}/summary")
    assert summary.status_code == 200
    facts = summary.json()
    assert facts["readme"] is None and facts["files"] == 2 and facts["entry_points"] == ["main.py"]
    assert facts["history"]["commits"] == 0
    assert client().get(f"/api/repos/{listed['id']}/map").status_code == 200

    calls = fake_claude(tmp_path, monkeypatch, {"overview": "", "how_it_works": [], "parts": [], "state": ""})
    empty = client().post(f"/api/repos/{listed['id']}/explain/project", json={})
    # An answer without its description is an error, not an empty box kept for ever.
    assert empty.status_code == 502 and "left out the description" in empty.json()["detail"]
    assert client().get(f"/api/repos/{listed['id']}/summary").json()["explanation"] is None

    fake_claude(tmp_path, monkeypatch, {"overview": ["A ledger.", "It adds up cash."], "how_it_works": [],
                                        "parts": [], "state": "New."})
    made = client().post(f"/api/repos/{listed['id']}/explain/project", json={}).json()
    assert made["overview"] == "A ledger. It adds up cash."
    # With no readme to go on, the AI is shown how the code starts.
    from looma import summary as summary_module
    samples = summary_module.summary(project, "HEAD")["samples"]
    assert samples[0]["file"] == "main.py" and "print(balance([1, 2]))" in samples[0]["start"]


def test_an_empty_description_kept_by_an_older_version_is_asked_again(repo):
    from looma import explain

    subject = {"name": "x"}
    conn = __import__("looma.store", fromlist=["connect"]).connect()
    with conn:
        conn.execute("INSERT INTO explanations (key, kind, result, created_at) VALUES (?, 'project', ?, 'now')",
                     (explain.key("project", subject), json.dumps({"overview": "", "how_it_works": [], "parts": [], "state": ""})))
    conn.close()
    assert explain.cached("project", subject) is None


def test_secrets_are_blanked_out_before_anything_reaches_an_ai():
    from looma.explain import REDACTED, redact

    fake = {
        "aws": "key = AKIAIOSFODNN7EXAMPLE",
        "github": "token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        "anthropic": "sk-ant-api03-" + "x" * 40,
        "openai": "sk-proj-" + "Y" * 40,
        "slack": "xoxb-1234567890-abcdefghij",
        "google": "AIza" + "S" * 35,
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7\nabc\n-----END RSA PRIVATE KEY-----",
    }
    for name, secret in fake.items():
        assert REDACTED in redact(secret), name
        assert secret.split()[-1][-12:] not in redact(secret), name

    code = (
        'DB = "mongodb+srv://admin:Sup3rS3cret@cluster0.example.net/app"\n'
        'api_key = "live_9f8e7d6c5b4a"\n'
        '{"password": "hunter2hunter2"}\n'
        "export STRIPE_SECRET_KEY=rk_live_abcdef123456\n"
        'password = request.form["password"]\n'
        "def total(items):\n    return sum(items)\n"
    )
    cleaned = redact(code)
    for leaked in ("Sup3rS3cret", "live_9f8e7d6c5b4a", "hunter2hunter2", "rk_live_abcdef123456"):
        assert leaked not in cleaned
    assert "mongodb+srv://admin:[redacted]@cluster0.example.net/app" in cleaned
    # Ordinary code is left alone, including code that only handles a password.
    assert 'password = request.form["password"]' in cleaned
    assert "def total(items):\n    return sum(items)" in cleaned


def test_claude_runs_with_no_tools_and_an_old_one_is_asked_to_update(repo, tmp_path, monkeypatch):
    repo_id = branch_with_changes(repo)
    calls = fake_claude(tmp_path, monkeypatch, {"summary": "Says hi.", "change": "x", "improves": [],
                                                "tradeoffs": [], "check": None})
    assert client().post(f"/api/repos/{repo_id}/explain/function", json={"path": "app.py", "name": "hello"}).status_code == 200
    call = calls.read_text().splitlines()[0]
    assert all("--tools  --strict-mcp-config" in c for c in calls.read_text().splitlines())
    assert "--tools  --strict-mcp-config" in call and "dangerously" not in call and "bypass" not in call.lower()

    old = tmp_path / "old"
    old.mkdir()
    (old / "claude").write_text("#!/bin/sh\nif [ \"$1 $2\" = 'auth status' ]; then echo '{\"loggedIn\": true}'; exit 0; fi\n"
                                "cat > /dev/null\necho \"error: unknown option '--tools'\" >&2\nexit 1\n")
    (old / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{old}{os.pathsep}{os.environ['PATH']}")
    # The first answer is kept, so ask about code not explained yet.
    (repo / "app.py").write_text("def hello(name):\n    return 'hey ' + name\n")
    git(repo, "commit", "-q", "-am", "Hey")
    answer = client().post(f"/api/repos/{repo_id}/explain/function", json={"path": "app.py", "name": "hello"})
    assert answer.status_code == 502 and "Update Claude Code" in answer.json()["detail"]


def test_looma_data_is_private_to_its_user(tmp_path, monkeypatch):
    from looma import paths

    home = tmp_path / "private-home"
    home.mkdir(mode=0o755)
    (home / "looma.db.before-x").write_text("old copy")
    monkeypatch.setenv("LOOMA_HOME", str(home))
    paths.home()
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    assert oct((home / "looma.db.before-x").stat().st_mode & 0o777) == "0o600"


def test_every_kind_of_changed_file_has_something_to_explain(repo):
    init(repo)
    (repo / "style.css").write_text("a,\nb {\n  color: red;\n}\n\n.card { padding: 4px; }\n")
    (repo / "README.md").write_text("Intro.\n\n# Setup\n\nRun it.\n\n## Use\n\nClick.\n")
    (repo / "config.json").write_text('{\n  "port": 80,\n  "debug": false\n}\n')
    (repo / "app.py").write_text("import os\n\n\ndef hello():\n    return 'hi'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "files")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "style.css").write_text("a,\nb {\n  color: blue;\n}\n\n.card { padding: 4px; }\n")
    (repo / "README.md").write_text("Intro.\n\n# Setup\n\nRun it twice.\n\n## Use\n\nClick.\n")
    (repo / "config.json").write_text('{\n  "port": 8080,\n  "debug": false\n}\n')
    # Only an import changes: outside every function.
    (repo / "app.py").write_text("import sys\n\n\ndef hello():\n    return 'hi'\n")
    (repo / "deploy.sh").write_text("#!/bin/sh\necho deploy\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change them")
    [listed] = client().get("/api/repos").json()

    def changed(path):
        result = client().get(f"/api/repos/{listed['id']}/functions", params={"path": path}).json()
        return [(f["name"], f["kind"], f["change"]) for f in result["functions"]], result["functions"]

    assert changed("style.css")[0] == [("a, b", "style", "modified")]
    assert changed("README.md")[0] == [("Setup", "section", "modified")]
    names, [config] = changed("config.json")
    assert names == [("config.json", "file", "modified")]
    assert '-  "port": 80,' in config["diff"] and '+  "port": 8080,' in config["diff"]
    assert changed("app.py")[0] == [("app.py", "file", "modified")]
    names, [script] = changed("deploy.sh")
    assert names == [("deploy.sh", "file", "added")] and script["diff"] is None
    assert script["after"]["code"].strip() == "#!/bin/sh\necho deploy"


def test_a_whole_file_is_explained_from_its_diff(repo, tmp_path, monkeypatch):
    init(repo)
    (repo / "settings.yaml").write_text("retries: 3\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "settings")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "settings.yaml").write_text("retries: 5\n")
    git(repo, "commit", "-q", "-am", "more retries")
    [listed] = client().get("/api/repos").json()
    prompts = tmp_path / "prompts"
    folder = tmp_path / "bin"
    folder.mkdir()
    reply = json.dumps({"type": "result", "is_error": False, "result": json.dumps({
        "summary": "Sets how often to retry.", "change": "Retries more.", "improves": [], "tradeoffs": [],
        "check": None})})
    (folder / "claude").write_text(f"#!/bin/sh\ncat >> {prompts}\ncat <<'EOF'\n{reply}\nEOF\n")
    (folder / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")

    made = client().post(f"/api/repos/{listed['id']}/explain/function",
                         json={"path": "settings.yaml", "name": "settings.yaml"})
    assert made.status_code == 200 and made.json()["summary"] == "Sets how often to retry."
    asked = prompts.read_text()
    assert "Whole file: settings.yaml" in asked and "+retries: 5" in asked


def test_prompts_fit_small_models_without_losing_the_instructions(monkeypatch):
    from looma import explain

    long = "x" * 50_000 + "\nReply with only a JSON object"
    fitted = explain._fit(long, 12_000)
    assert len(fitted) <= 12_000 and fitted.endswith("Reply with only a JSON object")
    monkeypatch.setattr(explain.preferences, "ai_choice", lambda: ("ollama", "qwen2.5:3b"))
    assert explain.prompt_budget() == explain.SMALL_PROMPT_CHARS
    subject = {"path": "big.py", "sha": "1"}
    prompt = explain._file_prompt(subject, {"text": "y\n" * 40_000, "outline": []}, explain.SMALL_PROMPT_CHARS)
    assert len(prompt) < explain.SMALL_PROMPT_CHARS and '"purpose"' in prompt


def test_knowledge_transfer_file_by_file(repo, tmp_path, monkeypatch):
    init(repo)
    (repo / "app.py").write_text("def hello(name):\n    \"\"\"Greets someone.\"\"\"\n    return name\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x00")
    (repo / "package-lock.json").write_text("{}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "app")
    [listed] = client().get("/api/repos").json()
    kt = client().get(f"/api/repos/{listed['id']}/kt").json()
    # Images and lockfiles aren't explained.
    paths = [f["path"] for f in kt["files"]]
    assert "app.py" in paths and "logo.png" not in paths and "package-lock.json" not in paths
    assert kt["explained"] == 0

    calls = fake_claude(tmp_path, monkeypatch, {
        "purpose": "Greets people.", "functions": [{"name": "hello", "what": "Returns the name."}, "junk"],
        "connects": "Nothing else.", "notes": ["Keep it short"],
    })
    made = client().post(f"/api/repos/{listed['id']}/explain/file", json={"path": "app.py"}).json()
    assert made == {"purpose": "Greets people.", "functions": [{"name": "hello", "what": "Returns the name."}],
                    "connects": "Nothing else.", "notes": ["Keep it short"]}
    client().post(f"/api/repos/{listed['id']}/explain/file", json={"path": "app.py"})
    assert len(calls.read_text().splitlines()) == 1  # saved: asked once
    kt = client().get(f"/api/repos/{listed['id']}/kt").json()
    assert kt["explained"] == 1
    assert next(f for f in kt["files"] if f["path"] == "app.py")["explanation"]["purpose"] == "Greets people."
    missing = client().post(f"/api/repos/{listed['id']}/explain/file", json={"path": "logo.png"})
    assert missing.status_code == 404

    # An edit to the file asks again.
    (repo / "app.py").write_text("def hello(name):\n    return name.title()\n")
    git(repo, "commit", "-q", "-am", "title case")
    assert client().get(f"/api/repos/{listed['id']}/kt").json()["explained"] == 0


def test_project_kt_reads_the_file_notes(repo, tmp_path, monkeypatch):
    init(repo)
    [listed] = client().get("/api/repos").json()
    fake_claude(tmp_path, monkeypatch, {"purpose": "Says hi.", "functions": [], "connects": "", "notes": []})
    client().post(f"/api/repos/{listed['id']}/explain/file", json={"path": "app.py"})

    prompts = tmp_path / "prompts"
    folder = tmp_path / "bin"
    reply = json.dumps({"type": "result", "is_error": False, "result": json.dumps({
        "overview": "A greeter.", "how_it_works": ["It greets."], "parts": [],
        "flows": [{"name": "Greeting", "steps": ["app.py says hi", 3]}, "junk"],
        "start_here": [{"path": "app.py", "why": "It's all there."}], "state": "New."})})
    (folder / "claude").write_text(f"#!/bin/sh\ncat >> {prompts}\ncat <<'EOF'\n{reply}\nEOF\n")
    made = client().post(f"/api/repos/{listed['id']}/explain/project", json={}).json()
    assert made["flows"] == [{"name": "Greeting", "steps": ["app.py says hi"]}]
    assert made["start_here"] == [{"path": "app.py", "why": "It's all there."}]
    assert "- app.py: Says hi." in prompts.read_text()
    # Saved, unless asked afresh.
    client().post(f"/api/repos/{listed['id']}/explain/project", json={})
    assert prompts.read_text().count("What each file is for") == 1
    client().post(f"/api/repos/{listed['id']}/explain/project", json={"fresh": True})
    assert prompts.read_text().count("What each file is for") == 2


def test_asking_about_the_project_reads_the_notes_and_the_files_it_names(repo, tmp_path, monkeypatch):
    init(repo)
    (repo / "billing.py").write_text("def charge(card, amount):\n    return card.pay(amount)\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "billing")
    [listed] = client().get("/api/repos").json()
    fake_claude(tmp_path, monkeypatch, {"purpose": "Takes payments.", "functions": [{"name": "charge", "what": "Charges a card."}],
                                       "connects": "", "notes": []})
    client().post(f"/api/repos/{listed['id']}/explain/file", json={"path": "billing.py"})

    prompts = tmp_path / "prompts"
    folder = tmp_path / "bin"
    reply = json.dumps({"type": "result", "is_error": False, "result": json.dumps({
        "answer": "`charge` takes the payment.", "files": ["billing.py", "not/a/file.py"]})})
    (folder / "claude").write_text(f"#!/bin/sh\ncat >> {prompts}\ncat <<'EOF'\n{reply}\nEOF\n")
    asked = client().post(f"/api/repos/{listed['id']}/ask", json={
        "question": "What does charge do?", "history": [{"role": "user", "text": "Hi"}, {"role": "assistant", "text": "Hello"}]})
    assert asked.status_code == 200
    assert asked.json() == {"answer": "`charge` takes the payment.", "files": ["billing.py"], "read": ["billing.py"],
                            "follow_ups": [], "explained": 1, "total": 2}
    prompt = prompts.read_text()
    # The note on each file, the named file's code, the conversation, commands in fenced blocks,
    # and the guide to Looma itself.
    assert "- billing.py: Takes payments." in prompt and "return card.pay(amount)" in prompt
    assert "Them: Hi" in prompt and "fenced block" in prompt
    assert "How to use Looma" in prompt and "`looma serve`" in prompt
    # Nothing is saved: asking again asks again.
    client().post(f"/api/repos/{listed['id']}/ask", json={"question": "What does charge do?"})
    assert prompt.count("Their question") == 1 and prompts.read_text().count("Their question") == 2

    # A follow-up that names no file is about the file of the question before.
    client().post(f"/api/repos/{listed['id']}/ask", json={
        "question": "And who calls it?", "history": [{"role": "user", "text": "What is billing.py for?"}]})
    assert prompts.read_text().split("Their question")[-2].count("return card.pay(amount)") == 1

    assert client().post(f"/api/repos/{listed['id']}/ask", json={"question": "  "}).status_code == 400


def test_ask_knows_the_projects_commands_and_looma_and_a_small_model_gets_the_guide_only_when_asked(monkeypatch):
    from looma import explain

    context = {"project": {"name": "Shop", "run": ["test: pytest -q (pyproject.toml)"], "entry_points": ["shop/main.py"],
                           "readme": {"title": "Shop", "intro": "Sells things.", "first_sentence": "Sells things.",
                                      "sections": ["Install", "Testing"]}}}
    big = explain._ask_prompt("How do I run the tests?", [], context, explain.MAX_PROMPT_CHARS)
    assert "test: pytest -q (pyproject.toml)" in big and "shop/main.py" in big
    assert "Sells things." in big and "Install, Testing" in big
    assert "How to use Looma" in big  # a large model always has it
    small = explain._ask_prompt("How do I run the tests?", [], context, explain.SMALL_PROMPT_CHARS)
    assert "How to use Looma" not in small and "pytest -q" in small
    asked = explain._ask_prompt("Where do I turn on uncommitted work?", [], context, explain.SMALL_PROMPT_CHARS)
    assert "How to use Looma" in asked and "Show uncommitted" in asked
    assert explain.about_looma("how do I write the KT") and not explain.about_looma("what does charge() return")
    # The readme and setup files, word for word: where the real commands are.
    context["setup"] = {"readme": "## Testing\n\n    pip install -e .[dev]\n    pytest -q\n",
                        "files": [{"path": "pyproject.toml", "text": "[project]\nname = \"shop\""}], "scripts": ["scripts/run.sh"]}
    assert "pip install -e .[dev]" in explain._ask_prompt("How do I run the tests?", [], context, explain.SMALL_PROMPT_CHARS)
    assert "pip install -e .[dev]" not in explain._ask_prompt("What does charge do?", [], context, explain.SMALL_PROMPT_CHARS)
    assert "scripts/run.sh" in explain._ask_prompt("What does charge do?", [], context, explain.MAX_PROMPT_CHARS)


def test_a_plain_words_answer_is_kept_and_files_are_picked_by_name(monkeypatch):
    from looma import explain

    files = [{"path": "src/api/client.ts", "explanation": None},
             {"path": "src/App.tsx", "explanation": {"purpose": "The page.", "functions": [{"name": "App.render", "what": ""}]}},
             {"path": "README.md", "explanation": None}]
    assert explain.pick_files("What does client.ts do?", files) == ["src/api/client.ts"]
    assert explain.pick_files("where is render called", files) == ["src/App.tsx"]
    assert explain.pick_files("How does the project work?", files) == []

    # A small model that ignores the JSON it was asked for.
    monkeypatch.setattr(explain, "_run", lambda prompt: "It reads git and shows the changes.")
    assert explain.answer("What is it?", [], {"files": files}) == {"answer": "It reads git and shows the changes.",
                                                                    "files": [], "follow_ups": []}
    # A function read from the code finds its file, with no KT note on it.
    assert explain.pick_files("what does fetchRepos do", files, functions={"src/api/client.ts": {"fetchRepos"}}) == ["src/api/client.ts"]


def test_the_short_kt_is_one_request_from_the_readme_and_setup_files(repo, monkeypatch):
    (repo / "README.md").write_text("# Shop\n\nSells things.\n\n![The cart](docs/cart.png)\n![build](https://img.shields.io/x.png)\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "cart.png").write_bytes(b"\x89PNG picture")
    (repo / ".env.example").write_text("API_URL=\n")
    (repo / ".env").write_text("API_URL=secret\n")
    (repo / "cart.py").write_text('def total(items):\n    """Adds up a cart."""\n    return sum(items)\n')
    (repo / "test_cart.py").write_text("def test_total():\n    assert True\n")
    git(repo, "add", "README.md", "docs", ".env.example", "cart.py", "test_cart.py")
    git(repo, "commit", "-q", "-m", "docs")
    asked = []

    def answer(prompt):
        asked.append(prompt)
        return json.dumps({
            "overview": "A shop.", "needs": ["Python", {"name": "Git", "why": "To get the code.", "check": "git --version"}],
            "setup": [{"step": "Install it", "detail": "In the shop folder.", "command": "pip install ."},
                                                                  {"step": "Wait", "command": ""}],
            "run": [{"what": "Start it", "command": "python app.py"}], "how_to_use": ["Open it"],
            "settings": [{"name": "API_URL", "what": "the backend"}], "start_here": [{"path": "app.py", "why": "start"}],
            "functions": [{"name": "total", "path": "cart.py", "what": "Adds up a cart.", "use": "For the checkout."},
                          {"name": "made_up", "path": "cart.py", "what": "Not real.", "use": ""}],
            "images": [{"path": "docs/cart.png", "caption": "The cart"}, {"path": "../secret.png", "caption": "Not real"}],
        })

    monkeypatch.setattr(explain_module, "_run", answer)
    listed = client().post("/api/repos", json={"path": str(repo)}).json()
    url = f"/api/repos/{listed['id']}/kt"
    assert client().get(f"{url}/guide").json() == {"guide": None}
    written = client().post(f"{url}/guide", json={}).json()
    assert written["overview"] == "A shop." and written["setup"][1]["command"] is None
    assert written["needs"] == [{"name": "Python", "why": "", "check": None},
                                {"name": "Git", "why": "To get the code.", "check": "git --version"}]
    assert written["setup"][0]["detail"] == "In the shop folder." and written["setup"][1]["detail"] == ""
    # Only pictures the project has, and only one request however big the project is.
    assert [image["path"] for image in written["images"]] == ["docs/cart.png"]
    # Only functions read from the code, never from a test, with the code's own signature.
    assert [(f["name"], f["signature"]) for f in written["functions"]] == [("total", "def total(items)")]
    assert "test_total" not in asked[0] and "Adds up a cart" in asked[0]
    assert len(asked) == 1 and "Sells things" in asked[0] and "API_URL=" in asked[0] and "secret" not in asked[0]
    assert "shields.io" not in json.dumps(summary_module.guide_facts(repo, "HEAD")["images"])
    assert client().get(f"{url}/guide").json()["guide"]["overview"] == "A shop."
    assert len(asked) == 1
    # Pictures are served, and that's no way to read any other file.
    image = client().get(f"{url}/image", params={"path": "docs/cart.png"})
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert client().get(f"{url}/image", params={"path": "app.py"}).status_code == 404
    assert client().get(f"{url}/image", params={"path": ".env"}).status_code == 404


def test_a_function_prompt_shows_the_changed_lines_and_what_it_calls():
    subject = {"path": "app.py", "name": "save", "kind": "function", "change": "modified",
               "before": "def save(p):\n    write(p)\n", "after": "def save(p):\n    check(p)\n    write(p)\n"}
    helpers = [{"name": "check", "code": "def check(p):\n    if not p:\n        raise ValueError", "new_call": True}]
    prompt = explain_module._function_prompt(subject, {"helpers": helpers, "commits": ["Big refactor"]})
    # The exact lines, not two copies to compare, and the helper's code rather than its name.
    assert "+    check(p)" in prompt and " def save(p):" in prompt
    assert "check (newly called):" in prompt and "raise ValueError" in prompt
    # Branch-wide commit titles are background, never proof.
    assert "never as proof" in prompt


def test_pros_and_cons_must_quote_the_code_and_not_guess():
    prompt = "```\n+    check(p)\n     write(p)\n```"
    items = [
        {"point": "Empty input is now refused.", "evidence": "+    check(p)"},       # quoted: kept
        {"point": "Saves are faster.", "evidence": "cache.get(p)"},              # not in the code: dropped
        {"point": "It is unclear what check does.", "evidence": "check(p)"},     # a guess: dropped
        {"point": "Empty input is now refused.", "evidence": "check(p)"},        # a repeat: dropped
        "Writes still happen once.",                                             # a small model's plain string: kept
        {"point": "No evidence given."},                                         # dropped
    ]
    assert explain_module._grounded(items, prompt) == ["Empty input is now refused.", "Writes still happen once."]
    result = explain_module._clean("function", {"summary": "Saves.", "change": "x", "improves": items, "tradeoffs": [],
                                         "check": None, "differences": ["a"]}, prompt)
    assert result["improves"] == ["Empty input is now refused.", "Writes still happen once."]
    assert "differences" not in result


def test_a_failed_review_keeps_the_first_answer(monkeypatch):
    replies = iter(['{"summary": "Saves.", "change": "x", "improves": [], "tradeoffs": [], "check": null}'])

    def run(prompt):
        try:
            return next(replies)
        except StopIteration:
            raise explain_module.ExplainError("Claude took too long to answer. Try again.")

    monkeypatch.setattr(explain_module, "_run", run)
    subject = {"path": "a.py", "name": "f", "kind": "function", "change": "added", "after": "def f():\n    pass\n"}
    assert explain_module.explain("function", subject)["summary"] == "Saves."


def test_a_changed_function_brings_the_code_of_what_it_newly_calls():
    from looma import functions
    old = "def check(p):\n    return p\n\ndef log(p):\n    print(p)\n\ndef save(p):\n    log(p)\n"
    new = "def check(p):\n    return p\n\ndef log(p):\n    print(p)\n\ndef save(p):\n    log(p)\n    check(p)\n"
    before = {u["name"]: u for u in units("a.py", old)}
    after = {u["name"]: u for u in units("a.py", new)}
    found = functions.helpers(after["save"], before["save"], after)
    assert [(h["name"], h["new_call"]) for h in found] == [("check", True), ("log", False)]
    assert "return p" in found[0]["code"]
