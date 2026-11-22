#!/usr/bin/env bash
# Installs, repairs, uninstalls or starts Looma on Linux and macOS, from this DevDash-BE
# clone (the folder above scripts/) and a DevDash-FE clone next to it. Double-click it, or run:
#
#     bash scripts/looma.sh
#
# LOOMA_FE=/path/to/DevDash-FE points it at a page clone somewhere else.
#
# Installing never touches your projects. Uninstalling deletes Looma's data, after you
# type DELETE to confirm.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BE="$(dirname "$HERE")"
FE="${LOOMA_FE:-$(dirname "$BE")/DevDash-FE}"
# LOOMA_HOME, or DEVDASH_HOME from before the rename, picks another data folder.
DATA="${LOOMA_HOME:-${DEVDASH_HOME:-$HOME/.looma}}"
# Where the data lived when Looma was called DevDash.
OLD_DATA="$HOME/.devdash"
URL="http://127.0.0.1:8765"
# Where pipx puts the looma command; a fresh pipx may not have it on PATH yet.
export PATH="$HOME/.local/bin:$PATH"

# Opened with a double-click and no terminal: open one and run there.
if [ ! -t 0 ] && [ -z "${LOOMA_IN_TERMINAL:-}" ] && { [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || [ "$(uname)" = Darwin ]; }; then
  export LOOMA_IN_TERMINAL=1
  self="$HERE/$(basename "${BASH_SOURCE[0]}")"
  if [ "$(uname)" = Darwin ]; then exec open -a Terminal "$self"; fi
  for term in x-terminal-emulator gnome-terminal konsole xfce4-terminal xterm; do
    command -v "$term" >/dev/null || continue
    case "$term" in
      gnome-terminal) exec gnome-terminal -- bash "$self" ;;
      xfce4-terminal) exec xfce4-terminal -x bash "$self" ;;
      *) exec "$term" -e bash "$self" ;;
    esac
  done
fi

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
yes_no() { # yes_no "Question" default(y|n)
  local answer hint="[y/N]"
  [ "$2" = y ] && hint="[Y/n]"
  read -r -p "$1 $hint " answer
  answer="${answer:-$2}"
  [[ "$answer" =~ ^[Yy] ]]
}
# A window opened by double-click closes when the script ends: keep it until Enter.
finish() {
  local code=$?
  if [ -n "${LOOMA_IN_TERMINAL:-}" ]; then read -r -p $'\nPress Enter to close. ' _ || true; fi
  exit "$code"
}
trap finish EXIT

# `pipx list --short` is newer than the pipx some systems carry (Ubuntu 22.04's 1.0), so read the long form.
installed() { command -v pipx >/dev/null && pipx list 2>/dev/null | grep -q 'package looma '; }
# Whether a Looma answers on its address, rather than any process with "looma serve" in its command line.
running() { python3 -c "import urllib.request, sys; urllib.request.urlopen(sys.argv[1] + '/api/auth', timeout=2)" "$URL" >/dev/null 2>&1; }
# The installed Looma's own server process: its command line starts with the looma command.
# Before the rename it was started as `devdash serve`.
serve_pids() { pgrep -f '(^|/)(looma|devdash) serve( |$)' || true; }
# Looma installed under its old name, DevDash.
old_installed() { command -v pipx >/dev/null && pipx list 2>/dev/null | grep -q 'package devdash '; }

# Looma's data from before the rename moves to its new folder before anything here writes
# there, leaving a link at the old place. Looma does the same when it starts; doing it here
# first keeps this script from making an empty new folder that would stop the move.
move_old_data() {
  [ "$DATA" = "$HOME/.looma" ] || return 0
  [ -d "$OLD_DATA" ] && [ ! -L "$OLD_DATA" ] && [ ! -e "$DATA" ] || return 0
  mv "$OLD_DATA" "$DATA" && ln -s "$DATA" "$OLD_DATA"
  echo "Moved your data from $OLD_DATA to $DATA."
}

# The program installed as DevDash is taken out and Looma installed in its place; the data
# stays. Looma moves its hooks over the first time it starts.
replace_old_install() {
  old_installed || return 0
  say "Looma is installed here under its old name, DevDash. Replacing it with Looma…"
  needs
  git_tools
  build_page
  stop_running
  pipx uninstall devdash
  reinstall
  echo "Done. Start Looma once, so your projects' hooks move to the new name."
}

# Installs system packages with the computer's package manager, after asking: it takes
# your password (sudo), except with Homebrew.
system_install() { # system_install what-for apt-names dnf-names pacman-names zypper-names brew-names
  local why="$1" manager names
  if command -v apt-get >/dev/null; then manager=apt; names="$2"
  elif command -v dnf >/dev/null; then manager=dnf; names="$3"
  elif command -v pacman >/dev/null; then manager=pacman; names="$4"
  elif command -v zypper >/dev/null; then manager=zypper; names="$5"
  elif command -v brew >/dev/null; then manager=brew; names="$6"
  else fail "$why isn't installed, and no package manager was found. Install it yourself, then run this again."
  fi
  say "$why isn't installed."
  echo "This runs: $([ "$manager" = brew ] || echo "sudo ")$manager install $names"
  [ "$manager" = brew ] || echo "Your password goes to sudo; this script never sees it."
  yes_no "Install it now?" y || fail "Install $why, then run this again."
  # $names is a list of package names: split on purpose.
  # shellcheck disable=SC2086
  case "$manager" in
    apt) sudo apt-get update && sudo apt-get install -y $names ;;
    dnf) sudo dnf install -y $names ;;
    pacman) sudo pacman -S --needed --noconfirm $names ;;
    zypper) sudo zypper install -y $names ;;
    brew) brew install $names ;;
  esac
}

needs() {
  [ -f "$BE/pyproject.toml" ] || fail "This script belongs in DevDash-BE/scripts; there's no DevDash-BE in $BE."
  if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    system_install "Python 3.10 or newer" "python3 python3-venv" python3 python python3 python
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "Looma needs Python 3.10 or newer; this one is $(python3 --version)."
  fi
  if ! command -v pipx >/dev/null; then
    system_install "pipx" pipx pipx python-pipx python3-pipx pipx
    pipx ensurepath >/dev/null || true
  fi
  # The page is built from DevDash-FE when it's here; without it, the page already built
  # into DevDash-BE is used, and Node isn't needed.
  if [ -d "$FE" ]; then
    if ! command -v npm >/dev/null; then
      system_install "Node.js" "nodejs npm" "nodejs npm" "nodejs npm" "nodejs npm" node
    fi
    local major
    major="$(node -p 'process.versions.node.split(".")[0]')"
    [ "$major" -ge 18 ] || fail "Building the page needs Node 18 or newer; this computer has $(node --version). Get a newer one from https://nodejs.org, then run this again."
  elif [ ! -f "$BE/src/looma/web/index.html" ]; then
    fail "No DevDash-FE folder at $FE, and no page built into DevDash-BE."
  fi
}

# git, and GitHub's gh tool, which Sign in with GitHub on the dashboard uses. gh is optional:
# without it the dashboard asks for a pasted token instead. Distributions often ship an old gh
# (older ones can't sign in from the dashboard), so on apt systems it comes from GitHub's own
# package list, as cli.github.com says.
GH_MIN_MINOR=17  # gh 2.17 or newer
gh_old() {
  local major minor
  read -r major minor < <(gh --version 2>/dev/null | head -n1 | sed -E 's/^gh version ([0-9]+)\.([0-9]+).*/\1 \2/')
  [ -z "${major:-}" ] || [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "${minor:-0}" -lt "$GH_MIN_MINOR" ]; }
}

install_gh() {
  if command -v apt-get >/dev/null; then
    echo "This adds GitHub's package list (cli.github.com/packages) and runs: sudo apt-get install gh"
    echo "Your password goes to sudo; this script never sees it."
    yes_no "Go ahead?" y || return 1
    command -v curl >/dev/null || system_install "curl" curl curl curl curl curl
    local key
    key="$(mktemp)"
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$key" || return 1
    sudo mkdir -p -m 755 /etc/apt/keyrings
    sudo install -m 644 "$key" /etc/apt/keyrings/githubcli-archive-keyring.gpg
    rm -f "$key"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" |
      sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    sudo apt-get update && sudo apt-get install -y gh
  elif command -v brew >/dev/null; then
    if command -v gh >/dev/null; then brew upgrade gh; else brew install gh; fi
  elif command -v dnf >/dev/null; then
    yes_no "This runs: sudo dnf install gh. Go ahead?" y && sudo dnf install -y gh
  elif command -v pacman >/dev/null; then
    yes_no "This runs: sudo pacman -S github-cli. Go ahead?" y && sudo pacman -S --needed --noconfirm github-cli
  elif command -v zypper >/dev/null; then
    yes_no "This runs: sudo zypper install gh. Go ahead?" y && sudo zypper install -y gh
  else
    return 1
  fi
}

git_tools() {
  command -v git >/dev/null || system_install "git" git git git git git
  if command -v gh >/dev/null && ! gh_old; then return 0; fi
  if command -v gh >/dev/null; then
    say "Your gh is too old to sign in from the dashboard ($(gh --version | head -n1))."
  else
    say "GitHub's gh tool isn't installed. Sign in with GitHub on the dashboard uses it."
  fi
  if ! yes_no "Install the current gh?" y || ! install_gh; then
    echo "Skipped. You can still connect GitHub on the dashboard by pasting a token."
  fi
}

build_page() {
  [ -d "$FE" ] || return 0
  say "Building the page from DevDash-FE…"
  python3 "$BE/scripts/build_ui.py" "$FE"
}

after_install() {
  say "Looma is installed. Your data is in $DATA."
  local folder
  read -r -p $'\nWhere are your projects? Looma lists the git repos in that folder. [~/Projects, or Enter to skip] ' folder
  folder="${folder/#\~/$HOME}"
  [ -z "$folder" ] && [ -d "$HOME/Projects" ] && folder="$HOME/Projects"
  if [ -n "$folder" ]; then
    if [ -d "$folder" ]; then looma init "$folder" || true
    else echo "There's no folder $folder. Add projects later under Add project on the dashboard."
    fi
  fi
  if yes_no $'\nStart Looma now?' y; then start; fi
}

do_install() {
  if installed; then
    say "Looma is already installed. Choose Repair to reinstall it."
    return
  fi
  needs
  git_tools
  build_page
  say "Installing Looma…"
  rm -rf "$BE/build"
  pipx install "$BE"
  mkdir -p "$DATA"
  source_stamp > "$STAMP"
  after_install
}

# The newest change to the code Looma is installed from: DevDash-BE's code (not its built
# page, which is made from DevDash-FE) and DevDash-FE's. Start reinstalls when it moves on.
STAMP="$DATA/installed-from"
source_stamp() {
  python3 - "$BE" "$FE" <<'PY'
import os, sys
be, fe = sys.argv[1:3]
roots = [os.path.join(be, "src"), os.path.join(be, "pyproject.toml"), os.path.join(fe, "src"),
         os.path.join(fe, "index.html"), os.path.join(fe, "package.json")]
newest = 0.0
for root in roots:
    if os.path.isfile(root):
        newest = max(newest, os.path.getmtime(root))
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("web", "__pycache__", "node_modules")]
        for name in files:
            newest = max(newest, os.path.getmtime(os.path.join(folder, name)))
print(int(newest))
PY
}

# pipx builds from DevDash-BE; setuptools' build/ folder keeps files deleted since, so
# it's cleared first. Then the stamp notes what this install was made from.
reinstall() {
  rm -rf "$BE/build"
  pipx install --force "$BE"
  mkdir -p "$DATA"
  source_stamp > "$STAMP"
}

# Start runs the installed copy, so edits to the code show only after reinstalling it.
update_if_changed() {
  [ -f "$STAMP" ] && [ "$(source_stamp)" = "$(cat "$STAMP")" ] && return 0
  say "Looma's code changed since it was installed. Updating…"
  build_page
  stop_running
  reinstall
}

# Reinstalls over whatever is there, broken or not. Your data stays.
repair() {
  needs
  git_tools
  build_page
  say "Reinstalling Looma…"
  stop_running
  reinstall
  after_install
}

stop_running() {
  local pids
  pids="$(serve_pids)"
  [ -z "$pids" ] && return
  echo "Stopping the Looma that's running…"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
}

uninstall() {
  say "Uninstall Looma and delete its data?"
  echo "This deletes:"
  echo "  - the looma program"
  echo "  - everything in $DATA: your Looma account, the project list, recorded AI history and explanations"
  echo "  - Looma's hooks in your projects and in Claude Code, Codex and Antigravity"
  echo "It doesn't touch your projects' files or git history, Ollama, or its models."
  local answer
  read -r -p $'\nType DELETE to continue, or press Enter to cancel: ' answer
  [ "$answer" = DELETE ] || { echo "Cancelled. Nothing was changed."; return; }

  stop_running
  if command -v looma >/dev/null; then looma remove-hooks || echo "Couldn't take every hook out; carrying on."; fi
  if installed; then pipx uninstall looma; fi
  # Never anything but Looma's own folder.
  if [ -n "$DATA" ] && [ "$DATA" != "$HOME" ] && [ "$DATA" != / ] && [ -d "$DATA" ]; then
    rm -rf -- "$DATA"
  fi
  # The link left at the old place by the move from DevDash.
  if [ -L "$OLD_DATA" ]; then rm -f -- "$OLD_DATA"; fi
  say "Looma is uninstalled and its data deleted."
}

# A Chromium-family browser, for Looma's own app window; empty when there's none.
app_browser() {
  local name
  for name in google-chrome google-chrome-stable chromium chromium-browser brave-browser brave microsoft-edge microsoft-edge-stable; do
    if command -v "$name" >/dev/null; then command -v "$name"; return; fi
  done
}

# The app window's own profile. A snap browser may only write to folders in your home that
# aren't hidden, so it gets one in its snap folder; any other uses Looma's data folder.
window_profile() { # window_profile browser-path
  case "$1" in
    /snap/*) echo "$HOME/snap/$(basename "$1")/common/looma-window" ;;
    *) echo "$DATA/window" ;;
  esac
}

# Opens the page in a session of its own (setsid), so stopping Looma, or closing the terminal
# it runs in, never takes the browser with it. With a Chromium-family browser the page gets its
# own app window, a browser process apart from your usual one, which close_window shuts.
WINDOW=""
open_page() {
  local browser
  browser="$(app_browser)"
  if [ -n "$browser" ] && [ "$(uname)" != Darwin ]; then
    WINDOW="$(window_profile "$browser")"
    mkdir -p "$WINDOW"
    setsid -f "$browser" --app="$URL" --user-data-dir="$WINDOW" --no-first-run --no-default-browser-check \
      </dev/null >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null; then
    if command -v setsid >/dev/null; then setsid -f xdg-open "$URL" </dev/null >/dev/null 2>&1 || true
    else nohup xdg-open "$URL" </dev/null >/dev/null 2>&1 & fi
  elif command -v open >/dev/null; then open "$URL"
  fi
}

# Closes Looma's app window, and only it: the one browser process using its own profile.
close_window() {
  [ -n "$WINDOW" ] || return 0
  pkill -f -- "--user-data-dir=$WINDOW" 2>/dev/null || true
}

start() {
  command -v looma >/dev/null || fail "Looma isn't installed. Choose Install first."
  update_if_changed
  if running; then
    echo "Looma is already running."
  else
    looma serve &
    local server=$!
    trap 'kill "$server" 2>/dev/null; close_window' INT TERM HUP
    sleep 2
  fi
  open_page
  if [ -n "${server:-}" ]; then
    if [ -n "$WINDOW" ]; then
      echo "Opened Looma in its own window. Keep this window open while you use Looma; press Ctrl+C to stop it,"
      echo "which closes Looma's window and leaves your browser as it is."
    else
      echo "Opened $URL. Keep this window open while you use Looma; press Ctrl+C to stop it."
    fi
    wait "$server" || true
    close_window
  fi
}

say "Looma"
move_old_data
replace_old_install
if installed; then echo "Installed. Data in $DATA."; else echo "Not installed yet."; fi
cat <<'MENU'

  1  Install      first time on this computer
  2  Repair       reinstall over the current one; your data stays
  3  Uninstall    remove Looma and delete its data
  4  Start        run Looma and open it in the browser
  5  Quit
MENU
read -r -p $'\nChoose 1-5: ' choice
case "$choice" in
  1) do_install ;;
  2) repair ;;
  3) uninstall ;;
  4) start ;;
  *) echo "Nothing changed." ;;
esac
