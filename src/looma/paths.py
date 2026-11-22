"""Where Looma keeps its data. LOOMA_HOME overrides the default ~/.looma. Data kept from
before the rename (~/.devdash and its devdash.db) is moved here the first time it's needed."""

import os
from pathlib import Path

_private: set[Path] = set()

# Where the data lived when Looma was called DevDash.
LEGACY_HOME = ".devdash"
LEGACY_DB = "devdash.db"


def home() -> Path:
    custom = os.environ.get("LOOMA_HOME")
    path = Path(custom) if custom else Path.home() / ".looma"
    if not custom:
        _move_legacy_home(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path not in _private:
        _make_private(path)
        _private.add(path)
    return path


def _move_legacy_home(path: Path) -> None:
    """Moves ~/.devdash to ~/.looma, once, and leaves a link at the old place so anything
    still pointing there (an older launcher, a shell alias) finds the same data."""
    old = Path.home() / LEGACY_HOME
    if path.exists() or old.is_symlink() or not old.is_dir():
        return
    try:
        old.rename(path)
        old.symlink_to(path, target_is_directory=True)
    except OSError:
        pass  # the old folder stays in use where it is if it can't be moved


def _make_private(path: Path) -> None:
    """Only you can read Looma's data (prompts, diffs, explanations), not other accounts
    on this computer. Windows already keeps a user's profile folder to that user."""
    if os.name != "posix":
        return
    try:
        path.chmod(0o700)
        for item in path.iterdir():
            if item.is_file() and not item.is_symlink():
                item.chmod(0o600)
    except OSError:
        pass


def db_path() -> Path:
    path = home() / "looma.db"
    old = home() / LEGACY_DB
    if not path.exists() and old.exists():
        # The database with its write-ahead log, which holds changes not yet folded in.
        for suffix in ("", "-wal", "-shm"):
            source = old.with_name(LEGACY_DB + suffix)
            if source.exists():
                source.rename(path.with_name(path.name + suffix))
    return path


def error_log() -> Path:
    return home() / "errors.log"


def hook_log() -> Path:
    return home() / "hooks.log"
