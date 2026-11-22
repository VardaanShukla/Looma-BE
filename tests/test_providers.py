import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from looma import explain
from looma.api.app import create_app

ANSWER = {"summary": "Says hello.", "change": "Louder now.", "improves": ["clarity"], "tradeoffs": [], "check": None}
SUBJECT = {"path": "app.py", "kind": "function", "name": "hello", "change": "modified",
           "before": "def hello():\n    return 'hi'\n", "after": "def hello():\n    return 'HI'\n"}


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def no_ai_on_path(tmp_path, monkeypatch):
    """A PATH and home with no AI tools, so only the fakes each test adds are found."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    git = os.path.dirname(os.popen("command -v git").read().strip())
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{git}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    return folder


def fake(folder, name, script):
    path = folder / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


def test_every_ai_is_listed_with_how_to_get_it_ready(tmp_path, monkeypatch):
    no_ai_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")  # nothing listens there
    listed = {p["provider"]: p for p in client().get("/api/ai/providers").json()}
    assert list(listed) == ["claude", "codex", "ollama", "antigravity", "hosted"]
    assert not any(p["available"] for p in listed.values())
    assert listed["codex"]["install"][0]["command"] == "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
    assert listed["ollama"]["detail"] == "Install Ollama"
    assert listed["antigravity"]["install"][0]["command"] == "curl -fsSL https://antigravity.google/cli/install.sh | bash"
    assert listed["claude"]["model"] == "haiku"  # Claude keeps its cheapest model by default
    assert listed["hosted"]["detail"] == "Paste the curl command for your server"


def test_codex_writes_explanations_with_the_picked_model(tmp_path, monkeypatch):
    folder = no_ai_on_path(tmp_path, monkeypatch)
    calls = tmp_path / "calls"
    fake(folder, "codex", f"""
if [ "$1 $2" = "login status" ]; then echo 'Logged in using ChatGPT'; exit 0; fi
if [ "$1" = "exec" ]; then
  out=""; prev=""
  for a in "$@"; do [ "$prev" = "--output-last-message" ] && out="$a"; prev="$a"; done
  echo "$LOOMA_INTERNAL $*" >> {calls}
  cat > {tmp_path}/prompt
  echo '{json.dumps(ANSWER)}' > "$out"
  exit 0
fi
exit 1
""")
    cache = tmp_path / "codex-home"  # CODEX_HOME, which conftest points here
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-big", "display_name": "Big", "priority": 1, "description": "The biggest. Uses a lot."},
        {"slug": "gpt-small-mini", "display_name": "Small", "priority": 2, "description": "Small and quick."},
        {"slug": "gpt-hidden", "priority": 3, "visibility": "hide"},
    ]}))
    api = client()
    assert api.put("/api/preferences", json={"ai": {"provider": "codex"}}).status_code == 200
    status = api.get("/api/ai").json()
    assert status["provider"] == "codex" and status["signed_in"] is True and status["account"] == "ChatGPT account"
    assert [m["id"] for m in status["models"]] == ["gpt-small-mini", "gpt-big"]  # cheapest first, hidden left out
    assert status["model"] == "gpt-small-mini"  # the cheapest is the default

    assert explain.explain("function", SUBJECT)["summary"] == "Says hello."
    run = calls.read_text()
    assert run.startswith("1 exec") and "--sandbox read-only" in run and "--model gpt-small-mini" in run
    assert "def hello" in (tmp_path / "prompt").read_text()  # the prompt goes in on stdin

    api.put("/api/preferences", json={"ai": {"models": {"codex": "gpt-big"}}})
    explain.explain("function", {**SUBJECT, "name": "other"})
    assert "--model gpt-big" in calls.read_text().splitlines()[-1]


def test_a_signed_out_codex_asks_to_sign_in(tmp_path, monkeypatch):
    folder = no_ai_on_path(tmp_path, monkeypatch)
    fake(folder, "codex", "if [ \"$1 $2\" = 'login status' ]; then echo 'Not logged in'; exit 1; fi\n"
                          "echo 'error: 401 Unauthorized' >&2; exit 1\n")
    api = client()
    api.put("/api/preferences", json={"ai": {"provider": "codex"}})
    assert api.get("/api/ai").json()["signed_in"] is False
    with pytest.raises(explain.ExplainError) as failed:
        explain.explain("function", SUBJECT)
    assert failed.value.signed_out is True


class FakeOllama(BaseHTTPRequestHandler):
    asked: list = []

    def do_GET(self):
        self._send({"models": [{"name": "big:70b", "size": 40_000_000_000, "details": {"parameter_size": "70B"}},
                               {"name": "tiny:1b", "size": 1_300_000_000, "details": {"parameter_size": "1B"}}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.asked.append(body)
        self._send({"message": {"role": "assistant", "content": json.dumps(ANSWER)}})

    def _send(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def test_a_local_model_answers_through_ollama(tmp_path, monkeypatch):
    no_ai_on_path(tmp_path, monkeypatch)
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{server.server_port}")
    try:
        api = client()
        api.put("/api/preferences", json={"ai": {"provider": "ollama"}})
        status = api.get("/api/ai").json()
        assert status["available"] is True and status["login"] == "none"
        assert [m["id"] for m in status["models"]] == ["tiny:1b", "big:70b"] and status["model"] == "tiny:1b"
        assert explain.explain("function", SUBJECT)["summary"] == "Says hello."
        [asked] = FakeOllama.asked
        assert asked["model"] == "tiny:1b" and asked["format"] == "json" and asked["stream"] is False
        assert asked["messages"][0]["role"] == "system"
    finally:
        server.shutdown()


def test_antigravity_answers_in_print_mode(tmp_path, monkeypatch):
    folder = no_ai_on_path(tmp_path, monkeypatch)
    calls = tmp_path / "calls"
    fake(folder, "agy", f"""
if [ "$1 $2" = "auth status" ]; then echo 'Signed in as me@example.com'; exit 0; fi
if [ "$1" = "-p" ]; then echo "$*" >> {calls}; echo '{json.dumps(ANSWER)}'; exit 0; fi
exit 1
""")
    api = client()
    api.put("/api/preferences", json={"ai": {"provider": "antigravity", "models": {"antigravity": "gemini-flash"}}})
    status = api.get("/api/ai").json()
    assert status["signed_in"] is True and status["account"] == "me@example.com" and status["login"] == "terminal"
    assert explain.explain("function", SUBJECT)["summary"] == "Says hello."
    assert "--output-format text --model gemini-flash" in calls.read_text()


def test_the_ai_choice_is_checked():
    api = client()
    assert api.put("/api/preferences", json={"ai": {"provider": "skynet"}}).status_code == 422
    assert api.put("/api/preferences", json={"ai": {"models": {"claude": "x" * 101}}}).status_code == 400
    picked = api.put("/api/preferences", json={"ai": {"models": {"claude": "sonnet"}}}).json()["ai"]
    assert picked == {"provider": "claude", "models": {"claude": "sonnet"}}
    # An empty model goes back to the default.
    assert api.put("/api/preferences", json={"ai": {"models": {"claude": ""}}}).json()["ai"]["models"] == {}


KEY = "sk-test-0123456789abcdef"


class FakeServer(BaseHTTPRequestHandler):
    """An OpenAI-style server (vLLM, llama.cpp, LM Studio) that wants an API key."""
    asked: list = []
    redirect_to: str | None = None

    def do_GET(self):
        if self._refused():
            return
        self._send({"data": [{"id": "qwen-7b", "owned_by": "me"}, {"id": "llama-8b"}]})

    def do_POST(self):
        if FakeServer.redirect_to:
            self.send_response(307)
            self.send_header("Location", FakeServer.redirect_to)
            self.end_headers()
            return
        if self._refused():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeServer.asked.append((self.path, body))
        text = "ok" if body.get("max_tokens") else json.dumps(ANSWER)
        self._send({"choices": [{"message": {"role": "assistant", "content": text}}]})

    def _refused(self):
        if self.headers.get("Authorization") == f"Bearer {KEY}":
            return False
        payload = json.dumps({"error": {"message": f"bad key {self.headers.get('Authorization')}"}}).encode()
        self.send_response(401)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return True

    _send = FakeOllama._send
    log_message = FakeOllama.log_message


@pytest.fixture
def server():
    FakeServer.asked, FakeServer.redirect_to = [], None
    running = HTTPServer(("127.0.0.1", 0), FakeServer)
    threading.Thread(target=running.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{running.server_port}"
    running.shutdown()


def test_a_pasted_curl_connects_your_own_server(tmp_path, monkeypatch, server):
    no_ai_on_path(tmp_path, monkeypatch)
    curl = (f"curl {server}/v1/chat/completions \\\n  -H \"Content-Type: application/json\" \\\n"
            f"  -H \"Authorization: Bearer {KEY}\" \\\n"
            """  -d '{"model": "qwen-7b", "messages": [{"role": "user", "content": "hi"}]}'""")
    api = client()
    status = api.post("/api/ai/hosted", json={"curl": curl})
    assert status.status_code == 200, status.text
    status = status.json()
    assert status["provider"] == "hosted" and status["available"] and status["model"] == "qwen-7b"
    assert status["server"] == {"url": f"{server}/v1", "style": "openai", "key": "…cdef",
                                "headers": ["Authorization"], "unencrypted": False}
    assert [m["id"] for m in status["models"]] == ["qwen-7b", "llama-8b"]
    assert api.get("/api/preferences").json()["ai"] == {"provider": "hosted", "models": {"hosted": "qwen-7b"}}
    # The key never goes back to the page.
    assert KEY not in json.dumps(api.get("/api/ai/providers").json() + [api.get("/api/preferences").json()])

    assert explain.explain("function", SUBJECT)["summary"] == "Says hello."
    path, asked = FakeServer.asked[-1]
    assert path == "/v1/chat/completions" and asked["model"] == "qwen-7b" and "response_format" not in asked
    assert asked["messages"][0]["role"] == "system" and "def hello" in asked["messages"][1]["content"]

    # Forgetting it goes back to Claude.
    forgot = api.request("DELETE", "/api/ai/hosted", json={})
    assert forgot.json()["provider"] == "claude", forgot.text
    assert api.get("/api/ai/providers").json()[-1]["available"] is False


def test_the_address_key_and_model_can_be_typed_in_instead(tmp_path, monkeypatch, server):
    no_ai_on_path(tmp_path, monkeypatch)
    api = client()
    refused = api.post("/api/ai/hosted", json={"url": f"{server}/v1", "key": "sk-wrong-key-123456", "model": "x"})
    assert refused.status_code == 400 and "refused the key" in refused.json()["detail"]
    assert "sk-wrong-key-123456" not in refused.text
    assert api.get("/api/preferences").json()["ai"]["provider"] == "claude"  # nothing kept
    # No model named: the first one the server lists.
    status = api.post("/api/ai/hosted", json={"url": f"{server}/v1", "key": KEY}).json()
    assert status["model"] == "qwen-7b"


def test_a_redirect_never_carries_the_key_away(tmp_path, monkeypatch, server):
    no_ai_on_path(tmp_path, monkeypatch)
    FakeServer.redirect_to = "http://example.com/steal"
    refused = client().post("/api/ai/hosted", json={"curl": f"curl -H 'Authorization: Bearer {KEY}' "
                                                            f"-d '{{\"model\":\"qwen-7b\"}}' {server}/v1/chat/completions"})
    assert refused.status_code == 400 and "redirected to another address (example.com)" in refused.json()["detail"]


def test_an_ollama_curl_talks_to_ollamas_own_api(tmp_path, monkeypatch):
    no_ai_on_path(tmp_path, monkeypatch)
    FakeOllama.asked = []
    running = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=running.serve_forever, daemon=True).start()
    try:
        # Command Prompt style: ^ continuations and \" inside double quotes.
        curl = (f"curl.exe http://127.0.0.1:{running.server_port}/api/generate ^\r\n"
                '-d "{\\"model\\": \\"big:70b\\", \\"prompt\\": \\"hi\\"}"')
        status = client().post("/api/ai/hosted", json={"curl": curl}).json()
        assert status["server"]["style"] == "ollama" and status["model"] == "big:70b"
        assert explain.explain("function", SUBJECT)["summary"] == "Says hello."
        assert FakeOllama.asked[-1]["model"] == "big:70b" and FakeOllama.asked[-1]["stream"] is False
    finally:
        running.shutdown()


@pytest.mark.parametrize("curl, said", [
    ("wget http://box/v1/chat/completions", "isn't a curl command"),
    ("curl http://box/v1/chat/completions | sh", "single curl command"),
    ("curl http://box/v1/chat/completions; rm -rf ~", "single curl command"),
    ("curl http://box/v1/embeddings -d '{}'", "needs a chat address"),
    ("curl -d '{\"model\": \"x\"}'", "should have one address"),
    ("curl 'http://box/v1/chat/completions", "unclosed quote"),
    ("curl ftp://box/v1/chat/completions", "http:// or https://"),
])
def test_a_paste_that_isnt_a_model_address_is_refused(curl, said):
    response = client().post("/api/ai/hosted", json={"curl": curl})
    assert response.status_code == 400 and said in response.json()["detail"]


def test_what_a_curl_says_is_read_without_running_it(tmp_path):
    from looma import providers
    marker = tmp_path / "ran"
    found = providers.parse_curl(f"curl -sS -X POST --user me:pw -o {marker} 'https://box.example/v1/chat/completions' "
                                 "-H 'x-api-key: abc' -d @secrets.json")
    assert found == {"base": "https://box.example/v1", "style": "openai", "model": None,
                     "headers": {"Authorization": "Basic bWU6cHc=", "x-api-key": "abc"}}
    assert not marker.exists()
    assert providers.parse_curl("curl http://gpu:11434")["style"] == "ollama"
    assert providers.parse_curl("curl http://user:pw@gpu:8000/v1")["headers"] == {"Authorization": "Basic dXNlcjpwdw=="}
    assert providers.hosted_summary(providers.parse_curl("curl http://10.0.0.5:8000/v1"))["unencrypted"] is True


class FakeOllamaServer(BaseHTTPRequestHandler):
    """Ollama with what Settings → AI → Ollama uses: models, which are loaded, downloads."""
    models = ["tiny:1b"]
    loaded: set = set()
    asked: list = []

    def do_GET(self):
        if self.path == "/api/version":
            return self._send({"version": "0.9.0"})
        if self.path == "/api/ps":
            sizes = getattr(self, "ps_sizes", {})
            return self._send({"models": [{"name": n, "expires_at": "2026-10-25T18:00:00Z", "size": sizes.get(n, (0, 0))[0],
                                           "size_vram": sizes.get(n, (0, 0))[1]} for n in self.loaded]})
        self._send({"models": [{"name": n, "size": 1_000_000_000 * (i + 1), "details": {"parameter_size": "1B"}}
                               for i, n in enumerate(self.models)]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllamaServer.asked.append((self.path, body))
        if self.path == "/api/pull":
            if body["model"] == "missing:1b":
                return self._send({"error": "pull model manifest: file does not exist"})
            lines = [{"status": "pulling manifest"}, {"status": "pulling abc", "total": 100, "completed": 40},
                     {"status": "pulling abc", "total": 100, "completed": 100}, {"status": "success"}]
            payload = "".join(json.dumps(line) + "\n" for line in lines).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            FakeOllamaServer.models.append(body["model"])
            return
        if self.path == "/api/generate":
            (FakeOllamaServer.loaded.add if body["keep_alive"] else FakeOllamaServer.loaded.discard)(body["model"])
        self._send({})

    def do_DELETE(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllamaServer.models.remove(body["model"])
        self._send({})

    _send = FakeOllama._send
    log_message = FakeOllama.log_message


def test_ollama_models_are_downloaded_run_and_stopped_from_settings(tmp_path, monkeypatch):
    no_ai_on_path(tmp_path, monkeypatch)
    FakeOllamaServer.models, FakeOllamaServer.loaded, FakeOllamaServer.asked = ["tiny:1b"], set(), []
    running = HTTPServer(("127.0.0.1", 0), FakeOllamaServer)
    threading.Thread(target=running.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{running.server_port}")
    try:
        api = client()
        state = api.get("/api/ollama").json()
        assert state["running"] and state["version"] == "0.9.0" and not state["started_here"]
        assert [m["name"] for m in state["models"]] == ["tiny:1b"] and not state["models"][0]["loaded"]

        state = api.post("/api/ollama/load", json={"name": "tiny:1b"}).json()
        assert state["models"][0]["loaded"] is True
        assert FakeOllamaServer.asked[-1] == ("/api/generate", {"model": "tiny:1b", "keep_alive": "30m"})
        assert api.post("/api/ollama/unload", json={"name": "tiny:1b"}).json()["models"][0]["loaded"] is False

        api.post("/api/ollama/pull", json={"name": "qwen2.5:3b"})
        api.post("/api/ollama/pull", json={"name": "missing:1b"})
        for _ in range(50):
            pulls = {p["name"]: p for p in api.get("/api/ollama").json()["pulls"]}
            if all(p["done"] for p in pulls.values()) and len(pulls) == 2:
                break
            time.sleep(0.05)
        assert pulls["qwen2.5:3b"]["error"] is None and pulls["qwen2.5:3b"]["status"] == "success"
        assert pulls["qwen2.5:3b"]["completed"] == 100 == pulls["qwen2.5:3b"]["total"]
        assert pulls["missing:1b"]["error"] == "pull model manifest: file does not exist"
        assert "qwen2.5:3b" in [m["name"] for m in api.get("/api/ollama").json()["models"]]

        assert [m["name"] for m in api.post("/api/ollama/remove", json={"name": "qwen2.5:3b"}).json()["models"]] == ["tiny:1b"]
        refused = api.post("/api/ollama/pull", json={"name": "../../etc; rm"})
        assert refused.status_code == 400 and "isn't a model name" in refused.json()["detail"]
    finally:
        running.shutdown()


def test_ollama_started_from_looma_can_be_stopped_from_it(tmp_path, monkeypatch):
    folder = no_ai_on_path(tmp_path, monkeypatch)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{port}")
    server = tmp_path / "serve.py"
    server.write_text(f"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({{"version": "0.9.0", "models": []}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", {port}), H).serve_forever()
""")
    fake(folder, "ollama", f'[ "$1" = serve ] && exec {sys.executable} {server}\nexit 1\n')
    api = client()
    assert api.get("/api/ollama").json()["running"] is False
    state = api.post("/api/ollama/start", json={}).json()
    assert state["running"] and state["started_here"]
    state = api.post("/api/ollama/stop", json={}).json()
    assert not state["running"] and not state["started_here"]


def test_ollama_run_by_its_linux_service_is_stopped_and_started_through_it(tmp_path, monkeypatch):
    """Ollama's Linux installer runs it as a systemd service: Stop and Start go through
    systemctl, with the desktop's password window (pkexec), never a terminal."""
    from looma import providers
    folder = no_ai_on_path(tmp_path, monkeypatch)
    calls = tmp_path / "calls"
    fake(folder, "systemctl", 'case "$1" in show) echo loaded;; is-active) echo active;; esac\n')
    fake(folder, "pkexec", f'echo "$@" >> {calls}\nexit "${{PKEXEC_EXIT:-0}}"\n')
    fake(folder, "ollama", "exit 0\n")
    monkeypatch.setattr(providers, "_wait_for", lambda running, seconds=10: True)
    monkeypatch.setattr(providers, "_ollama_running", lambda: True)
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    monkeypatch.setattr(providers.os, "geteuid", lambda: 1000)
    assert providers.ollama_manager() == "service" and providers.needs_admin()

    # Never without the person's OK on the page: no password window, nothing run.
    with pytest.raises(providers.NeedsPermission):
        providers.stop_ollama()
    assert not calls.exists()
    refused = client().post("/api/ollama/stop", json={})
    assert refused.status_code == 409 and "needs your OK" in refused.json()["detail"] and not calls.exists()

    assert client().post("/api/ollama/stop", json={"confirm": True}).status_code == 200
    assert calls.read_text().splitlines() == ["systemctl stop ollama"]

    monkeypatch.setattr(providers, "_ollama_running", lambda: False)
    providers.start_ollama(confirmed=True)
    assert calls.read_text().splitlines()[-1] == "systemctl start ollama"

    # The password window closed without a password.
    monkeypatch.setenv("PKEXEC_EXIT", "126")
    with pytest.raises(providers.ProviderError, match="password window was closed"):
        providers.start_ollama(confirmed=True)
    # No way to ask for a password here: the exact command to run instead.
    (folder / "pkexec").unlink()
    with pytest.raises(providers.ProviderError, match="run: sudo systemctl start ollama"):
        providers.start_ollama(confirmed=True)


def test_ollama_run_by_hand_is_stopped_with_pkill(tmp_path, monkeypatch):
    from looma import providers
    folder = no_ai_on_path(tmp_path, monkeypatch)
    calls = tmp_path / "calls"
    fake(folder, "systemctl", "echo not-found\n")
    fake(folder, "pkill", f'echo "$@" >> {calls}\n')
    fake(folder, "ollama", "exit 0\n")
    running = iter([True, True, False, False])
    monkeypatch.setattr(providers, "_ollama_running", lambda: next(running, False))
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    assert providers.ollama_manager() == "command"
    providers.stop_ollama()
    assert calls.read_text().strip() == "-x ollama"


def test_an_ollama_on_another_computer_is_not_started_or_stopped_from_here(monkeypatch):
    from looma import providers
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box:11434")
    monkeypatch.setattr(providers, "_ollama_running", lambda: True)
    with pytest.raises(providers.ProviderError, match="on another computer"):
        providers.stop_ollama()


def test_the_usage_chart_reads_the_gpu_and_how_much_of_each_model_is_on_it(tmp_path, monkeypatch):
    folder = no_ai_on_path(tmp_path, monkeypatch)
    fake(folder, "nvidia-smi", "echo 'NVIDIA GeForce RTX 4060 Laptop GPU, 37, 2048, 8188'\n")
    FakeOllamaServer.models, FakeOllamaServer.loaded = ["tiny:1b"], set()
    running = HTTPServer(("127.0.0.1", 0), FakeOllamaServer)
    threading.Thread(target=running.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{running.server_port}")
    monkeypatch.setattr(FakeOllamaServer, "ps_sizes", {"tiny:1b": (2_000_000_000, 1_000_000_000)}, raising=False)
    try:
        FakeOllamaServer.loaded.add("tiny:1b")
        usage = client().get("/api/ollama/usage").json()
    finally:
        running.shutdown()
    assert usage["gpus"] == [{"name": "NVIDIA GeForce RTX 4060 Laptop GPU", "percent": 37.0,
                              "memory_used": 2048 * 2**20, "memory_total": 8188 * 2**20}]
    assert usage["note"] is None and usage["cores"] >= 1
    assert usage["models"] == [{"name": "tiny:1b", "size": 2_000_000_000, "on_gpu": 0.5}]


def test_with_no_gpu_the_usage_chart_shows_the_cpu(tmp_path, monkeypatch):
    from looma import providers
    no_ai_on_path(tmp_path, monkeypatch)
    monkeypatch.setattr(providers, "_amd_gpus", lambda: [])
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    usage = client().get("/api/ollama/usage").json()
    assert usage["gpus"] == [] and "shows the CPU" in usage["note"]
    assert 0 <= usage["cpu"] <= 100 and usage["memory"]["total"] > usage["memory"]["used"] > 0


def test_the_suggested_model_follows_the_cpu_memory_and_gpu(monkeypatch):
    from looma import providers
    computer = {"cpu": "Core i7", "threads": 8, "memory_gb": 15.5, "gpu": "GTX 1650", "gpu_memory_gb": 4.0}

    def advice(**changes):
        monkeypatch.setattr(providers, "_hardware", lambda: {**computer, **changes})
        return providers.ollama_advice()

    # A 4 GB GPU holds the small model; the 7b would spill over to the CPU and crawl.
    small_gpu = advice()
    assert small_gpu["recommended"] == "qwen2.5:3b" and "GTX 1650" in small_gpu["reason"]
    assert {m["name"]: m["fit"] for m in small_gpu["models"]}["qwen2.5-coder:7b"] == "mixed"
    assert advice(gpu_memory_gb=8.0)["recommended"] == "qwen2.5-coder:7b"
    assert advice(gpu_memory_gb=24.0)["recommended"] == "qwen2.5-coder:32b"
    # With no GPU, plenty of memory doesn't make a big model quick enough.
    assert advice(gpu=None, gpu_memory_gb=None, memory_gb=64.0)["recommended"] == "qwen2.5:3b"
    assert advice(gpu=None, gpu_memory_gb=None, memory_gb=4.5)["recommended"] == "qwen2.5:1.5b"
    assert advice(gpu=None, gpu_memory_gb=None, memory_gb=3.0)["recommended"] == "qwen2.5:1.5b"
    assert advice(gpu=None, gpu_memory_gb=None, memory_gb=None)["recommended"] == "qwen2.5:3b"


def test_with_no_model_picked_the_best_one_here_is_used(monkeypatch):
    from looma import providers
    gb = 10**9
    monkeypatch.setattr(providers, "_hardware", lambda: {"cpu": None, "threads": 8, "memory_gb": 15.5,
                                                         "gpu": "GTX 1650", "gpu_memory_gb": 4.0})
    assert providers._best_installed({"qwen2.5:1.5b": gb, "qwen2.5:3b": 2 * gb, "big:13b": 8 * gb}) == "qwen2.5:3b"
    assert providers._best_installed({"tiny:1b": gb, "mid:2b": 2 * gb, "big:13b": 8 * gb}) == "mid:2b"
    assert providers._best_installed({"big:13b": 8 * gb, "bigger:30b": 20 * gb}) == "big:13b"
