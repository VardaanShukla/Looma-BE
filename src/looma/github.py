"""Your GitHub account, connected through Looma's GitHub App with GitHub's device flow:
Looma shows a short code, you enter it on github.com, and GitHub hands Looma a token.
No password or client secret passes through Looma, so it works with no server of its own.

The app asks for read-only access (contents, metadata, pull requests) and only to the
repositories you install it on, so you choose what Looma can see. Tokens last eight hours
and are refreshed with the refresh token, which a device-flow token can do without a secret.
The token is kept in Looma's local database, never sent to the page.

Until Looma's app is registered (or for people who'd rather not use it), there are two other
ways in: the sign-in of GitHub's own `gh` tool, which Looma starts and then borrows the token
of, and a token pasted from github.com/settings/tokens. Both see every repository the account
can, and gh's token can also write to them, which the screen says.

With it Looma lists your repositories (to clone one as a project) and shows a project's
open pull requests. Fetching and everything else still use your own git login."""

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from looma.store import connect

# Looma's GitHub App: its client ID (not a secret) and its name in github.com/apps/<name>.
# Set them here once the app is registered, or with these environment variables.
CLIENT_ID = os.environ.get("LOOMA_GITHUB_CLIENT_ID", "")
APP_NAME = os.environ.get("LOOMA_GITHUB_APP", "")
WEB = os.environ.get("LOOMA_GITHUB_URL", "https://github.com").rstrip("/")
API = os.environ.get("LOOMA_GITHUB_API", "https://api.github.com").rstrip("/")
SETTING = "github"
TIMEOUT = 20
# A token this close to running out is refreshed first.
REFRESH_EARLY = 300
MAX_REPOS = 500
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# The sign-in under way: GitHub's device code, never shown to the page, and when to ask next;
# or, signing in through gh, its process.
_pending: dict = {}
GH_CODE = re.compile(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
GH_TOKEN_LINE = re.compile(r"Token:\s*([A-Za-z0-9_]{20,})")
GH_URL = re.compile(r"https://\S+/login/device\S*")


class GitHubError(RuntimeError):
    def __init__(self, message: str, signed_out: bool = False):
        super().__init__(message)
        self.signed_out = signed_out


def configured() -> bool:
    return bool(CLIENT_ID)


def status() -> dict:
    """Whether Looma's GitHub App is set up and an account connected, how and as whom,
    and which ways of signing in this computer has."""
    saved = _saved()
    pending = _pending if _pending and _pending["expires_at"] > time.time() and _pending.get("user_code") else None
    return {
        "configured": configured(),
        "gh": _find_gh() is not None,
        "connected": saved is not None,
        "via": saved.get("source", "app") if saved else None,
        "login": saved.get("login") if saved else None,
        "name": saved.get("name") if saved else None,
        "avatar": saved.get("avatar") if saved else None,
        "install_url": f"{WEB}/apps/{APP_NAME}/installations/new" if APP_NAME else None,
        "code": pending["user_code"] if pending else None,
        "verify_url": pending["verification_uri"] if pending else None,
        "expires_at": int(pending["expires_at"]) if pending else None,
    }


def start(method: str = "app") -> dict:
    """Asks GitHub for a code to enter on github.com/login/device: through Looma's app, or
    through gh, which is used straight away when it's already signed in."""
    if method == "gh":
        return _start_gh()
    if not configured():
        raise GitHubError("Looma's GitHub App isn't set up yet: set LOOMA_GITHUB_CLIENT_ID.")
    found = _form(f"{WEB}/login/device/code", {"client_id": CLIENT_ID})
    if "device_code" not in found:
        raise GitHubError(f"GitHub didn't give a sign-in code: {found.get('error_description') or found.get('error') or 'no reason given'}")
    _pending.clear()
    _pending.update(device_code=found["device_code"], user_code=found["user_code"],
                    verification_uri=found.get("verification_uri") or f"{WEB}/login/device",
                    interval=int(found.get("interval", 5)), next_at=time.time(),
                    expires_at=time.time() + int(found.get("expires_in", 900)))
    return status()


def poll() -> dict:
    """Checks once whether the code was entered, no more often than GitHub allows. The page
    calls this every few seconds while it shows the code."""
    if not _pending:
        return status()
    if _pending.get("source") == "gh":
        return _poll_gh()
    now = time.time()
    if now > _pending["expires_at"]:
        _pending.clear()
        raise GitHubError("The code ran out before it was entered. Connect again for a new one.")
    if now < _pending["next_at"]:
        return status()
    found = _form(f"{WEB}/login/oauth/access_token",
                  {"client_id": CLIENT_ID, "device_code": _pending["device_code"], "grant_type": DEVICE_GRANT})
    error = found.get("error")
    if error == "authorization_pending":
        _pending["next_at"] = now + _pending["interval"]
        return status()
    if error == "slow_down":
        _pending["interval"] = int(found.get("interval", _pending["interval"] + 5))
        _pending["next_at"] = now + _pending["interval"]
        return status()
    _pending.clear()
    if error == "access_denied":
        raise GitHubError("The sign-in was cancelled on GitHub.")
    if error or "access_token" not in found:
        raise GitHubError(f"GitHub refused the sign-in: {found.get('error_description') or error or 'no token came back'}")
    tokens = _tokens(found)
    _save({**tokens, "source": "app", **_who(_call("/user", bearer=tokens["token"]))})
    return status()


def use_token(pasted: str) -> dict:
    """Connects with a token made on github.com/settings/tokens, once GitHub accepts it."""
    pasted = pasted.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{20,255}", pasted):
        raise GitHubError("That doesn't look like a GitHub token. They start with github_pat_ or ghp_.")
    try:
        user = _call("/user", bearer=pasted, forget=False)
    except GitHubError:
        raise GitHubError("GitHub didn't accept that token. Check it hasn't expired, and copy it again.")
    _pending.clear()
    _save({"token": pasted, "source": "token", "expires_at": None, **_who(user)})
    return status()


def _find_gh() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    places = [Path("/opt/homebrew/bin/gh"), Path("/usr/local/bin/gh"), Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "GitHub CLI" / "gh.exe"]
    return next((str(p) for p in places if p.is_file()), None)


def _gh(*args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run([_find_gh() or "gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL, env={**os.environ, "GH_PROMPT_DISABLED": "1"})


def _gh_token() -> str | None:
    """gh's token for github.com, which gh keeps fresh itself; None when it isn't signed in.
    gh before 2.17 has no `auth token`: its status shows the token instead."""
    try:
        result = _gh("auth", "token", "--hostname", "github.com")
        found = result.stdout.strip()
        if result.returncode == 0 and found:
            return found
        result = _gh("auth", "status", "--show-token", "--hostname", "github.com")
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Older gh writes its status to stderr.
    shown = GH_TOKEN_LINE.search(result.stdout + result.stderr)
    return shown.group(1) if shown and result.returncode == 0 else None


def _start_gh() -> dict:
    if _find_gh() is None:
        raise GitHubError("GitHub's gh tool isn't installed on this computer. Paste a token instead, or install gh "
                          "from cli.github.com.")
    if _gh_token():
        return _finish_gh()
    previous = _pending.get("process")
    if previous is not None and previous.poll() is None:
        previous.terminate()
    _pending.clear()
    process = subprocess.Popen([_find_gh(), "auth", "login", "--web", "--hostname", "github.com"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", env={**os.environ, "GH_PROMPT_DISABLED": "1"})
    _pending.update(source="gh", process=process, output=[], user_code=None,
                    verification_uri=f"{WEB}/login/device", expires_at=time.time() + 900)
    threading.Thread(target=_read_gh, args=(process, _pending["output"]), daemon=True).start()
    # gh may wait for Enter before it goes on to wait for the browser.
    try:
        process.stdin.write("\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    deadline = time.monotonic() + 15
    while _pending.get("user_code") is None and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pending.get("user_code") is None:
        return _poll_gh()
    return status()


def _read_gh(process: subprocess.Popen, output: list):
    for line in process.stdout:
        output.append(line.strip())
        code, url = GH_CODE.search(line), GH_URL.search(line)
        if code and _pending.get("process") is process:
            _pending["user_code"] = code.group(1)
        if url and _pending.get("process") is process:
            _pending["verification_uri"] = url.group(0)


def _poll_gh() -> dict:
    process = _pending["process"]
    if process.poll() is None:
        if time.time() > _pending["expires_at"]:
            process.terminate()
            _pending.clear()
            raise GitHubError("The code ran out before it was entered. Sign in again for a new one.")
        return status()
    lines = [line for line in _pending["output"] if line and not GH_CODE.search(line)]
    _pending.clear()
    if process.returncode != 0:
        raise GitHubError(f"gh couldn't sign in: {_gh_reason(lines)}")
    return _finish_gh()


def _gh_reason(lines: list[str]) -> str:
    """What gh said went wrong: the line naming the problem, not the help text gh prints
    after it (a flag this gh doesn't know prints its whole usage)."""
    usage = next((i for i, line in enumerate(lines) if line.startswith("Usage")), None)
    if usage is not None:
        lines = lines[:usage]
    said = next((line for line in lines if re.search(r"unknown|error|failed|not |must|denied|cancel", line, re.I)), None)
    return said or (lines[-1] if lines else "no reason given")


def _finish_gh() -> dict:
    found = _gh_token()
    if not found:
        raise GitHubError("gh signed in but gave no token. Run gh auth status in a terminal to see why.")
    _pending.clear()
    _save({"source": "gh", **_who(_call("/user", bearer=found, forget=False))})
    return status()


def _who(user: dict) -> dict:
    return {"login": user.get("login"), "name": user.get("name"), "avatar": user.get("avatar_url")}


def disconnect() -> dict:
    """Forgets the token here. GitHub still lists Looma under your authorized apps until
    you revoke it there (Settings → Applications); signing in through gh leaves gh signed in."""
    process = _pending.get("process")
    if process is not None and process.poll() is None:
        process.terminate()
    _pending.clear()
    conn = connect()
    with conn:
        conn.execute("DELETE FROM settings WHERE name = ?", (SETTING,))
    conn.close()
    return status()


def repos(tracked: dict[str, str] | None = None) -> list[dict]:
    """Every repository the app is installed on for you, newest activity first. `tracked`
    maps owner/name to the folder of a Looma project that is a clone of it."""
    tracked = {k.lower(): v for k, v in (tracked or {}).items()}
    found = []
    if (_saved() or {}).get("source", "app") == "app":
        listed = [repo for installation in _pages("/user/installations", "installations")
                  for repo in _pages(f"/user/installations/{installation['id']}/repositories", "repositories")]
    else:
        # A gh or pasted token isn't limited to the app's installations: every repository the account can see.
        listed = _pages("/user/repos?sort=pushed&affiliation=owner,collaborator,organization_member", None)
    for repo in listed[:MAX_REPOS]:
        found.append({
            "full_name": repo["full_name"],
            "private": bool(repo.get("private")),
            "description": repo.get("description"),
            "default_branch": repo.get("default_branch"),
            "url": repo.get("html_url"),
            "clone_url": repo.get("clone_url"),
            "updated_at": repo.get("pushed_at") or repo.get("updated_at"),
            "tracked": tracked.get(repo["full_name"].lower()),
        })
    unique = {r["full_name"]: r for r in found}
    return sorted(unique.values(), key=lambda r: r["updated_at"] or "", reverse=True)


def clone(full_name: str, folder: Path) -> Path:
    """Clones the repository into `folder`/<its name> and returns where. The token goes to
    git through the environment for this one command, so it is never written into the
    clone's settings or shown in the list of running programs."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name):
        raise GitHubError("That isn't a GitHub repository name (owner/name).")
    target = folder / full_name.split("/", 1)[1]
    if target.exists():
        raise GitHubError(f"{target} already exists. Pick another folder, or add that one as a project.")
    folder.mkdir(parents=True, exist_ok=True)
    basic = base64.b64encode(f"x-access-token:{token()}".encode()).decode()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": f"http.{WEB}/.extraHeader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
    try:
        result = subprocess.run(["git", "clone", "--quiet", f"{WEB}/{full_name}.git", str(target)], env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                                stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise GitHubError("The clone took more than ten minutes and was stopped.")
    if result.returncode != 0:
        lines = [line for line in result.stderr.replace(basic, "[token]").splitlines() if line.strip()]
        raise GitHubError(f"git couldn't clone {full_name}: {lines[-1] if lines else 'no reason given'}")
    return target


def pulls(full_name: str) -> list[dict]:
    """The repository's open pull requests, newest first."""
    found = _call(f"/repos/{full_name}/pulls?state=open&sort=updated&direction=desc&per_page=30")
    return [{
        "number": p["number"],
        "title": p["title"],
        "author": (p.get("user") or {}).get("login"),
        "branch": (p.get("head") or {}).get("ref"),
        "base": (p.get("base") or {}).get("ref"),
        "draft": bool(p.get("draft")),
        "url": p["html_url"],
        "updated_at": p.get("updated_at"),
    } for p in found if isinstance(p, dict)]


_REMOTE = re.compile(r"^(?:https?://(?:[^@/]+@)?|ssh://git@|git@)github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def remote_name(repo: Path) -> str | None:
    """owner/name of the project's GitHub remote (origin first), or None when it has none."""
    try:
        out = subprocess.run(["git", "remote", "-v"], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    remotes = [line.split()[:2] for line in out.splitlines() if len(line.split()) >= 2]
    remotes.sort(key=lambda r: r[0] != "origin")
    for _, url in remotes:
        found = _REMOTE.match(url.strip())
        if found:
            return f"{found.group(1)}/{found.group(2)}"
    return None


def token() -> str:
    """A token that works now, refreshed first when it's about to run out."""
    saved = _saved()
    if saved is None:
        raise GitHubError("Connect your GitHub account first (Settings → GitHub).", signed_out=True)
    if saved.get("source") == "gh":
        found = _gh_token()
        if not found:
            disconnect()
            raise GitHubError("gh is no longer signed in to GitHub. Sign in again.", signed_out=True)
        return found
    expires = saved.get("expires_at")
    if expires and expires - REFRESH_EARLY < time.time():
        if not saved.get("refresh") or (saved.get("refresh_expires_at") or 0) < time.time():
            disconnect()
            raise GitHubError("Your GitHub sign-in ran out. Connect again.", signed_out=True)
        found = _form(f"{WEB}/login/oauth/access_token",
                      {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": saved["refresh"]})
        if "access_token" not in found:
            disconnect()
            raise GitHubError("GitHub didn't renew the sign-in. Connect again.", signed_out=True)
        saved = {**saved, **_tokens(found)}
        _save(saved)
    return saved["token"]


def _tokens(found: dict) -> dict:
    now = time.time()
    return {"token": found["access_token"],
            "expires_at": now + int(found["expires_in"]) if found.get("expires_in") else None,
            "refresh": found.get("refresh_token"),
            "refresh_expires_at": now + int(found["refresh_token_expires_in"]) if found.get("refresh_token_expires_in") else None}


def _saved() -> dict | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (SETTING,)).fetchone()
    conn.close()
    try:
        saved = json.loads(row["value"]) if row else None
    except json.JSONDecodeError:
        return None
    return saved if isinstance(saved, dict) and (saved.get("token") or saved.get("source") == "gh") else None


def _save(saved: dict) -> None:
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (SETTING, json.dumps(saved)))
    conn.close()


def _pages(path: str, key: str | None, limit: int = 5) -> list[dict]:
    items = []
    for page in range(1, limit + 1):
        found = _call(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        batch = (found.get(key, []) if isinstance(found, dict) else []) if key else (found if isinstance(found, list) else [])
        items += batch
        if len(batch) < 100:
            break
    return items


def _form(url: str, fields: dict) -> dict:
    """A form POST to github.com's sign-in endpoints, answered as JSON. Errors such as
    authorization_pending come back in the body, so they are returned, not raised."""
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                     headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except json.JSONDecodeError:
            raise GitHubError(f"GitHub answered {error.code}.")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise GitHubError(f"Couldn't reach GitHub ({getattr(error, 'reason', error)}).")


def _call(path: str, bearer: str | None = None, forget: bool = True):
    request = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {bearer or token()}", "User-Agent": "Looma"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            if not forget:
                raise GitHubError("GitHub didn't accept the token.", signed_out=True)
            disconnect()
            raise GitHubError("GitHub no longer accepts Looma's sign-in. Connect again.", signed_out=True)
        if error.code in (403, 404):
            if (_saved() or {}).get("source", "app") != "app":
                raise GitHubError("GitHub says this account can't see that, or it doesn't exist.")
            raise GitHubError("Looma's GitHub App can't see this repository. Install it on the repository "
                              "(Settings → GitHub → Pick repositories).")
        raise GitHubError(f"GitHub answered {error.code}.")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise GitHubError(f"Couldn't reach GitHub ({getattr(error, 'reason', error)}).")
