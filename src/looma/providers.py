"""The AIs besides Claude Code (explain.py) that can write Looma's explanations: ChatGPT's
Codex CLI, a local model served by Ollama, Google's Antigravity CLI, and a model on the
person's own server, connected by pasting the curl command that reaches it. Each says whether
it is installed and ready, how to install it (the official commands for this computer),
which models it offers and which is cheapest, and answers one prompt with text. Every run
is in an empty folder, read-only where the tool allows it, so nothing touches a project.

Recording changes is separate (capture/): Claude Code's, Codex's and Antigravity's edits
can be recorded, one tool per project."""

import base64
import functools
import glob
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from urllib.parse import urlsplit, urlunsplit

try:
    import psutil  # CPU and memory use on every system; without it, Linux and macOS are read directly
except ImportError:
    psutil = None

from looma import paths
from looma.capture.record import INTERNAL_ENV
from looma.store import connect

IDS = ("claude", "codex", "ollama", "antigravity", "hosted")
NAMES = {"claude": "Claude", "codex": "ChatGPT Codex", "ollama": "Local model (Ollama)", "antigravity": "Antigravity",
         "hosted": "Your own server"}
TIMEOUT = 240
# A small model that runs on most laptops, offered when the computer can't be read.
SMALL_LOCAL_MODEL = "qwen2.5:3b"
# Models to suggest, smallest first: name, Ollama's download size in GB, and what it's for.
LOCAL_MODELS = [
    ("qwen2.5:1.5b", 1.0, "The smallest. For a computer short on memory."),
    (SMALL_LOCAL_MODEL, 1.9, "Small and quick. Enough for explanations."),
    ("qwen2.5-coder:7b", 4.7, "Reads code better."),
    ("qwen2.5-coder:14b", 9.0, "Reads code better still, and takes a bigger GPU."),
    ("qwen2.5-coder:32b", 20.0, "The best at code here, for a large GPU."),
]
# A model takes about this much more in memory than its download, with the room NUM_CTX asks for.
MODEL_OVERHEAD_GB = 1.0
# Left for the system and other programs when a model runs in the computer's own memory.
SPARE_MEMORY_GB = 3.0
# Biggest download that still answers before TIMEOUT when it runs on the CPU alone.
MAX_CPU_MODEL_GB = 2.0
# Apple's chips share memory with the GPU, and macOS keeps about this much of it for the GPU.
APPLE_GPU_SHARE = 0.7
# Codex models when its own list (~/.codex/models_cache.json) isn't there yet, cheapest first.
CODEX_FALLBACK = [
    {"id": "gpt-6-luna", "label": "GPT-6 Luna", "note": "Most efficient: fewest tokens, good for explanations."},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "note": "Near the top model at a lower cost."},
    {"id": "gpt-6-astra", "label": "GPT-6 Astra", "note": "Most capable, uses the most."},
]
# Words in a model's name that mark the small, cheap ones.
CHEAP = re.compile(r"mini|nano|luna|flash|lite|small", re.IGNORECASE)


class ProviderError(RuntimeError):
    def __init__(self, message: str, signed_out: bool = False):
        super().__init__(message)
        self.signed_out = signed_out


def status(provider: str) -> dict:
    return {"codex": _codex_status, "ollama": _ollama_status, "antigravity": _agy_status,
            "hosted": _hosted_status}[provider]()


def ask(provider: str, model: str | None, system: str, prompt: str) -> str:
    """One answer from the provider, as text. With no model picked, the cheapest one it
    offers is used (tokens matter); Antigravity, which can't list them, uses its own choice."""
    model = model or default_model(provider)
    return {"codex": _codex_ask, "ollama": _ollama_ask, "antigravity": _agy_ask,
            "hosted": _hosted_ask}[provider](model or None, system, prompt)


def default_model(provider: str) -> str | None:
    if provider == "codex":
        return _cheapest(_codex_models())
    return None  # Ollama picks its smallest model when asked; Antigravity uses its own setting


def login_command(provider: str) -> list[str] | None:
    """The command that signs in from the dashboard (it opens the browser), when there is one."""
    if provider == "codex":
        path = _find_codex()
        if not path:
            return None
        return [path, "login"]
    return None


def _base(provider: str, available: bool, **fields) -> dict:
    return {"provider": provider, "name": NAMES[provider], "tool": provider, "available": available,
            "signed_in": None, "account": None, "detail": None, "install": [], "login": None,
            "login_command": None, "models": [], "default_model": None, "model": "", "server": None, **fields}


def _env() -> dict:
    # Looma's own runs are never recorded as someone's prompt.
    return {**os.environ, INTERNAL_ENV: "1"}


def _windows() -> bool:
    return platform.system() == "Windows"


def _first(*candidates) -> str | None:
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# ChatGPT Codex -------------------------------------------------------------------------

def _find_codex() -> str | None:
    """Codex on the PATH, where its installer puts it, or the copy bundled with the ChatGPT
    extension for VS Code (or Cursor), which many people already have."""
    found = shutil.which("codex")
    if found:
        return found
    exe = "codex.exe" if _windows() else "codex"
    home = Path.home()
    bundled = []
    for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
        bundled += glob.glob(str(home / editor / "extensions" / "openai.chatgpt-*" / "bin" / "*" / exe))
    bundled.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    npm = (Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "npm" / "codex.cmd" if _windows()
           else home / ".npm-global" / "bin" / "codex")
    return _first(home / ".local" / "bin" / exe, home / ".codex" / "bin" / exe, npm, *bundled)


def _codex_status() -> dict:
    path = _find_codex()
    if not path:
        install = ([{"shell": "PowerShell", "command": 'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"'}]
                   if _windows() else [{"shell": "Terminal", "command": "curl -fsSL https://chatgpt.com/codex/install.sh | sh"}])
        install.append({"shell": "Or with npm", "command": "npm install -g @openai/codex"})
        return _base("codex", False, detail="Install the Codex CLI", install=install, models=_codex_models(),
                     default_model=_cheapest(_codex_models()))
    signed_in, account = None, None
    try:
        result = subprocess.run([path, "login", "status"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20, env=_env())
        signed_in = result.returncode == 0
        said = (result.stdout + result.stderr).strip()
        account = ("ChatGPT account" if "chatgpt" in said.lower() else "API key" if "api key" in said.lower() else None) \
            if signed_in else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    models = _codex_models()
    return _base("codex", True, signed_in=signed_in, account=account, login="button", login_command="codex login",
                 models=models, default_model=_cheapest(models))


def _codex_models() -> list[dict]:
    """The models Codex lists for this account, from its own cache, cheapest first."""
    try:
        cache = json.loads((Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CODEX_FALLBACK
    models = []
    for item in sorted(cache.get("models", []), key=lambda m: m.get("priority", 99)):
        if not isinstance(item, dict) or not item.get("slug") or item.get("visibility") == "hide":
            continue
        note = (item.get("description") or "").split(". ")[0].strip()
        models.append({"id": item["slug"], "label": item.get("display_name") or item["slug"], "note": note})
    models.sort(key=lambda m: not CHEAP.search(m["id"]))
    return models or CODEX_FALLBACK


def _codex_ask(model: str | None, system: str, prompt: str) -> str:
    path = _find_codex()
    if not path:
        raise ProviderError("The Codex CLI isn't installed on this computer.")
    with tempfile.TemporaryDirectory(prefix="looma-explain-") as folder:
        answer = Path(folder) / "answer.txt"
        command = [path, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--color", "never",
                   "--output-last-message", str(answer)]
        if model:
            command += ["--model", model]
        # "-" reads the prompt from stdin, which has no length limit (Windows command lines do).
        try:
            result = subprocess.run([*command, "-"], input=f"{system}\n\n{prompt}", cwd=folder, env=_env(),
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ProviderError("Codex took too long to answer. Try again.")
        text = answer.read_text(encoding="utf-8", errors="replace") if answer.is_file() else ""
    if result.returncode != 0 or not text.strip():
        said = [line for line in (result.stderr + result.stdout).splitlines() if line.strip()]
        if _codex_status().get("signed_in") is False or re.search(r"log ?in|unauthori[sz]ed|401", " ".join(said[-3:]), re.I):
            raise ProviderError("Codex isn't signed in. Sign in with your ChatGPT account to get explanations.",
                                signed_out=True)
        raise ProviderError(f"Codex: {said[-1] if said else 'it gave no answer'}")
    return text


# Local models (Ollama) ---------------------------------------------------------------

def _ollama_url(path: str) -> str:
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip().rstrip("/")
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0"):]
    return (host if "://" in host else f"http://{host}") + path


def _ollama_get(path: str, timeout: float = 3):
    with urllib.request.urlopen(_ollama_url(path), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _ollama_status() -> dict:
    try:
        tags = _ollama_get("/api/tags")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        if shutil.which("ollama") or _first(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"):
            return _base("ollama", False, detail="Start Ollama",
                         install=[{"shell": "Terminal", "command": "ollama serve"}],
                         login="none", login_command="Or open the Ollama app.")
        install = ([{"shell": "Download", "command": "https://ollama.com/download/windows"}] if _windows() else
                   [{"shell": "Download", "command": "https://ollama.com/download/mac"}] if platform.system() == "Darwin" else
                   [{"shell": "Terminal", "command": "curl -fsSL https://ollama.com/install.sh | sh"}])
        return _base("ollama", False, detail="Install Ollama", install=install, login="none")
    models = sorted(
        ({"id": m["name"], "label": m["name"], "size": m.get("size", 0),
          "note": " · ".join(x for x in (_size(m.get("size")), (m.get("details") or {}).get("parameter_size")) if x)}
         for m in tags.get("models", []) if isinstance(m, dict) and m.get("name")),
        key=lambda m: m["size"])
    sizes = {m["id"]: m.pop("size") for m in models}
    if not models:
        return _base("ollama", False, detail="Download a model",
                     install=[{"shell": "Terminal", "command": f"ollama pull {ollama_advice()['recommended']}"}], login="none")
    # Local models need no account, and cost no tokens.
    return _base("ollama", True, signed_in=True, account="On this computer", login="none", models=models,
                 default_model=_best_installed(sizes))


@functools.cache
def _hardware() -> dict:
    """The computer's CPU, memory and biggest GPU, in GB. Read once: it doesn't change."""
    memory = _memory()
    gpus = _nvidia_gpus() or _amd_gpus()
    gpu = max(gpus, key=lambda g: g["memory_total"], default=None)
    found = {"cpu": _cpu_name(), "threads": os.cpu_count() or 1,
             "memory_gb": round(memory["total"] / 2**30, 1) if memory else None,
             "gpu": gpu["name"] if gpu else None,
             "gpu_memory_gb": round(gpu["memory_total"] / 2**30, 1) if gpu else None}
    if not gpu and platform.system() == "Darwin" and platform.machine() == "arm64" and found["memory_gb"]:
        found.update(gpu="Apple GPU", gpu_memory_gb=round(found["memory_gb"] * APPLE_GPU_SHARE, 1))
    return found


def _cpu_name() -> str | None:
    if platform.system() == "Darwin":
        result = _run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
        name = result.stdout.strip() if result and result.returncode == 0 else ""
    else:
        try:
            info = Path("/proc/cpuinfo").read_text()
            name = re.search(r"^model name\s*:\s*(.+)$", info, re.M).group(1)
        except (OSError, AttributeError):
            name = platform.processor()
    return re.sub(r"\s+", " ", re.sub(r"\((R|TM)\)|\bCPU\b|@.*$", "", name)).strip() or None


def _fit(size_gb: float, hardware: dict) -> str:
    """Where a model of this size runs here: "gpu", "mixed" (part GPU, part CPU, and slow),
    "cpu", or "too_big" for the computer's memory."""
    needs = size_gb + MODEL_OVERHEAD_GB
    if needs <= (hardware["gpu_memory_gb"] or 0):
        return "gpu"
    memory = hardware["memory_gb"]
    if memory is not None and needs > memory - SPARE_MEMORY_GB:
        return "too_big"
    return "mixed" if hardware["gpu_memory_gb"] else "cpu"


def ollama_advice() -> dict:
    """What this computer has, the model that suits it best, and how each suggested one
    would run. Speed comes first: a model wholly on the GPU, else a small one on the CPU."""
    hardware = _hardware()
    fits = [(name, size, note, _fit(size, hardware)) for name, size, note in LOCAL_MODELS]
    on_gpu = [f for f in fits if f[3] == "gpu"]
    on_cpu = [f for f in fits if f[3] != "too_big" and f[1] <= MAX_CPU_MODEL_GB]
    best = (on_gpu or on_cpu or fits)[-1 if on_gpu or on_cpu else 0]
    if on_gpu:
        reason = f"It fits wholly on your {hardware['gpu']}, so answers come quickly."
    elif hardware["gpu"]:
        reason = f"Your {hardware['gpu']} is too small for these models, so this one runs on the CPU at a usable speed."
    else:
        reason = "Looma found no NVIDIA, AMD or Apple GPU, so this one runs on the CPU at a usable speed."
    return {**hardware, "recommended": best[0], "reason": reason,
            "models": [{"name": name, "size_gb": size, "note": note, "fit": fit} for name, size, note, fit in fits]}


def _best_installed(sizes: dict[str, int]) -> str:
    """The model to use when none was picked: the suggested one if it's here, else the biggest
    that fits on the GPU, else the smallest, which is the quickest on a CPU."""
    hardware = _hardware()
    recommended = ollama_advice()["recommended"]
    if recommended in sizes:
        return recommended
    on_gpu = [name for name, size in sizes.items() if _fit(size / 1e9, hardware) == "gpu"]
    return max(on_gpu, key=sizes.get) if on_gpu else min(sizes, key=sizes.get)


def _size(count) -> str | None:
    return f"{count / 1e9:.1f} GB" if isinstance(count, (int, float)) and count else None


# Ollama reads only its first 2,048 or 4,096 tokens unless told otherwise, and cuts the rest
# of a prompt without saying so. Explanations' prompts fit in this (see explain.py).
NUM_CTX = 8192


def _ollama_ask(model: str | None, system: str, prompt: str) -> str:
    if not model:
        ready = _ollama_status()
        if not ready["available"]:
            raise ProviderError(f"The local model isn't ready: {ready['detail']}.")
        model = ready["default_model"]
    body = json.dumps({"model": model, "stream": False, "format": "json", "options": {"temperature": 0.2, "num_ctx": NUM_CTX},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
    request = urllib.request.Request(_ollama_url("/api/chat"), data=body.encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise ProviderError(f"Ollama: {json.loads(detail).get('error', detail) if detail.startswith('{') else detail or error}")
    except (OSError, urllib.error.URLError) as error:
        raise ProviderError(f"Couldn't reach Ollama ({getattr(error, 'reason', error)}). Is it running?")
    return (reply.get("message") or {}).get("content") or ""


# Running Ollama from the dashboard: starting and stopping its server (only one Looma
# started itself: a system service is stopped the system's way), and downloading, loading,
# unloading and removing models. Downloads run in the background; their progress is read
# from ollama_state().

# A model's name as Ollama takes it: "qwen2.5:3b", "library/llama3.2", "hf.co/user/model:Q4_K_M".
MODEL_NAME = re.compile(r"[A-Za-z0-9][\w.\-/:]{0,199}")
LOAD_TIMEOUT = 300
# A downloaded model's finished pull stays in the list this long, so the page can say it's done.
PULL_KEPT = 60
_server: dict = {"process": None}
_pulls: dict[str, dict] = {}
_pulls_lock = threading.Lock()


def _find_ollama() -> str | None:
    return shutil.which("ollama") or _first(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe")


def _started_here() -> bool:
    process = _server["process"]
    return process is not None and process.poll() is None


def ollama_state() -> dict:
    """Whether Ollama is installed and running, its models (with which are loaded in memory),
    and the downloads under way."""
    state = {"installed": _find_ollama() is not None, "running": False, "version": None,
             "host": _ollama_url("").split("://", 1)[-1], "started_here": _started_here(),
             "managed_by": ollama_manager(), "needs_admin": needs_admin(), "local": _local_ollama(), "models": [], "pulls": _pull_list(),
             "install": []}
    try:
        state["version"] = _ollama_get("/api/version").get("version")
        state["running"] = True
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        if not state["installed"]:
            state["install"] = _ollama_status()["install"]
        return state
    state["installed"] = True  # running, even if not where Looma looks for it
    try:
        loaded = {m.get("name"): m for m in _ollama_get("/api/ps").get("models", []) if isinstance(m, dict)}
        tags = _ollama_get("/api/tags").get("models", [])
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        loaded, tags = {}, []
    state["models"] = sorted(
        ({"name": m["name"], "size": m.get("size") or 0,
          "parameters": (m.get("details") or {}).get("parameter_size") or None,
          "family": (m.get("details") or {}).get("family") or None,
          "loaded": m["name"] in loaded, "until": (loaded.get(m["name"]) or {}).get("expires_at")}
         for m in tags if isinstance(m, dict) and isinstance(m.get("name"), str)),
        key=lambda m: m["size"])
    return state


def ollama_manager() -> str | None:
    """What runs Ollama here, so starting and stopping it goes through the same thing:
    "looma" (started from the dashboard), "service" (the systemd service Ollama's Linux
    installer sets up), "app" (the Ollama app on macOS or Windows), or "command" (an
    `ollama serve` run by hand). None when it isn't installed."""
    if _started_here():
        return "looma"
    if platform.system() == "Linux" and _linux_service():
        return "service"
    if _ollama_app():
        return "app"
    return "command" if _find_ollama() else None


def _linux_service() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(["systemctl", "show", "--property=LoadState", "--value", "ollama.service"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "loaded"


def _ollama_app() -> str | None:
    if platform.system() == "Darwin":
        for folder in (Path("/Applications"), Path.home() / "Applications"):
            if (folder / "Ollama.app").is_dir():
                return str(folder / "Ollama.app")
    if _windows():
        return _first(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe")
    return None


def _local_ollama() -> bool:
    """Start and Stop only reach an Ollama on this computer, not one OLLAMA_HOST points elsewhere."""
    return _local(urlsplit(_ollama_url("")).hostname or "")


class NeedsPermission(ProviderError):
    """Asking for an administrator's password waits for the person's OK on the page first."""


def needs_admin() -> bool:
    """Starting or stopping Ollama here takes an administrator's password (its Linux service)."""
    return ollama_manager() == "service" and not (hasattr(os, "geteuid") and os.geteuid() == 0)


def _ask_first(command: list[str], confirmed: bool) -> None:
    if not confirmed:
        raise NeedsPermission(f"Looma needs your OK before it asks for your password to run {' '.join(command)}.")


def start_ollama(confirmed: bool = False) -> None:
    """Starts Ollama the way it's set up here: its Linux service (Linux asks for your
    password), its app on macOS or Windows, or `ollama serve` in the background."""
    if _ollama_running():
        return
    if not _local_ollama():
        raise ProviderError(f"Ollama at {_ollama_url('')} is on another computer. Start it there.")
    manager = ollama_manager()
    if manager is None:
        raise ProviderError("Ollama isn't installed on this computer.")
    if manager == "service":
        _ask_first(["systemctl", "start", "ollama"], confirmed or not needs_admin())
        _as_admin(["systemctl", "start", "ollama"], "start")
    elif manager == "app" and platform.system() == "Darwin":
        _run(["open", "-a", _ollama_app()])
    elif manager == "app":
        subprocess.Popen([_ollama_app()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        _serve()
    if not _wait_for(True):
        if _server["process"] is not None and _server["process"].poll() is not None:
            raise ProviderError("Ollama stopped as soon as it started. Its output is in ~/.looma/ollama.log.")
        raise ProviderError("Ollama started but isn't answering yet. Check again in a moment.")


def _serve() -> None:
    log = open(paths.home() / "ollama.log", "ab")
    detached = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if _windows()
                else {"start_new_session": True})
    try:
        _server["process"] = subprocess.Popen([_find_ollama(), "serve"], stdin=subprocess.DEVNULL, stdout=log,
                                              stderr=subprocess.STDOUT, env=_env(), **detached)
    finally:
        log.close()


def stop_ollama(confirmed: bool = False) -> None:
    """Stops Ollama however it was started: the one Looma started, its Linux service (Linux
    asks for your password), its app, or an `ollama serve` run by hand."""
    if not _ollama_running() and not _started_here():
        return
    if not _local_ollama():
        raise ProviderError(f"Ollama at {_ollama_url('')} is on another computer. Stop it there.")
    manager = ollama_manager()
    if manager == "looma":
        process = _server["process"]
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        _server["process"] = None
        return
    if manager == "service" and _service_active():
        _ask_first(["systemctl", "stop", "ollama"], confirmed or not needs_admin())
        _as_admin(["systemctl", "stop", "ollama"], "stop")
    elif _windows():
        for image in ("ollama app.exe", "ollama.exe"):
            _run(["taskkill", "/IM", image, "/F"])
    else:
        # The macOS app is asked to quit first, so it doesn't restart the server.
        quit_app = platform.system() == "Darwin" and _run(["osascript", "-e", 'quit app "Ollama"']) is not None
        if not (quit_app and _wait_for(False, seconds=3)):
            _run(["pkill", "-x", "ollama"])
    if not _wait_for(False):
        raise ProviderError("Ollama is still running: it may belong to another account on this computer. "
                            "Stop it with sudo pkill ollama.")


def _service_active() -> bool:
    try:
        result = subprocess.run(["systemctl", "is-active", "ollama.service"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "active"


def _as_admin(command: list[str], verb: str) -> None:
    """Runs a system command that needs an administrator: as is when Looma already is one,
    otherwise through pkexec, which shows the desktop's own password window."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        result = _run(command)
    elif shutil.which("pkexec"):
        result = _run(["pkexec", *command], timeout=180)
    else:
        result = None
    if result is None or result.returncode != 0:
        cancelled = result is not None and result.returncode == 126
        raise ProviderError(("The password window was closed, so Ollama wasn't " + ("started." if verb == "start" else "stopped."))
                            if cancelled else
                            f"Ollama runs as a system service, and Linux couldn't ask for your password here. "
                            f"In a terminal, run: sudo {' '.join(command)}")


def _run(command: list[str], timeout: float = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _wait_for(running: bool, seconds: float = 10) -> bool:
    for _ in range(int(seconds / 0.25)):
        if _ollama_running() == running:
            return True
        time.sleep(0.25)
    return _ollama_running() == running


def _ollama_running() -> bool:
    try:
        _ollama_get("/api/version", timeout=1.5)
        return True
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _checked_name(name: str) -> str:
    name = (name or "").strip()
    if not MODEL_NAME.fullmatch(name) or ".." in name:
        raise ProviderError("That isn't a model name. Names look like qwen2.5:3b or llama3.2.")
    return name


def pull_model(name: str) -> None:
    """Starts downloading a model in the background; ollama_state() shows how far it got."""
    name = _checked_name(name)
    if not _ollama_running():
        raise ProviderError("Start Ollama first.")
    with _pulls_lock:
        if name in _pulls and not _pulls[name]["done"]:
            return
        _pulls[name] = {"name": name, "status": "Starting", "completed": 0, "total": 0, "error": None, "done": False,
                        "finished": None}
    threading.Thread(target=_pull, args=(name,), daemon=True).start()


def _pull(name: str) -> None:
    progress = _pulls[name]
    request = urllib.request.Request(_ollama_url("/api/pull"), data=json.dumps({"model": name, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            for line in response:
                try:
                    step = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if step.get("error"):
                    progress["error"] = str(step["error"])
                    break
                progress["status"] = str(step.get("status") or progress["status"])
                if step.get("total"):
                    progress["total"], progress["completed"] = step["total"], step.get("completed") or 0
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        progress["error"] = json.loads(detail).get("error", detail) if detail.startswith("{") else detail or str(error)
    except (OSError, urllib.error.URLError) as error:
        progress["error"] = f"The download stopped ({getattr(error, 'reason', error)})."
    if not progress["error"] and progress["status"] != "success":
        progress["error"] = "The download ended before it finished."
    progress["done"], progress["finished"] = True, time.monotonic()


def _pull_list() -> list[dict]:
    with _pulls_lock:
        moment = time.monotonic()
        for name in [n for n, p in _pulls.items() if p["done"] and not p["error"] and moment - p["finished"] > PULL_KEPT]:
            del _pulls[name]
        return [{k: v for k, v in p.items() if k != "finished"} for p in _pulls.values()]


def load_model(name: str) -> None:
    """Loads a model into memory, so the next explanation starts at once. It stays for 30
    minutes after its last use, as `ollama run` keeps it."""
    _ollama_post("/api/generate", {"model": _checked_name(name), "keep_alive": "30m"}, LOAD_TIMEOUT)


def unload_model(name: str) -> None:
    """Frees a model's memory, as `ollama stop` does."""
    _ollama_post("/api/generate", {"model": _checked_name(name), "keep_alive": 0}, 30)


def remove_model(name: str) -> None:
    _ollama_post("/api/delete", {"model": _checked_name(name)}, 30, method="DELETE")
    with _pulls_lock:
        _pulls.pop(name, None)


def _ollama_post(path: str, body: dict, timeout: float, method: str = "POST") -> dict:
    request = urllib.request.Request(_ollama_url(path), data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            said = response.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise ProviderError(f"Ollama: {json.loads(detail).get('error', detail) if detail.startswith('{') else detail or error}")
    except (OSError, urllib.error.URLError) as error:
        raise ProviderError(f"Couldn't reach Ollama ({getattr(error, 'reason', error)}). Is it running?")
    try:
        return json.loads(said) if said else {}
    except json.JSONDecodeError:
        return {}


# What a local model costs the computer, for the chart on Settings → AI → Ollama: the
# GPU's use and memory (NVIDIA through nvidia-smi, AMD through Linux's sysfs), or the CPU
# and memory when there's no GPU Looma can read; and how much of each loaded model sits
# on the GPU, as `ollama ps` shows it.

_cpu_last: dict = {"busy": None, "total": None}


def ollama_usage() -> dict:
    gpus = _nvidia_gpus() or _amd_gpus()
    try:
        loaded = _ollama_get("/api/ps", timeout=1.5).get("models", []) if _local_ollama() else []
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        loaded = []
    models = [{"name": m["name"], "size": m.get("size") or 0,
               "on_gpu": round(min(1.0, (m.get("size_vram") or 0) / m["size"]), 2) if m.get("size") else 0.0}
              for m in loaded if isinstance(m, dict) and isinstance(m.get("name"), str)]
    note = None
    if not gpus and platform.system() == "Darwin":
        note = "macOS shares GPU use only with an administrator, so this shows the CPU."
    elif not gpus:
        note = "No GPU Looma can read (NVIDIA or AMD), so this shows the CPU."
    return {"gpus": gpus, "cpu": _cpu_percent(), "cores": os.cpu_count() or 1, "memory": _memory(),
            "models": models, "note": note}


def _nvidia_gpus() -> list[dict]:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    result = _run([smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                   "--format=csv,noheader,nounits"], timeout=5)
    gpus = []
    for line in (result.stdout if result and result.returncode == 0 else "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            gpus.append({"name": parts[0], "percent": float(parts[1]),
                         "memory_used": int(float(parts[2]) * 2**20), "memory_total": int(float(parts[3]) * 2**20)})
        except ValueError:
            continue
    return gpus


def _amd_gpus() -> list[dict]:
    if platform.system() != "Linux":
        return []
    gpus = []
    for device in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        folder = Path(device)
        busy, used, total = (_read_int(folder / name) for name in
                             ("gpu_busy_percent", "mem_info_vram_used", "mem_info_vram_total"))
        if busy is None or folder.parent.name in {g["card"] for g in gpus}:
            continue
        gpus.append({"card": folder.parent.name, "name": "AMD GPU", "percent": float(busy),
                     "memory_used": used or 0, "memory_total": total or 0})
    for gpu in gpus:
        gpu.pop("card")
    return gpus


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _cpu_percent() -> float | None:
    """The whole CPU's use since the last call (or over a tenth of a second, the first time)."""
    if psutil is not None:
        # psutil measures since its last call; the first call measures a tenth of a second.
        primed, _cpu_last["psutil"] = _cpu_last.get("psutil"), True
        return float(psutil.cpu_percent(interval=None if primed else 0.1))
    if platform.system() == "Linux":
        def sample():
            fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            return sum(fields) - idle, sum(fields)
        try:
            if _cpu_last["total"] is None:
                _cpu_last["busy"], _cpu_last["total"] = sample()
                time.sleep(0.1)
            busy, total = sample()
        except (OSError, ValueError, IndexError):
            return None
        span = total - _cpu_last["total"]
        share = (busy - _cpu_last["busy"]) / span * 100 if span > 0 else 0.0
        _cpu_last["busy"], _cpu_last["total"] = busy, total
        return round(max(0.0, min(100.0, share)), 1)
    if platform.system() == "Darwin":
        result = _run(["ps", "-A", "-o", "%cpu="], timeout=5)
        try:
            return round(min(100.0, sum(float(x) for x in result.stdout.split()) / (os.cpu_count() or 1)), 1)
        except (AttributeError, ValueError):
            return None
    return None


def _memory() -> dict | None:
    if psutil is not None:
        found = psutil.virtual_memory()
        return {"used": int(found.total - found.available), "total": int(found.total)}
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        total = int(info["MemTotal"].split()[0]) * 1024
        return {"used": total - int(info["MemAvailable"].split()[0]) * 1024, "total": total}
    except (OSError, KeyError, ValueError):
        return None


# Antigravity -----------------------------------------------------------------------

def _find_agy() -> str | None:
    found = shutil.which("agy")
    if found:
        return found
    if _windows():
        return _first(Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe")
    return _first(Path.home() / ".local" / "bin" / "agy")


def _agy_status() -> dict:
    path = _find_agy()
    if not path:
        install = ([{"shell": "PowerShell", "command": "irm https://antigravity.google/cli/install.ps1 | iex"},
                    {"shell": "Command Prompt",
                     "command": "curl -fsSL https://antigravity.google/cli/install.cmd -o install.cmd && install.cmd && del install.cmd"}]
                   if _windows() else [{"shell": "Terminal", "command": "curl -fsSL https://antigravity.google/cli/install.sh | bash"}])
        return _base("antigravity", False, detail="Install the Antigravity CLI", install=install, login="terminal",
                     login_command="agy")
    signed_in, account = None, None
    try:
        result = subprocess.run([path, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20, env=_env())
        said = result.stdout + result.stderr
        if result.returncode == 0:
            signed_in = True
            email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", said)
            account = email.group(0) if email else None
        elif re.search(r"not (signed|logged) in|unauthenticated|no account", said, re.I):
            signed_in = False
        # Anything else: this version can't say; the first explanation finds out.
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Antigravity has no command that lists its models: its own choice (/model) is used
    # unless one is typed here.
    return _base("antigravity", True, signed_in=signed_in, account=account, login="terminal", login_command="agy",
                 models=[], default_model="")


def _agy_ask(model: str | None, system: str, prompt: str) -> str:
    path = _find_agy()
    if not path:
        raise ProviderError("The Antigravity CLI isn't installed on this computer.")
    command = [path, "-p", f"{system}\n\n{prompt}", "--output-format", "text"]
    if model:
        command += ["--model", model]
    with tempfile.TemporaryDirectory(prefix="looma-explain-") as folder:
        try:
            result = subprocess.run(command, cwd=folder, env=_env(), capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ProviderError("Antigravity took too long to answer. Try again.")
    if result.returncode != 0 or not result.stdout.strip():
        said = [line for line in (result.stderr + result.stdout).splitlines() if line.strip()]
        last = said[-1] if said else "it gave no answer"
        if re.search(r"auth|sign ?in|log ?in|credential", last, re.I):
            raise ProviderError("Antigravity isn't signed in. Run agy in a terminal once and sign in with Google.",
                                signed_out=True)
        raise ProviderError(f"Antigravity: {last}")
    return result.stdout


# A model on your own server -------------------------------------------------------
#
# Connected by pasting the curl command that reaches it (from Ollama's, vLLM's, llama.cpp's
# or LM Studio's docs, or the person's own notes). The command is only read, never run:
# Looma keeps its address, its headers (the API key) and the model it names, and talks to
# the server itself. The key stays in Looma's database and never goes back to the page.

HOSTED_SETTING = "hosted_model"
# What a pasted curl may carry besides its address and data; anything else is ignored.
CURL_VALUE_FLAGS = {"-H", "--header", "-u", "--user", "-d", "--data", "--data-raw", "--data-binary",
                    "--data-ascii", "--json", "-X", "--request", "--url", "-o", "--output", "-m", "--max-time",
                    "--connect-timeout", "-A", "--user-agent", "-e", "--referer", "-x", "--proxy", "-w",
                    "--write-out", "--retry", "-b", "--cookie", "-c", "--cookie-jar", "--cacert", "--cert", "--key",
                    "--oauth2-bearer", "-F", "--form"}
# Headers that describe the request itself; Looma sets its own.
SKIPPED_HEADERS = {"content-type", "content-length", "accept", "host", "connection", "user-agent", "accept-encoding"}
MAX_CURL = 20_000
MAX_HEADERS = 10
CHECK_TIMEOUT = 90  # a server may have to load the model into memory first


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the API key to wherever it points, so none is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError(f"Your server redirected to another address ({urlsplit(newurl).netloc or newurl}). "
                            "Paste the address it redirects to instead.")


_opener = urllib.request.build_opener(_NoRedirects)


def parse_curl(text: str) -> dict:
    """The server, headers and model in a pasted curl command, without running it. Line
    continuations from bash (\\), PowerShell (`) and Command Prompt (^) are all taken."""
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("Paste the curl command that reaches your model.")
    if len(text) > MAX_CURL:
        raise ProviderError("That's too long to be a curl command.")
    joined = re.sub(r"[\\`^][ \t]*\r?\n", " ", text.strip())
    try:
        lexer = shlex.shlex(joined, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        raise ProviderError("That curl command has an unclosed quote.")
    if not words or os.path.basename(words[0]).lower() not in ("curl", "curl.exe"):
        raise ProviderError("That isn't a curl command. It should start with curl.")
    if any(re.fullmatch(r"[();<>|&]+", word) for word in words[1:]):
        # One command only: nothing piped, chained or redirected (it's never run, but a paste
        # like `curl … | sh` isn't a model's address).
        raise ProviderError("Paste a single curl command, without |, ;, && or >.")
    urls, headers, data = [], {}, None
    position = 1
    while position < len(words):
        word = words[position]
        flag, value = word, None
        if word.startswith("--") and "=" in word:
            flag, value = word.split("=", 1)
        elif word in CURL_VALUE_FLAGS or (re.fullmatch(r"-[HudXmAeoxwbcF].+", word) and word[:2] in CURL_VALUE_FLAGS):
            if word in CURL_VALUE_FLAGS:
                position += 1
                value = words[position] if position < len(words) else ""
            else:
                flag, value = word[:2], word[2:]
        elif not word.startswith("-"):
            urls.append(word)
        position += 1
        if value is None:
            continue
        if flag in ("--url",):
            urls.append(value)
        elif flag in ("-H", "--header"):
            name, _, said = value.partition(":")
            if name.strip() and name.strip().lower() not in SKIPPED_HEADERS:
                headers[name.strip()] = said.strip()
        elif flag in ("-u", "--user"):
            headers["Authorization"] = "Basic " + base64.b64encode(value.encode()).decode()
        elif flag == "--oauth2-bearer":
            headers["Authorization"] = f"Bearer {value}"
        elif flag in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json"):
            data = value
    # A value of a flag Looma doesn't know looks like an address too; an http(s) one wins.
    urls = [url for url in urls if re.match(r"https?://", url, re.I)] or urls
    if len(urls) != 1:
        raise ProviderError("The curl command should have one address, like http://your-server:11434/api/chat."
                            if not urls else "The curl command has more than one address; paste the one for chat.")
    model = None
    if data and not data.startswith("@"):  # @file would be read from disk: never
        try:
            body = json.loads(data)
            model = body.get("model") if isinstance(body, dict) and isinstance(body.get("model"), str) else None
        except json.JSONDecodeError:
            pass
    return hosted_connection(urls[0], headers, model)


def hosted_connection(url: str, headers: dict | None = None, model: str | None = None, key: str | None = None) -> dict:
    """A connection from an address, headers and model: from a pasted curl, or typed in."""
    url = (url or "").strip()
    if not re.match(r"https?://", url, re.I):
        url = f"http://{url}" if url and "://" not in url else url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ProviderError("The address should start with http:// or https://, like http://your-server:11434.")
    headers = dict(headers or {})
    if parts.username is not None:  # http://user:pass@host: sent as a header instead
        credentials = f"{parts.username}:{parts.password or ''}"
        headers.setdefault("Authorization", "Basic " + base64.b64encode(credentials.encode()).decode())
    if key and key.strip():
        headers["Authorization"] = key.strip() if key.strip().lower().startswith(("bearer ", "basic ")) \
            else f"Bearer {key.strip()}"
    if len(headers) > MAX_HEADERS:
        raise ProviderError(f"The curl command has more than {MAX_HEADERS} headers.")
    netloc = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    netloc += f":{parts.port}" if parts.port else ""
    style, base = _hosted_endpoint(parts.path.rstrip("/"), parts.port)
    return {"base": urlunsplit((parts.scheme.lower(), netloc, base, "", "")), "style": style, "headers": headers,
            "model": (model or "").strip()[:100] or None}


def _hosted_endpoint(path: str, port: int | None) -> tuple[str, str]:
    """Which API the server speaks, from the address in the curl, and the address it lives
    under. Ollama's own (/api/chat) or the OpenAI-style one (/v1/chat/completions) that
    vLLM, llama.cpp, LM Studio and Ollama all serve."""
    for suffix, style in (("/api/chat", "ollama"), ("/api/generate", "ollama"), ("/api/tags", "ollama"),
                          ("/chat/completions", "openai"), ("/completions", "openai"), ("/models", "openai")):
        if path.endswith(suffix):
            return style, path[:-len(suffix)]
    if path.endswith("/v1") or re.search(r"/v\d+$", path):
        return "openai", path
    if path in ("", "/api"):
        return ("ollama", "") if port == 11434 else ("openai", "/v1")
    raise ProviderError(f"Looma can't write explanations through {path}: it needs a chat address, like "
                        "/v1/chat/completions or Ollama's /api/chat.")


def save_hosted(connection: dict) -> None:
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)",
                     (HOSTED_SETTING, json.dumps(connection)))
    conn.close()


def forget_hosted() -> None:
    conn = connect()
    with conn:
        conn.execute("DELETE FROM settings WHERE name = ?", (HOSTED_SETTING,))
    conn.close()


def hosted() -> dict | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (HOSTED_SETTING,)).fetchone()
    conn.close()
    try:
        saved = json.loads(row["value"]) if row else None
    except json.JSONDecodeError:
        return None
    return saved if isinstance(saved, dict) and saved.get("base") else None


def hosted_summary(connection: dict) -> dict:
    """The connection as the page may see it: the key only as its last four characters."""
    parts = urlsplit(connection["base"])
    auth = next((v for k, v in connection["headers"].items() if k.lower() in ("authorization", "x-api-key", "api-key")), "")
    secret = auth.split(" ", 1)[-1]
    return {"url": connection["base"], "style": connection["style"],
            "key": f"…{secret[-4:]}" if len(secret) >= 12 else ("set" if secret else None),
            "headers": sorted(connection["headers"]),
            "unencrypted": parts.scheme == "http" and not _local(parts.hostname or "")}


def _local(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _hosted_call(connection: dict, path: str, body: dict | None = None, timeout: float = TIMEOUT):
    headers = {**connection["headers"], "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(connection["base"] + path, headers=headers, method="POST" if body else "GET",
                                     data=json.dumps(body).encode("utf-8") if body is not None else None)
    host = urlsplit(connection["base"]).netloc
    try:
        with _opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        said = _hosted_hide(connection, error.read().decode("utf-8", "replace")[:500])
        try:
            found = json.loads(said)
            said = (found.get("error") or {}).get("message") if isinstance(found.get("error"), dict) else \
                found.get("error") or found.get("detail") or said
        except (json.JSONDecodeError, AttributeError):
            pass
        if error.code in (401, 403):
            raise ProviderError(f"Your server at {host} refused the key ({error.code}). Check the API key in the curl.",
                                signed_out=True)
        raise ProviderError(f"Your server at {host} answered {error.code}: {str(said).strip() or error.reason}")
    except ProviderError:
        raise
    except json.JSONDecodeError:
        raise ProviderError(f"Your server at {host} didn't answer with JSON. Is that the model's API address?")
    except (OSError, urllib.error.URLError) as error:
        reason = getattr(error, "reason", error)
        raise ProviderError(f"Couldn't reach your server at {host} ({_hosted_hide(connection, str(reason))}).")


def _hosted_hide(connection: dict, text: str) -> str:
    """Error text with the key taken out, in case the server repeats it back."""
    for value in connection["headers"].values():
        for secret in {value, value.split(" ", 1)[-1]}:
            if len(secret) >= 6:
                text = text.replace(secret, "[key]")
    return text


def hosted_models(connection: dict, timeout: float = 5) -> list[dict]:
    if connection["style"] == "ollama":
        found = _hosted_call(connection, "/api/tags", timeout=timeout).get("models", [])
        return [{"id": m["name"], "label": m["name"], "note": _size(m.get("size")) or ""}
                for m in found if isinstance(m, dict) and isinstance(m.get("name"), str)]
    found = _hosted_call(connection, "/models", timeout=timeout)
    items = found.get("data", []) if isinstance(found, dict) else found if isinstance(found, list) else []
    return [{"id": m["id"], "label": m["id"], "note": ""}
            for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)]


def check_hosted(connection: dict) -> dict:
    """Proves the connection works before it's kept: the server lists its models (or can't,
    which some don't), and the model answers a one-word question."""
    try:
        models = hosted_models(connection, timeout=10)
    except ProviderError as error:
        if error.signed_out or "Couldn't reach" in str(error):
            raise
        models = []  # some servers don't list models; the question below still has to work
    model = connection["model"] or (models[0]["id"] if models else None)
    if not model:
        raise ProviderError("The curl command doesn't name a model, and your server didn't list any. "
                            "Add \"model\" to its -d data, or type the model's name.")
    answer = _hosted_chat(connection, model, "Reply with only the word ok.", "Are you there?", tokens=16,
                          timeout=CHECK_TIMEOUT)
    if not answer.strip():
        raise ProviderError(f"Your server answered, but {model} said nothing. Is it a chat model?")
    return {**connection, "model": model, "models": models}


def _hosted_status() -> dict:
    connection = hosted()
    if not connection:
        return _base("hosted", False, detail="Paste the curl command for your server", login="none")
    summary = hosted_summary(connection)
    host = urlsplit(connection["base"]).netloc
    try:
        models = hosted_models(connection, timeout=3)
    except ProviderError as error:
        return _base("hosted", False, detail=str(error), login="none", server=summary, account=host,
                     default_model=connection.get("model"))
    default = connection.get("model") or (models[0]["id"] if models else None)
    if default and not any(m["id"] == default for m in models):
        models.insert(0, {"id": default, "label": default, "note": "From your curl command"})
    return _base("hosted", True, signed_in=True, account=host, login="none", models=models,
                 default_model=default, server=summary)


def _hosted_ask(model: str | None, system: str, prompt: str) -> str:
    connection = hosted()
    if not connection:
        raise ProviderError("No server is connected. Paste its curl command in Settings → AI.")
    model = model or connection.get("model")
    if not model:
        models = hosted_models(connection)
        if not models:
            raise ProviderError("Your server lists no models. Pick one in Settings → AI.")
        model = models[0]["id"]
    return _hosted_chat(connection, model, system, prompt)


def _hosted_chat(connection: dict, model: str, system: str, prompt: str, tokens: int | None = None,
                 timeout: float = TIMEOUT) -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if connection["style"] == "ollama":
        options = {"temperature": 0.2, "num_ctx": NUM_CTX, **({"num_predict": tokens} if tokens else {})}
        reply = _hosted_call(connection, "/api/chat", {"model": model, "stream": False, "options": options,
                                                       "messages": messages}, timeout)
        return (reply.get("message") or {}).get("content") or ""
    # No response_format: not every OpenAI-style server takes it, and explain.py finds the
    # JSON in the answer anyway.
    body = {"model": model, "messages": messages, "temperature": 0.2, "stream": False,
            **({"max_tokens": tokens} if tokens else {})}
    reply = _hosted_call(connection, "/chat/completions", body, timeout)
    choices = reply.get("choices") if isinstance(reply, dict) else None
    if not choices or not isinstance(choices[0], dict):
        raise ProviderError("Your server's answer had no choices in it. Is that a chat completions address?")
    return (choices[0].get("message") or {}).get("content") or choices[0].get("text") or ""


def _cheapest(models: list[dict]) -> str | None:
    cheap = [m for m in models if CHEAP.search(m["id"])]
    return (cheap or models or [{"id": None}])[0]["id"]
