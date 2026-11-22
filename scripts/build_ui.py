"""Builds the dashboard page from the UI repo into src/looma/web, where `looma serve`
serves it. Run it after changing the UI, then commit src/looma/web, so that
`pipx install` gets the page with no Node needed on the machine installing it.

    python scripts/build_ui.py [path to the UI repo, default ../DevDash-FE]

Needs Node 18 or newer. Works the same on Linux, macOS and Windows."""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
WEB = HERE / "src" / "looma" / "web"


def main() -> int:
    ui = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else HERE.parent / "DevDash-FE"
    if not (ui / "package.json").is_file():
        print(f"No UI at {ui}. Pass the path of your DevDash-FE clone.")
        return 1
    # npm is npm.cmd on Windows, which only its full path runs.
    npm = shutil.which("npm")
    if npm is None:
        print("npm isn't installed. Install Node 18 or newer first.")
        return 1
    subprocess.run([npm, "ci" if (ui / "package-lock.json").is_file() else "install"], cwd=ui, check=True)
    subprocess.run([npm, "run", "build"], cwd=ui, check=True)
    shutil.rmtree(WEB, ignore_errors=True)
    shutil.copytree(ui / "dist", WEB)
    print(f"Built the page into {WEB.relative_to(HERE)}. Commit it so installs include it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
