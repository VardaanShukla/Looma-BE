import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

from conftest import git
from looma import github
from looma.api.app import create_app


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


class FakeGitHub:
    """github.com's device sign-in and the parts of its API Looma reads."""

    def __init__(self):
        self.entered = False  # the person has typed the code on GitHub
        self.forms = []
        self.headers = []
        self.token = "ghu_first"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body, code=200):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                fake.forms.append((self.path, form))
                if self.path == "/login/device/code":
                    return self._send({"device_code": "dev-123", "user_code": "ABCD-1234", "interval": 0,
                                       "verification_uri": "https://github.com/login/device", "expires_in": 900})
                if form.get("grant_type") == "refresh_token":
                    fake.token = "ghu_second"
                    return self._send({"access_token": fake.token, "expires_in": 28800, "refresh_token": "ghr_2",
                                       "refresh_token_expires_in": 15897600})
                if not fake.entered:
                    return self._send({"error": "authorization_pending"})
                return self._send({"access_token": fake.token, "expires_in": 28800, "refresh_token": "ghr_1",
                                   "refresh_token_expires_in": 15897600})

            def do_GET(self):
                fake.headers.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._send({"message": "Bad credentials"}, 401)
                if self.path == "/user":
                    return self._send({"login": "vardaan", "name": "Vardaan", "avatar_url": "https://a/v.png"})
                if self.path.startswith("/user/installations?"):
                    return self._send({"installations": [{"id": 7}]})
                if self.path.startswith("/user/installations/7/repositories"):
                    return self._send({"repositories": [
                        {"full_name": "vardaan/looma", "private": True, "description": "It", "default_branch": "main",
                         "html_url": "https://github.com/vardaan/looma", "clone_url": "x", "pushed_at": "2026-10-27T10:00:00Z"},
                        {"full_name": "vardaan/old", "private": False, "html_url": "u", "pushed_at": "2025-01-01T00:00:00Z"}]})
                if self.path.startswith("/user/repos?"):
                    return self._send([{"full_name": "vardaan/everything", "private": False, "html_url": "u",
                                        "pushed_at": "2026-10-01T00:00:00Z"}])
                if self.path.startswith("/repos/vardaan/looma/pulls"):
                    return self._send([{"number": 4, "title": "Add Ask", "user": {"login": "vardaan"}, "draft": False,
                                        "head": {"ref": "ask"}, "base": {"ref": "main"},
                                        "html_url": "https://github.com/vardaan/looma/pull/4", "updated_at": "now"}])
                return self._send({"message": "Not Found"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake(monkeypatch):
    server = FakeGitHub()
    monkeypatch.setattr(github, "CLIENT_ID", "Iv1.test")
    monkeypatch.setattr(github, "APP_NAME", "looma-test")
    monkeypatch.setattr(github, "WEB", server.url)
    monkeypatch.setattr(github, "API", server.url)
    github._pending.clear()
    yield server
    server.server.shutdown()


def test_connecting_with_a_code_then_listing_repos_and_pull_requests(fake, repo):
    api = client()
    assert api.get("/api/github").json()["connected"] is False
    started = api.post("/api/github/connect", json={}).json()
    # The page shows the code; the device code itself never leaves Looma.
    assert started["code"] == "ABCD-1234" and started["verify_url"] == "https://github.com/login/device"
    assert "dev-123" not in json.dumps(started)
    assert api.post("/api/github/poll", json={}).json()["connected"] is False  # not entered yet

    fake.entered = True
    done = api.post("/api/github/poll", json={}).json()
    assert (done["connected"], done["login"], done["code"]) == (True, "vardaan", None)
    assert done["install_url"].endswith("/apps/looma-test/installations/new")
    assert all(form.get("client_id") == "Iv1.test" and "client_secret" not in form for _, form in fake.forms)

    # A project cloned from one of them is marked as such; newest activity first.
    git(repo, "remote", "add", "origin", "git@github.com:vardaan/looma.git")
    api.post("/api/repos", json={"path": str(repo)})
    listed = api.get("/api/github/repos").json()
    assert [r["full_name"] for r in listed] == ["vardaan/looma", "vardaan/old"]
    assert listed[0]["tracked"] == str(repo) and listed[1]["tracked"] is None

    [project] = api.get("/api/repos").json()
    pulls = api.get(f"/api/repos/{project['id']}/pulls").json()
    assert pulls["repo"] == "vardaan/looma" and pulls["pulls"][0]["title"] == "Add Ask"
    assert pulls["pulls"][0]["branch"] == "ask"

    assert api.request("DELETE", "/api/github", json={}).json()["connected"] is False
    assert api.get("/api/github/repos").status_code == 409


def test_a_token_about_to_run_out_is_refreshed_without_a_secret(fake):
    github._save({"token": "ghu_first", "expires_at": time.time() + 10, "refresh": "ghr_1",
                  "refresh_expires_at": time.time() + 1000, "login": "vardaan"})
    assert github.token() == "ghu_second"
    path, form = fake.forms[-1]
    assert form == {"client_id": "Iv1.test", "grant_type": "refresh_token", "refresh_token": "ghr_1"}
    assert github._saved()["refresh"] == "ghr_2"


def test_a_refused_token_disconnects(fake):
    github._save({"token": "ghu_revoked", "expires_at": None, "login": "vardaan"})
    with pytest.raises(github.GitHubError) as error:
        github.repos()
    assert error.value.signed_out and github.status()["connected"] is False


def test_not_set_up_yet_says_so(monkeypatch):
    monkeypatch.setattr(github, "CLIENT_ID", "")
    api = client()
    assert api.get("/api/github").json()["configured"] is False
    failed = api.post("/api/github/connect", json={})
    assert failed.status_code == 502 and "LOOMA_GITHUB_CLIENT_ID" in failed.json()["detail"]


def test_cloning_adds_the_project_and_keeps_the_token_out_of_the_clone(repo, tmp_path, monkeypatch):
    # GitHub stood in for by a folder of bare repositories.
    hosted = tmp_path / "hosted" / "vardaan"
    hosted.mkdir(parents=True)
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(hosted / "tool.git"))
    monkeypatch.setattr(github, "WEB", (tmp_path / "hosted").as_uri())
    github._save({"token": "ghu_secret_value", "expires_at": None, "login": "vardaan"})

    made = client().post("/api/github/clone", json={"full_name": "vardaan/tool", "folder": str(tmp_path / "code")})
    assert made.status_code == 201 and made.json()["name"] == "tool"
    cloned = tmp_path / "code" / "tool"
    assert (cloned / "app.py").is_file()
    assert "ghu_secret_value" not in (cloned / ".git" / "config").read_text()

    again = client().post("/api/github/clone", json={"full_name": "vardaan/tool", "folder": str(tmp_path / "code")})
    assert again.status_code == 400 and "already exists" in again.json()["detail"]
    bad = client().post("/api/github/clone", json={"full_name": "../etc", "folder": str(tmp_path)})
    assert bad.status_code == 400


def test_github_remotes_are_read_in_every_spelling(tmp_path):
    for url in ("https://github.com/vardaan/dev-dash.git", "git@github.com:vardaan/dev-dash.git",
                "ssh://git@github.com/vardaan/dev-dash", "https://token@github.com/vardaan/dev-dash/"):
        assert github._REMOTE.match(url).groups() == ("vardaan", "dev-dash"), url
    assert github._REMOTE.match("https://gitlab.com/vardaan/dev-dash.git") is None


def test_a_pasted_token_connects_and_lists_every_repository(fake):
    api = client()
    assert api.post("/api/github/token", json={"token": "not a token"}).status_code == 400
    refused = api.post("/api/github/token", json={"token": "github_pat_" + "x" * 30})
    assert refused.status_code == 400 and "didn't accept" in refused.json()["detail"]
    assert api.get("/api/github").json()["connected"] is False  # a refused token isn't kept

    fake.token = "github_pat_" + "y" * 30
    made = api.post("/api/github/token", json={"token": fake.token}).json()
    assert (made["connected"], made["via"], made["login"]) == (True, "token", "vardaan")
    assert fake.token not in json.dumps(made)
    # Not limited to the app's installations.
    assert [r["full_name"] for r in api.get("/api/github/repos").json()] == ["vardaan/everything"]


def test_signing_in_through_gh(fake, tmp_path, monkeypatch):
    folder = tmp_path / "ghbin"
    folder.mkdir()
    entered = tmp_path / "entered"
    gh = folder / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1 $2\" = 'auth token' ]; then [ -f {entered} ] && echo ghu_first && exit 0; exit 1; fi\n"
        "if [ \"$1 $2\" = 'auth login' ]; then\n"
        "  echo '! First copy your one-time code: WXYZ-1234'\n"
        "  echo 'Open this URL to continue in your web browser: https://github.com/login/device'\n"
        "  read enter\n"
        f"  while [ ! -f {entered} ]; do sleep 0.1; done; exit 0\n"
        "fi\n"
        "exit 1\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")
    api = client()
    assert api.get("/api/github").json()["gh"] is True
    started = api.post("/api/github/connect", json={"method": "gh"}).json()
    assert (started["code"], started["verify_url"], started["connected"]) == ("WXYZ-1234", "https://github.com/login/device", False)
    assert api.post("/api/github/poll", json={}).json()["connected"] is False

    entered.write_text("")
    for _ in range(50):
        done = api.post("/api/github/poll", json={}).json()
        if done["connected"]:
            break
        time.sleep(0.1)
    assert (done["connected"], done["via"], done["login"]) == (True, "gh", "vardaan")
    # gh keeps the token; Looma asks gh for it each time rather than keeping a copy.
    assert "ghu_first" not in json.dumps(github._saved())
    assert api.get("/api/repos").status_code == 200 and github.token() == "ghu_first"

    # Already signed in to gh: connected straight away, no code.
    api.request("DELETE", "/api/github", json={})
    again = api.post("/api/github/connect", json={"method": "gh"}).json()
    assert again["connected"] is True and again["code"] is None


def test_a_gh_failure_names_the_problem_not_its_help_text():
    printed = ["unknown flag: --web", "", "Usage:  gh auth login [flags]", "", "Flags:",
               "      --with-token          Read token from standard input"]
    assert github._gh_reason(printed) == "unknown flag: --web"
    assert github._gh_reason(["something odd"]) == "something odd"


def test_an_old_gh_without_auth_token_still_hands_over_its_token(tmp_path, monkeypatch):
    folder = tmp_path / "oldgh"
    folder.mkdir()
    gh = folder / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = 'auth token' ]; then echo 'unknown command \"token\" for \"gh auth\"' >&2; exit 1; fi\n"
        "if [ \"$1 $2\" = 'auth status' ]; then\n"
        "  echo 'github.com' >&2; echo '  ✓ Logged in to github.com as vardaan' >&2\n"
        "  echo '  ✓ Token: gho_0123456789abcdefghijABCDEFGHIJ' >&2; exit 0\n"
        "fi\n"
        "exit 1\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")
    assert github._gh_token() == "gho_0123456789abcdefghijABCDEFGHIJ"
