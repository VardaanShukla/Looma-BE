"""The Git screen's actions: branches, picking files, commits, updating from the base, stashes,
rebase, push and pull. Each action is planned as the git commands it will run, so the page
can show them before anything happens (a preview runs nothing). Nothing is ever forced onto a
remote. A step that moves a branch, or deletes a branch or a stash, is logged, so the last one
can be undone; a push can't be, and the page says so."""

import json
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from looma import compare, git
from looma.store import connect
from looma.store.db import now

TIMEOUT = 120  # a commit runs the project's own hooks, which can be slow
REMOTE_TIMEOUT = 90
# Actions allowed while a merge or rebase has stopped halfway: the ones that finish it.
MIDWAY_ACTIONS = {"stage", "unstage", "abort", "continue"}
# Where git keeps the state of a step that stopped halfway, and what it is.
MIDWAY_FILES = (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"),
                ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"))
STATES = {"M": "modified", "T": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied"}
CONFLICTS = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


class GitOpsError(ValueError):
    pass


@dataclass
class Plan:
    steps: list[list[str]]
    summary: str  # what it will do, shown with the preview
    done: str  # what it did
    may_conflict: bool = False  # it can stop halfway on conflicting files, which isn't a failure
    finish: Callable[[], None] | None = None  # run once every step worked
    verify: Callable[[str], dict] | None = None  # checks the result with git's output, giving message and url
    notes: list[str] = field(default_factory=list)  # said before it runs, like what can't be undone


# Reading the state.

def status(repo: Path, repo_id: int) -> dict:
    branch = git.branch(repo)
    head = _sha(repo, "HEAD")
    base = compare.default_base(repo)
    remote = _remote(repo)
    upstream = _upstream(repo, branch)
    ahead = behind = 0
    if upstream and head:
        ahead, behind = (int(n) for n in git.run(repo, "rev-list", "--left-right", "--count",
                                                  f"HEAD...{upstream}").split())
    files = _files(repo)
    url = _remote_url(repo, remote) if remote else ""
    return {
        "remote_url": clean_url(url) if url else None,
        "branch_url": remote_web_url(url, upstream.split("/", 1)[1]) if url and upstream else None,
        "branch": branch,
        "head": head,
        "base": base,
        "remote": remote,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "pushed": bool(head) and _on_remote(repo, head),
        "midway": _midway(repo),
        "conflicts": [item["path"] for item in files if item["conflict"]],
        "files": files,
        "stashes": _stashes(repo),
        "branches": _branches(repo, branch, base),
        "remote_branches": _remote_only(repo, remote),
        "last": _last(repo, repo_id),
    }


def _files(repo: Path) -> list[dict]:
    items = git.run(repo, "status", "--porcelain", "-z", "--untracked-files=all").split("\0")
    files, i = [], 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path, old = entry[:2], entry[3:], None
        if code[0] in "RC":  # a rename names the old path next
            old = items[i]
            i += 1
        if code == "??":
            files.append({"path": path, "old_path": None, "staged": None, "unstaged": "new", "conflict": False})
        elif code in CONFLICTS:
            files.append({"path": path, "old_path": None, "staged": None, "unstaged": None, "conflict": True})
        else:
            files.append({"path": path, "old_path": old, "staged": STATES.get(code[0]),
                          "unstaged": STATES.get(code[1]), "conflict": False})
    return files


def _stashes(repo: Path) -> list[dict]:
    out = git.run(repo, "stash", "list", "--format=%H%x1f%gs%x1f%cI%x1e")
    found = []
    for index, record in enumerate(part.strip("\n") for part in out.split("\x1e") if part.strip()):
        sha, message, at = record.split("\x1f")
        found.append({"sha": sha, "index": index, "message": message, "at": at})
    return found


def _branches(repo: Path, current: str | None, base: str | None) -> list[dict]:
    base_ref = compare.freshest(repo, base) if base else None
    out = git.run(repo, "for-each-ref", "--format=%(refname:short)%1f%(upstream:short)%1f%(upstream:track)",
                  "refs/heads")
    found = []
    for line in out.splitlines():
        name, upstream, track = line.split("\x1f")
        unmerged = 0
        if base_ref and name != base:
            unmerged = int(git.run(repo, "rev-list", "--count", f"{base_ref}..{name}").strip())
        found.append({"name": name, "current": name == current, "upstream": upstream or None,
                      "gone": track == "[gone]", "unmerged": unmerged})
    return found


def _remote_only(repo: Path, remote: str | None) -> list[str]:
    """The remote's branches with no local copy, which switching to makes one."""
    if not remote:
        return []
    local = set(compare.branches(repo))
    out = git.run(repo, "for-each-ref", "--format=%(refname:short)", f"refs/remotes/{remote}")
    names = [line.split("/", 1)[1] for line in out.splitlines() if "/" in line]
    return [name for name in names if name != "HEAD" and name not in local]


def _midway(repo: Path) -> str | None:
    for name, kind in MIDWAY_FILES:
        if (repo / git.run(repo, "rev-parse", "--git-path", name).strip()).exists():
            return kind
    return None


def _sha(repo: Path, ref: str) -> str | None:
    return git.run(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", ok=(0, 1)).strip() or None


def _remote(repo: Path) -> str | None:
    names = git.run(repo, "remote").split()
    return "origin" if "origin" in names else (names[0] if names else None)


def _remote_url(repo: Path, remote: str) -> str:
    return git.run(repo, "remote", "get-url", remote, ok=(0, 2, 128)).strip()


def clean_url(url: str) -> str:
    """The remote's address without a login or token that may be written into it."""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^@/]+@", r"\1", url.strip(), flags=re.IGNORECASE)


GITHUB = re.compile(r"^(?:https?://(?:[^@/]+@)?|ssh://git@|git@)github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$", re.IGNORECASE)


def remote_web_url(url: str, branch: str | None = None) -> str | None:
    """The page for a remote's repository, or one of its branches, on GitHub. None for any other host."""
    found = GITHUB.match(url.strip())
    if not found:
        return None
    base = f"https://github.com/{found.group(1)}/{found.group(2)}"
    return f"{base}/tree/{quote(branch, safe='/')}" if branch else base


def _where(repo: Path, remote: str) -> str:
    """The remote as the person knows it: GitHub's owner/name, else its address."""
    url = _remote_url(repo, remote)
    found = GITHUB.match(url)
    return f"GitHub ({found.group(1)}/{found.group(2)})" if found else f"{remote} ({clean_url(url)})"


def _upstream(repo: Path, branch: str | None) -> str | None:
    if not branch:
        return None
    out = git.run(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}", ok=(0, 128))
    upstream = out.strip()
    # A branch whose remote copy was deleted still names it; there's nothing there to compare with.
    return upstream if upstream and _sha(repo, upstream) else None


def _on_remote(repo: Path, sha: str) -> bool:
    return bool(git.run(repo, "branch", "-r", "--contains", sha).strip())


def _local_branch(repo: Path, name: str) -> bool:
    return bool(_sha(repo, f"refs/heads/{name}"))


# The actions. Each plans its commands from the request; nothing runs until perform does.

def _create(repo: Path, repo_id: int, body: dict) -> Plan:
    name = _new_name(repo, body.get("name"))
    came_from = git.branch(repo)
    steps = ["switch", "-c", name]
    where = f"from {came_from}" if came_from else "from here"
    if body.get("start") == "base":
        base = _base(repo, body)
        start = _base_ref(repo, base)
        # --no-track: a branch made from origin/dev would otherwise pull from and compare with dev.
        steps += ["--no-track", start]
        where = f"from {start}"

    def finish():
        _log(repo_id, "branch-create", came_from, None, _sha(repo, name), {"name": name, "from": came_from},
             f"Started the branch {name} {where}")
    return Plan([steps], f"Starts a new branch called {name} {where} and moves you onto it. Changes you haven't "
                         "saved yet come along.", f"Started {name} and moved you onto it.", finish=finish)


def _switch(repo: Path, repo_id: int, body: dict) -> Plan:
    name = _name(body.get("name"))
    if _local_branch(repo, name):
        return Plan([["switch", name]], f"Moves you onto {name}. Changes you haven't saved yet come along.",
                    f"You're now on {name}.")
    remote = _remote(repo)
    if remote and _sha(repo, f"refs/remotes/{remote}/{name}"):
        return Plan([["switch", "-c", name, "--track", f"{remote}/{name}"]],
                    f"Gets {name} from {remote} onto this computer and moves you onto it.", f"You're now on {name}.")
    raise GitOpsError(f"There's no branch called {name}.")


def _rename(repo: Path, repo_id: int, body: dict) -> Plan:
    old = _existing(repo, body.get("name"))
    new = _new_name(repo, body.get("new_name"))
    notes = []
    if _upstream(repo, old):
        notes.append("The copy online keeps its old name; sending this branch again puts it there under the new name.")

    def finish():
        _log(repo_id, "branch-rename", git.branch(repo), None, None, {"old": old, "new": new},
             f"Renamed the branch {old} to {new}")
    return Plan([["branch", "-m", old, new]], f"Renames {old} to {new}.", f"Renamed {old} to {new}.",
                finish=finish, notes=notes)


def _delete(repo: Path, repo_id: int, body: dict) -> Plan:
    name = _existing(repo, body.get("name"))
    if name == git.branch(repo):
        raise GitOpsError(f"{name} is the branch you're on. Switch to another branch first.")
    sha = _sha(repo, f"refs/heads/{name}")
    force = bool(body.get("force"))
    steps = [["branch", "-D" if force else "-d", name]]
    remote, upstream = _remote(repo), _upstream(repo, name)
    on_remote = bool(body.get("remote")) and bool(remote and upstream)
    notes = []
    if on_remote:
        steps.append(["push", remote, "--delete", upstream.split("/", 1)[1]])
        notes.append(f"It's deleted on {remote} too, for everyone. Undo brings back only the copy on this computer.")

    def finish():
        _log(repo_id, "branch-delete", git.branch(repo), sha, None, {"name": name},
             f"Deleted the branch {name}" + (f" here and on {remote}" if on_remote else ""))
    return Plan(steps, f"Deletes {name}" + (f" here and on {remote}." if on_remote else " from this computer."),
                f"Deleted {name}.", finish=finish, notes=notes)


def _stage(repo: Path, repo_id: int, body: dict) -> Plan:
    paths = _paths(body)
    if paths is None:
        return Plan([["add", "."]], "Picks every changed file for the next save.", "Picked every changed file.")
    return Plan([["add", "--", *paths]], f"Picks {_count(paths, 'file')} for the next save.",
                f"Picked {_count(paths, 'file')}.")


def _unstage(repo: Path, repo_id: int, body: dict) -> Plan:
    paths = _paths(body) or ["."]
    if _sha(repo, "HEAD"):
        steps = ["restore", "--staged", "--", *paths]
    else:  # before the first commit there's nothing to restore from
        steps = ["rm", "--cached", "-r", "-q", "--", *paths]
    what = "every file" if paths == ["."] else _count(paths, "file")
    return Plan([steps], f"Leaves {what} out of the next save. The edits stay.", f"Left {what} out.")


def _commit(repo: Path, repo_id: int, body: dict) -> Plan:
    message = _message(body)
    if not _staged(repo):
        raise GitOpsError("No files are picked for the save yet. Tick the files, or press Select all.")
    branch, before = git.branch(repo), _sha(repo, "HEAD")

    def finish():
        _log(repo_id, "commit", branch, before, _sha(repo, "HEAD"), {}, f"Saved “{_first_line(message)}”")
    return Plan([["commit", "-m", message]], "Saves the picked files as one step in your project's history, on " + (branch or "this commit") + ".",
                "Saved.", finish=finish)


def _amend(repo: Path, repo_id: int, body: dict) -> Plan:
    message = _message(body)
    before = _sha(repo, "HEAD")
    if not before:
        raise GitOpsError("There's no commit yet to change.")
    if _on_remote(repo, before):
        raise GitOpsError("Your last save is already sent to the remote. Changing it would rewrite history other "
                          "people have, so Looma won't. Save a new step instead.")
    branch = git.branch(repo)

    def finish():
        _log(repo_id, "amend", branch, before, _sha(repo, "HEAD"), {}, f"Changed the message of the last save to "
             f"“{_first_line(message)}”")
    # --only with no paths changes the message alone, leaving added files for the next commit.
    return Plan([["commit", "--amend", "--only", "-m", message]], "Changes the message of your last save. Its files stay "
                "the same.", "Changed the message.", finish=finish)


def _update(repo: Path, repo_id: int, body: dict) -> Plan:
    branch = _on_branch(repo)
    base = _base(repo, body)
    remote = _remote(repo)
    name = base.split("/", 1)[1] if remote and base.startswith(f"{remote}/") else base
    target = f"{remote}/{name}" if remote and (_sha(repo, f"refs/remotes/{remote}/{name}") or not
                                                _local_branch(repo, name)) else name
    rebase = body.get("how") == "rebase"
    steps = [["fetch", remote]] if remote else []
    steps.append(["rebase", "--autostash", target] if rebase else ["merge", "--no-edit", "--autostash", target])
    before = _sha(repo, "HEAD")

    def finish():
        _log(repo_id, "update", branch, before, _sha(repo, "HEAD"), {},
             f"Brought the latest from {target} into {branch}")
    how = (f"puts this branch's own work on top of the newest {name}, as if it had started there" if rebase
           else f"combines the newest {name} into {branch}")
    lead = f"Downloads the newest work from {remote}, then {how}." if remote else f"{how[0].upper()}{how[1:]}."
    return Plan(steps, lead, f"{branch} now has the latest from {name}.", may_conflict=True, finish=finish)


def _stash(repo: Path, repo_id: int, body: dict) -> Plan:
    message = (body.get("message") or "").strip()
    if not _files(repo):  # git says so but still succeeds
        raise GitOpsError("Nothing to set aside: there are no unsaved changes.")
    steps = ["stash", "push", "--include-untracked"] + (["-m", message] if message else [])
    return Plan([steps], "Puts your unsaved changes on the shelf, new files too, and leaves the files as they were at your last "
                "save.", "Put your changes on the shelf.")


def _stash_apply(repo: Path, repo_id: int, body: dict) -> Plan:
    ref, entry = _stash_ref(repo, body)
    return Plan([["stash", "apply", ref]], f"Brings back “{entry['message']}” and keeps a copy on the shelf.",
                "Brought your changes back.", may_conflict=True)


def _stash_pop(repo: Path, repo_id: int, body: dict) -> Plan:
    ref, entry = _stash_ref(repo, body)
    return Plan([["stash", "pop", ref]], f"Brings back “{entry['message']}” and takes it off the shelf (if a file "
                "conflicts it stays on the shelf).", "Brought your changes back.", may_conflict=True)


def _stash_drop(repo: Path, repo_id: int, body: dict) -> Plan:
    ref, entry = _stash_ref(repo, body)

    def finish():
        _log(repo_id, "stash-drop", git.branch(repo), None, None, {"sha": entry["sha"], "message": entry["message"]},
             f"Threw away the shelved changes “{entry['message']}”")
    return Plan([["stash", "drop", ref]], f"Throws away “{entry['message']}”.", "Threw them away.", finish=finish)


def _push(repo: Path, repo_id: int, body: dict) -> Plan:
    branch = _on_branch(repo)
    remote = _remote(repo)
    if not remote:
        raise GitOpsError("This project isn't connected to GitHub or any other remote, so there's nowhere to send it.")
    if not _sha(repo, "HEAD"):
        raise GitOpsError("There's nothing to push before the first commit.")
    notes = ["This can't be undone from Looma: once sent, everyone with access to the repository can see it."]

    def verify(output: str) -> dict:
        # Git's exit code only says it didn't fail. Ask the remote what it has now.
        head = _sha(repo, "HEAD")
        where = _where(repo, remote)
        try:
            listed = _git(repo, ["ls-remote", "--heads", remote, f"refs/heads/{branch}"]).stdout.split()
        except GitOpsError:
            return {"message": f"Sent {branch} to {where}. Looma couldn't double-check what's there."}
        there = listed[0] if listed else None
        url = remote_web_url(_remote_url(repo, remote), branch)
        if there == head:
            if "up-to-date" in output.lower():
                return {"message": f"Nothing new to send: {where} already has everything on {branch}.", "url": url}
            return {"message": f"Sent {branch} to {where}. It's there now.", "url": url}
        if there:
            return {"message": f"Git said it sent it, but {where} has an older {branch} ({there[:7]} instead of {head[:7]}). "
                               "The remote may have rules for this branch."}
        return {"message": f"Git said it sent it, but {where} doesn't list {branch}. Check the remote's rules for it."}
    if _upstream(repo, branch):
        return Plan([["push"]], f"Sends the saved changes on this branch to {remote}.", f"Sent {branch}.", notes=notes,
                    verify=verify)
    return Plan([["push", "--set-upstream", remote, branch]], f"Puts {branch} on {remote} for the first time, and "
                "remembers it for next time.", f"Sent {branch}.", notes=notes, verify=verify)


def _pull(repo: Path, repo_id: int, body: dict) -> Plan:
    branch = _on_branch(repo)
    upstream = _upstream(repo, branch)
    if not upstream:
        raise GitOpsError(f"{branch} isn't online yet, so there's nothing to get. Send it first.")
    rebase = body.get("how") == "rebase"
    steps = ["pull", "--rebase", "--autostash"] if rebase else ["pull", "--no-rebase", "--no-edit", "--autostash"]
    before = _sha(repo, "HEAD")

    def finish():
        _log(repo_id, "pull", branch, before, _sha(repo, "HEAD"), {}, f"Got the latest for {branch} from {upstream}")
    return Plan([steps], f"Downloads what's new on {upstream} and " + ("puts this branch's own work on top of it."
                if rebase else "combines it into this branch."), f"{branch} now has the latest.",
                may_conflict=True, finish=finish)


def _abort(repo: Path, repo_id: int, body: dict) -> Plan:
    kind = _midway(repo)
    if not kind:
        raise GitOpsError("Nothing has stopped halfway, so there's nothing to abort.")
    return Plan([[kind, "--abort"]], f"Stops the {kind} and puts everything back as it was before it started.",
                f"Stopped the {kind}.")


def _continue(repo: Path, repo_id: int, body: dict) -> Plan:
    kind = _midway(repo)
    if not kind:
        raise GitOpsError("Nothing has stopped halfway, so there's nothing to continue.")
    left = [item["path"] for item in _files(repo) if item["conflict"]]
    if left:
        raise GitOpsError(f"{_count(left, 'file')} still {'has' if len(left) == 1 else 'have'} conflicts: "
                          f"{', '.join(left[:5])}. Fix them in your editor, then press Mark fixed.")
    branch, before = git.branch(repo), _sha(repo, "ORIG_HEAD")
    step = ["commit", "--no-edit"] if kind == "merge" else [kind, "--continue"]

    def finish():
        if kind in ("merge", "rebase") and _midway(repo) is None:
            _log(repo_id, "update", branch or git.branch(repo), before, _sha(repo, "HEAD"), {},
                 f"Finished the {kind} on {branch or git.branch(repo)}")
    return Plan([step], f"Finishes the {kind} with the fixed files.", f"Finished the {kind}.", may_conflict=True,
                finish=finish)


def _undo(repo: Path, repo_id: int, body: dict) -> Plan:
    row = _last_row(repo_id)
    if row is None:
        raise GitOpsError("There's nothing to undo.")
    steps, why = _undo_steps(repo, row)
    if steps is None:
        raise GitOpsError(why)

    def finish():
        conn = connect()
        with conn:
            conn.execute("UPDATE git_actions SET undone_at = ? WHERE id = ?", (now(), row["id"]))
        conn.close()
    return Plan(steps, f"Undoes: {row['summary']}.", f"Undid: {row['summary']}.", finish=finish)


ACTIONS: dict[str, Callable[[Path, int, dict], Plan]] = {
    "branch-create": _create, "switch": _switch, "branch-rename": _rename, "branch-delete": _delete,
    "stage": _stage, "unstage": _unstage, "commit": _commit, "amend": _amend, "update": _update,
    "stash": _stash, "stash-apply": _stash_apply, "stash-pop": _stash_pop, "stash-drop": _stash_drop,
    "push": _push, "pull": _pull, "abort": _abort, "continue": _continue, "undo": _undo,
}


def perform(repo: Path, repo_id: int, action: str, body: dict, preview: bool = False) -> dict:
    """Runs one action, or with `preview` only says what it would run. Stops at the first
    command that fails; a merge, rebase or stash that stops on conflicts isn't a failure, and
    says which files conflict."""
    planner = ACTIONS.get(action)
    if planner is None:
        raise GitOpsError(f"Looma has no git action called {action}.")
    kind = _midway(repo)
    if kind and action not in MIDWAY_ACTIONS:
        raise GitOpsError(f"A {kind} stopped halfway. Finish it (fix the files, then Continue) or Abort it first.")
    plan = planner(repo, repo_id, body)
    result = {"commands": [_shown(step) for step in plan.steps], "summary": plan.summary, "notes": plan.notes,
              "done": False, "message": plan.summary, "conflicts": [], "url": None}
    if preview:
        return result
    output = ""
    for step in plan.steps:
        ran = _git(repo, step)
        output += f"{ran.stdout}\n{ran.stderr}\n"
        if ran.returncode != 0:
            conflicts = [item["path"] for item in _files(repo) if item["conflict"]]
            if plan.may_conflict and conflicts:
                return {**result, "conflicts": conflicts,
                        "message": f"Git stopped: {_count(conflicts, 'file')} changed on both sides. Fix them in "
                                   "your editor, then press Mark fixed" + (" and Continue." if _midway(repo) else ".")}
            raise GitOpsError(_plain(ran))
    if plan.finish:
        plan.finish()
    checked = plan.verify(output) if plan.verify else {}
    return {**result, "done": True, "message": checked.get("message", plan.done), "url": checked.get("url")}


def staged_diff(repo: Path) -> tuple[str, list[str]]:
    """What the next commit holds, for writing its message."""
    files = [name for name in git.run(repo, "diff", "--cached", "--name-only", "-z").split("\0") if name]
    if not files:
        raise GitOpsError("No files are picked for the save yet. Tick the files, or press Select all.")
    return git.run(repo, "diff", "--cached", "--stat") + "\n" + git.run(repo, "diff", "--cached"), files


# The log, and undoing its last step.

def _log(repo_id: int, action: str, branch: str | None, before: str | None, after: str | None, detail: dict,
         summary: str) -> None:
    conn = connect()
    with conn:
        conn.execute("INSERT INTO git_actions (repo_id, action, branch, before_sha, after_sha, detail, summary, at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (repo_id, action, branch, before, after, json.dumps(detail), summary, now()))
    conn.close()


def _last_row(repo_id: int):
    conn = connect()
    row = conn.execute("SELECT * FROM git_actions WHERE repo_id = ? ORDER BY id DESC LIMIT 1", (repo_id,)).fetchone()
    conn.close()
    return row if row is not None and row["undone_at"] is None else None


def _last(repo: Path, repo_id: int) -> dict | None:
    row = _last_row(repo_id)
    if row is None:
        return None
    steps, why = _undo_steps(repo, row)
    return {"id": row["id"], "action": row["action"], "summary": row["summary"], "at": row["at"],
            "undoable": steps is not None, "why_not": why}


def _undo_steps(repo: Path, row) -> tuple[list[list[str]] | None, str | None]:
    """The commands that undo a logged step, or None and why it can't be undone any more."""
    action, branch, before, after = row["action"], row["branch"], row["before_sha"], row["after_sha"]
    detail = json.loads(row["detail"])
    if action in ("commit", "amend", "update", "pull"):
        if git.branch(repo) != branch or _sha(repo, "HEAD") != after:
            return None, f"{branch} has moved on since, so Looma can't undo that step."
        if action in ("commit", "amend") and _on_remote(repo, after):
            return None, "That commit is already pushed, so undoing it here would only confuse things."
        if action in ("commit", "amend"):
            # --soft: the commit's changes go back to being added, ready to commit again.
            return ([["reset", "--soft", before]] if before else [["update-ref", "-d", "HEAD"]]), None
        # --keep refuses rather than lose uncommitted changes.
        return [["reset", "--keep", before]], None
    if action == "branch-create":
        name = detail["name"]
        if _sha(repo, f"refs/heads/{name}") != after:
            return None, f"{name} has new commits or is gone, so Looma won't delete it."
        back = [["switch", detail["from"]]] if git.branch(repo) == name and detail["from"] else []
        if git.branch(repo) == name and not back:
            return None, f"{name} is checked out and there's no branch to go back to."
        return back + [["branch", "-D", name]], None
    if action == "branch-rename":
        if not _local_branch(repo, detail["new"]) or _local_branch(repo, detail["old"]):
            return None, "The branches have changed since, so Looma can't rename it back."
        return [["branch", "-m", detail["new"], detail["old"]]], None
    if action == "branch-delete":
        if _local_branch(repo, detail["name"]):
            return None, f"There's a branch called {detail['name']} again."
        return [["branch", detail["name"], before]], None
    if action == "stash-drop":
        return [["stash", "store", "-m", detail["message"], detail["sha"]]], None
    return None, "Looma can't undo that step."


# Running git, and saying what went wrong in plain words.

def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    remote = args[0] in ("fetch", "push", "pull", "ls-remote")
    # Never wait for an editor (merge and rebase messages are kept as git writes them) or a password.
    env = {**git.remote_env(), "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"}
    try:
        return subprocess.run(["git", *args], check=False, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, stdin=subprocess.DEVNULL,
                              timeout=REMOTE_TIMEOUT if remote else TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GitOpsError("Reaching the remote took too long. Check your connection and try again." if remote
                          else f"git {args[0]} took too long and was stopped.")


PLAIN = [
    (re.compile(r"would be overwritten by (checkout|merge|switch)|Please commit your changes or stash them", re.IGNORECASE),
     "Some of your unsaved changes would be lost. Save them, or put them on the shelf, first."),
    (re.compile(r"not fully merged", re.IGNORECASE),
     "That branch has commits that aren't merged anywhere. Tick Delete anyway to delete it and those commits."),
    (re.compile(r"already exists", re.IGNORECASE), "A branch with that name already exists."),
    (re.compile(r"\[rejected\]|non-fast-forward|fetch first|Updates were rejected", re.IGNORECASE),
     "The remote has newer work than this branch. Get the latest first, then send again."),
    (re.compile(r"No local changes to save", re.IGNORECASE), "Nothing to set aside: there are no unsaved changes."),
    (re.compile(r"nothing (added )?to commit", re.IGNORECASE), "No files are picked for the save yet."),
    (re.compile(r"could not read Username|Authentication failed|Permission denied|Could not read from remote|"
                r"terminal prompts disabled|Host key verification failed", re.IGNORECASE),
     ("Looma couldn't sign in to the remote. Run `git fetch` once in a terminal, where git can ask for your "
      "login, then try again.")),
    (re.compile(r"Could not resolve host|unable to access|Connection (timed out|refused)", re.IGNORECASE),
     "Couldn't reach the remote. Check your connection and try again."),
]


def _plain(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    for pattern, message in PLAIN:
        if pattern.search(text):
            return message
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("hint:")]
    return lines[-1] if lines else "git failed without saying why."


def _shown(step: list[str]) -> str:
    return shlex.join(["git", *step])


# Checking what the page sent.

def _name(value) -> str:
    name = (value or "").strip() if isinstance(value, str) else ""
    if not name:
        raise GitOpsError("Type a branch name.")
    if name.startswith("-"):
        raise GitOpsError("A branch name can't start with -.")
    return name


def _new_name(repo: Path, value) -> str:
    name = _name(value)
    if git.run(repo, "check-ref-format", "--branch", name, ok=(0, 1, 128)).strip() != name:
        raise GitOpsError(f"“{name}” isn't a name git allows for a branch. Use letters, numbers, - _ / and . "
                          "without spaces.")
    if _local_branch(repo, name):
        raise GitOpsError(f"There's already a branch called {name}.")
    return name


def _existing(repo: Path, value) -> str:
    name = _name(value)
    if not _local_branch(repo, name):
        raise GitOpsError(f"There's no branch called {name}.")
    return name


def _on_branch(repo: Path) -> str:
    branch = git.branch(repo)
    if not branch:
        raise GitOpsError("You're not on a branch (git calls this a detached HEAD). Switch to a branch first.")
    return branch


def _base(repo: Path, body: dict) -> str:
    base = (body.get("base") or "").strip() or compare.default_base(repo)
    if not base:
        raise GitOpsError("This project has no main, master, develop or dev branch. Pick the base branch.")
    if base.startswith("-") or not _sha(repo, base):
        raise GitOpsError(f"There's no branch called {base}.")
    return base


def _base_ref(repo: Path, base: str) -> str:
    """The newest copy of the base: origin/dev when it's ahead of dev."""
    return compare.freshest(repo, base)


def _message(body: dict) -> str:
    message = (body.get("message") or "").strip()
    if not message:
        raise GitOpsError("Write a commit message.")
    return message


def _paths(body: dict) -> list[str] | None:
    paths = body.get("paths")
    if paths is None:
        return None
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise GitOpsError("Pick at least one file.")
    return paths


def _staged(repo: Path) -> bool:
    return subprocess.run(["git", "diff", "--cached", "--quiet"], check=False, cwd=repo,
                          capture_output=True).returncode == 1


def _stash_ref(repo: Path, body: dict) -> tuple[str, dict]:
    # The page names a stash by its commit, since the numbers shift when one is added or dropped.
    sha = body.get("sha")
    for entry in _stashes(repo):
        if entry["sha"] == sha:
            return f"stash@{{{entry['index']}}}", entry
    raise GitOpsError("Those shelved changes aren't there any more. The list changed; look again.")


def _count(items: list, word: str) -> str:
    return f"1 {word}" if len(items) == 1 else f"{len(items)} {word}s"


def _first_line(message: str) -> str:
    line = message.splitlines()[0]
    return line if len(line) <= 60 else line[:57] + "…"
