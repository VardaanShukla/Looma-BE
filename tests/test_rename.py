"""Moving a computer set up when Looma was called DevDash: its data, hooks and project folders."""

import json

from conftest import connect_claude, git
from looma import paths
from looma.capture.install import (
    LEGACY_MARKER, antigravity_hooks_path, claude_settings_path, connect_tool, install_antigravity_hooks,
    record_command, refresh,
)
from looma.store import connect

OLD_COMMAND = "/old/pipx/venvs/devdash/bin/python -I -m devdash record"


def test_old_data_folder_moves_to_the_new_one_and_leaves_a_link(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOMA_HOME")
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    old = tmp_path / ".devdash"
    old.mkdir()
    (old / "devdash.db").write_text("db")
    (old / "devdash.db-wal").write_text("wal")
    (old / "hooks.log").write_text("log")

    assert paths.db_path() == tmp_path / ".looma" / "looma.db"
    new = tmp_path / ".looma"
    assert (new / "looma.db").read_text() == "db" and (new / "looma.db-wal").read_text() == "wal"
    assert (new / "hooks.log").read_text() == "log"
    assert old.is_symlink() and old.resolve() == new.resolve()
    assert not (new / "devdash.db").exists()


def test_a_new_data_folder_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOMA_HOME")
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    (tmp_path / ".looma").mkdir()
    (tmp_path / ".devdash").mkdir()
    (tmp_path / ".devdash" / "devdash.db").write_text("old")

    paths.home()
    assert (tmp_path / ".devdash" / "devdash.db").read_text() == "old"  # never mixed in


def test_old_names_for_settings_still_count(monkeypatch):
    import importlib

    import looma

    monkeypatch.delenv("LOOMA_CLAUDE_MODEL", raising=False)
    monkeypatch.setenv("DEVDASH_CLAUDE_MODEL", "sonnet")
    importlib.reload(looma)
    import os

    assert os.environ["LOOMA_CLAUDE_MODEL"] == "sonnet"


def test_refresh_replaces_hooks_from_before_the_rename(repo):
    connect_claude(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\necho mine\n{LEGACY_MARKER}\n{OLD_COMMAND} post-commit || true\n")
    settings = json.loads(claude_settings_path().read_text())
    for groups in settings["hooks"].values():
        for group in groups:
            for h in group["hooks"]:
                h["command"] = h["command"].replace(" -m looma record", " -m devdash record")
    claude_settings_path().write_text(json.dumps(settings))

    assert refresh() == []
    text = hook.read_text()
    assert "devdash" not in text and "echo mine" in text
    assert text.count(record_command("post-commit")) == 1
    hooks = json.loads(claude_settings_path().read_text())["hooks"]
    commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert commands and all("-m looma record" in c for c in commands)  # replaced, not doubled
    assert len(hooks["Stop"]) == 1


def test_antigravity_entry_from_before_the_rename_is_replaced(repo):
    path = antigravity_hooks_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"devdash": {"Stop": [{"type": "command", "command": OLD_COMMAND}]}, "other": {}}))
    install_antigravity_hooks()
    config = json.loads(path.read_text())
    assert "devdash" not in config and "looma" in config and "other" in config


def test_a_projects_old_folder_moves_and_its_requests_are_still_read(repo):
    connect_tool(repo, "claude")
    (repo / "app.py").write_text("v = 2\n")
    old = repo / ".devdash" / "turns"
    old.mkdir(parents=True)
    (old / "20261031T183000Z.json").write_text(json.dumps({"prompt": "bump v", "files": [{"path": "app.py", "before": "v = 1\n"}]}))

    refresh()
    assert not (repo / ".devdash").exists() and (repo / ".looma" / "turns").is_dir()
    assert "/.looma/" in (repo / ".git" / "info" / "exclude").read_text()

    # Instructions pasted before the rename keep writing to the old folder: still read.
    from looma.capture import inbox

    again = repo / ".devdash" / "turns"
    again.mkdir(parents=True)
    (repo / "new.py").write_text("x = 1\n")
    (again / "20261031T190000Z.json").write_text(json.dumps({"prompt": "again", "files": [{"path": "new.py", "before": None}]}))
    conn = connect()
    found = inbox.ingest(conn, conn.execute("SELECT id, path FROM repos").fetchone())
    assert found == 2  # the moved one and the new one in the old folder


def test_old_prompt_file_still_becomes_the_commits_turn(repo):
    connect_claude(repo)
    (repo / ".devdash").mkdir()
    (repo / ".devdash" / "prompt.md").write_text("make hello shout\n")
    (repo / "app.py").write_text('def hello():\n    return "HI"\n')
    git(repo, "commit", "-q", "-am", "Shout")
    conn = connect()
    assert conn.execute("SELECT prompt FROM turns").fetchone()["prompt"] == "make hello shout"
    assert not (repo / ".devdash" / "prompt.md").exists()
