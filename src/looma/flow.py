"""The project's recent history across branches: which commits exist, how they connect,
and where each branch points. The UI draws it as a flow of branches forking and merging."""

from pathlib import Path

from looma import git
from looma.capture.record import AI_TRAILER

LIMIT = 60


def flow(repo: Path, limit: int = LIMIT) -> dict:
    """The newest `limit` commits reachable from any branch, newest first, each with its
    parents and the branches pointing at it. A remote branch is left out when a local
    branch of the same name exists, so each branch shows once."""
    local = _refs(repo, "refs/heads")
    remote = [name for name in _refs(repo, "refs/remotes")
              if not name.endswith("/HEAD") and name.split("/", 1)[-1] not in local]
    full = [f"refs/heads/{name}" for name in local] + [f"refs/remotes/{name}" for name in remote]
    if not full:
        return {"commits": [], "truncated": False}

    tips: dict[str, list[str]] = {}
    out = git.run(repo, "for-each-ref", "--format=%(objectname)%00%(refname:short)", *full)
    for line in out.splitlines():
        sha, _, name = line.partition("\x00")
        tips.setdefault(sha, []).append(name)

    out = git.run(repo, "log", "--topo-order", f"-n{limit + 1}",
                  "--format=%H%x00%P%x00%an%x00%aI%x00%s%x00%b%x1e", *full, "--")
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, author, authored_at, subject, body = record.split("\x00", 5)
        commits.append({
            "sha": sha,
            "short_sha": sha[:7],
            "parents": parents.split(),
            "author": author,
            "authored_at": authored_at,
            "subject": subject,
            "ai": bool(AI_TRAILER.search(body)),
            "branches": tips.get(sha, []),
        })
    return {"commits": commits[:limit], "truncated": len(commits) > limit}


def _refs(repo: Path, prefix: str) -> list[str]:
    out = git.run(repo, "for-each-ref", "--format=%(refname)", prefix)
    return [line.removeprefix(prefix + "/") for line in out.splitlines() if line]
