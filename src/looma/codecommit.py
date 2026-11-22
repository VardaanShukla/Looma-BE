"""Your AWS CodeCommit repositories, read through the AWS CLI with one of your AWS profiles.

Looma keeps no AWS keys. It remembers only which profile and region you picked, and asks
the `aws` tool to sign each request with that profile, the same way `aws codecommit ...`
does in a terminal. So whatever the profile uses (access keys in ~/.aws/credentials, IAM
Identity Center/SSO, a role to assume) works here too, and what Looma can see is what
the profile's IAM permissions allow.

With it Looma lists the repositories in that region (to clone one as a project) and shows
a project's open pull requests. A clone is set up to sign git with the same profile
(`aws codecommit credential-helper`), so fetching it later works without a git password."""

import configparser
import datetime
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from looma.store import connect

SETTING = "codecommit"
TIMEOUT = 30
MAX_REPOS = 500
MAX_PULLS = 30
# batch-get-repositories takes at most this many names at once.
BATCH = 25
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d")
NAME = re.compile(r"[\w.-]{1,100}")
PROFILE = re.compile(r"[\w.@+=,/-]{1,128}")

# Every spelling of a CodeCommit remote: HTTPS (git credentials or the credential helper),
# SSH, and git-remote-codecommit's codecommit:// with or without a region and profile.
_REMOTES = (
    re.compile(r"^(?:https|ssh)://(?:[^@/]+@)?git-codecommit(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/v1/repos/([\w.-]+?)/?$"),
    re.compile(r"^codecommit::([a-z0-9-]+)://(?:[^@/]+@)?([\w.-]+)$"),
    re.compile(r"^codecommit://(?:[^@/]+@)?([\w.-]+)$"),
)


class CodeCommitError(RuntimeError):
    def __init__(self, message: str, signed_out: bool = False):
        super().__init__(message)
        self.signed_out = signed_out


def status() -> dict:
    """Whether the AWS CLI is installed and a profile connected, as which AWS identity, and
    the profiles this computer has to pick from (with the region each is set to)."""
    saved = _saved()
    return {
        "cli": _find_aws() is not None,
        "connected": saved is not None,
        "profile": saved.get("profile") if saved else None,
        "region": saved.get("region") if saved else None,
        "account": saved.get("account") if saved else None,
        "arn": saved.get("arn") if saved else None,
        "profiles": profiles(),
    }


def profiles() -> list[dict]:
    """The profiles in ~/.aws/config and ~/.aws/credentials (or where AWS_CONFIG_FILE and
    AWS_SHARED_CREDENTIALS_FILE point), default first. Read straight from the files, which
    holds no secret for the page: only names and regions leave this function."""
    found: dict[str, str | None] = {}
    home = Path.home() / ".aws"
    for path, prefixed in ((Path(os.environ.get("AWS_CONFIG_FILE") or home / "config"), True),
                           (Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or home / "credentials"), False)):
        parser = configparser.RawConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        for section in parser.sections():
            name = section[len("profile "):].strip() if prefixed and section.startswith("profile ") else section.strip()
            if prefixed and section != "default" and not section.startswith("profile "):
                continue  # sso-session and services sections aren't profiles
            region = parser.get(section, "region", fallback=None)
            if name not in found or (found[name] is None and region):
                found[name] = region
    names = sorted(found, key=lambda n: (n != "default", n.lower()))
    return [{"name": n, "region": found[n]} for n in names]


def use_profile(profile: str, region: str) -> dict:
    """Connects with an AWS profile and region, once AWS says who the profile is and that it
    may list CodeCommit repositories there."""
    profile, region = profile.strip(), region.strip()
    if not PROFILE.fullmatch(profile):
        raise CodeCommitError("Pick one of your AWS profiles.")
    if not REGION.fullmatch(region):
        raise CodeCommitError("That doesn't look like an AWS region. They look like us-east-1 or ap-south-1.")
    if _find_aws() is None:
        raise CodeCommitError("The AWS CLI isn't installed on this computer. Install it from aws.amazon.com/cli, "
                              "run aws configure, then come back.")
    who = _aws("sts", "get-caller-identity", profile=profile, region=region)
    # Checked now so a profile without CodeCommit access says so here, not later on every screen.
    _aws("codecommit", "list-repositories", "--max-items", "1", profile=profile, region=region)
    _save({"profile": profile, "region": region, "account": who.get("Account"), "arn": who.get("Arn")})
    return status()


def disconnect() -> dict:
    """Forgets the profile here. The profile itself, and any clone already set up to use it,
    stay as they are."""
    conn = connect()
    with conn:
        conn.execute("DELETE FROM settings WHERE name = ?", (SETTING,))
    conn.close()
    return status()


def repos(tracked: dict[str, str] | None = None) -> list[dict]:
    """Every repository in the connected region, newest change first. `tracked` maps a
    repository's name to the folder of a Looma project that is a clone of it."""
    saved = _need()
    tracked = {k.lower(): v for k, v in (tracked or {}).items()}
    names = [r["repositoryName"] for r in _aws("codecommit", "list-repositories", "--sort-by", "lastModifiedDate",
                                               "--order", "descending").get("repositories", [])][:MAX_REPOS]
    found = []
    for start in range(0, len(names), BATCH):
        batch = _aws("codecommit", "batch-get-repositories", "--repository-names", *names[start:start + BATCH])
        for repo in batch.get("repositories", []):
            name = repo["repositoryName"]
            found.append({
                "name": name,
                "description": repo.get("repositoryDescription"),
                "default_branch": repo.get("defaultBranch"),
                "url": _console(saved["region"], f"repositories/{name}/browse"),
                "clone_url": repo.get("cloneUrlHttp") or _https(saved["region"], name),
                "updated_at": _when(repo.get("lastModifiedDate")),
                "tracked": tracked.get(name.lower()),
            })
    return sorted(found, key=lambda r: r["updated_at"] or "", reverse=True)


def clone(name: str, folder: Path) -> Path:
    """Clones the repository into `folder`/<its name> and returns where. The clone is told
    to ask `aws codecommit credential-helper` for git's password with the connected profile,
    so it holds the profile's name but no key, and later fetches sign themselves."""
    saved = _need()
    if not NAME.fullmatch(name) or name in (".", ".."):
        raise CodeCommitError("That isn't a CodeCommit repository name.")
    target = folder / name
    if target.exists():
        raise CodeCommitError(f"{target} already exists. Pick another folder, or add that one as a project.")
    folder.mkdir(parents=True, exist_ok=True)
    # The empty helper first clears any other helper (a keychain, say) for this clone, which
    # would otherwise answer with a stale CodeCommit password before the AWS one is asked.
    helper = f"!{_quote(_find_aws() or 'aws')} --profile {_quote(saved['profile'])} codecommit credential-helper $@"
    settings = ["-c", "credential.helper=", "-c", f"credential.helper={helper}", "-c", "credential.UseHttpPath=true"]
    try:
        result = subprocess.run(["git", "clone", "--quiet", *settings, _https(saved["region"], name), str(target)],
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "AWS_PAGER": ""},
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                                stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise CodeCommitError("The clone took more than ten minutes and was stopped.")
    if result.returncode != 0:
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        raise CodeCommitError(f"git couldn't clone {name}: {lines[-1] if lines else 'no reason given'}")
    return target


def pulls(name: str) -> list[dict]:
    """The repository's open pull requests, newest activity first. CodeCommit lists only
    their ids, so each is read on its own; the newest MAX_PULLS are enough for a screen."""
    saved = _need()
    ids = _aws("codecommit", "list-pull-requests", "--repository-name", name, "--pull-request-status", "OPEN",
               "--max-items", str(MAX_PULLS)).get("pullRequestIds", [])
    found = []
    for pull_id in ids:
        pull = _aws("codecommit", "get-pull-request", "--pull-request-id", str(pull_id)).get("pullRequest") or {}
        target = next((t for t in pull.get("pullRequestTargets") or [] if t.get("repositoryName") == name),
                      (pull.get("pullRequestTargets") or [{}])[0])
        found.append({
            "number": int(pull.get("pullRequestId", pull_id)),
            "title": pull.get("title") or "(no title)",
            "author": _author(pull.get("authorArn")),
            "branch": _branch(target.get("sourceReference")),
            "base": _branch(target.get("destinationReference")),
            "draft": False,  # CodeCommit has no drafts
            "url": _console(saved["region"], f"repositories/{name}/pull-requests/{pull_id}/details"),
            "updated_at": _when(pull.get("lastActivityDate") or pull.get("creationDate")),
        })
    return sorted(found, key=lambda p: p["updated_at"] or "", reverse=True)


def remote_name(repo: Path) -> tuple[str | None, str] | None:
    """(region, name) of the project's CodeCommit remote (origin first), or None when it has
    none. The region is None for a codecommit:// remote that leaves it to the profile."""
    try:
        out = subprocess.run(["git", "remote", "-v"], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    remotes = [line.split()[:2] for line in out.splitlines() if len(line.split()) >= 2]
    remotes.sort(key=lambda r: r[0] != "origin")
    for _, url in remotes:
        found = parse_remote(url.strip())
        if found:
            return found
    return None


def parse_remote(url: str) -> tuple[str | None, str] | None:
    for pattern in _REMOTES:
        found = pattern.match(url)
        if found:
            groups = found.groups()
            return (groups[0], groups[1]) if len(groups) == 2 else (None, groups[0])
    return None


def region() -> str | None:
    saved = _saved()
    return saved["region"] if saved else None


def _need() -> dict:
    saved = _saved()
    if saved is None:
        raise CodeCommitError("Connect an AWS profile first (Settings → CodeCommit).", signed_out=True)
    return saved


def _find_aws() -> str | None:
    found = shutil.which("aws")
    if found:
        return found
    places = [Path("/usr/local/bin/aws"), Path("/opt/homebrew/bin/aws"),
              Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Amazon" / "AWSCLIV2" / "aws.exe"]
    return next((str(p) for p in places if p.is_file()), None)


def _aws(*args: str, profile: str | None = None, region: str | None = None) -> dict:
    """Runs one `aws` command with the connected profile (or the one given) and returns its
    JSON answer. AWS's own words for a failure are kept, since they name the missing
    permission or the expired SSO sign-in."""
    if profile is None:
        saved = _need()
        profile, region = saved["profile"], saved["region"]
    tool = _find_aws()
    if tool is None:
        raise CodeCommitError("The AWS CLI isn't installed on this computer any more. Install it from aws.amazon.com/cli.")
    try:
        result = subprocess.run([tool, *args, "--profile", profile, "--region", region, "--output", "json"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT,
                                stdin=subprocess.DEVNULL, env={**os.environ, "AWS_PAGER": ""})
    except subprocess.TimeoutExpired:
        raise CodeCommitError("AWS didn't answer within 30 seconds. Check this computer is online.")
    except OSError as error:
        raise CodeCommitError(f"Couldn't run the AWS CLI ({error}).")
    if result.returncode != 0:
        raise CodeCommitError(_reason(result.stderr, profile))
    try:
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        raise CodeCommitError("The AWS CLI answered with something that isn't JSON.")


def _reason(said: str, profile: str) -> str:
    """What went wrong, in AWS's words, with the fix when it's a usual one."""
    lines = [line.strip() for line in said.splitlines() if line.strip()]
    text = lines[-1] if lines else "no reason given"
    text = re.sub(r"^An error occurred \((\w+)\) when calling the \w+ operation: ", r"\1: ", text)
    if re.search(r"sso|token has expired|refresh failed", said, re.I):
        return f"The AWS sign-in for {profile} has run out. Run aws sso login --profile {profile}, then try again."
    if "could not be found" in said and "profile" in said:
        return f"There is no AWS profile called {profile} on this computer any more."
    if re.search(r"Unable to locate credentials|InvalidClientTokenId|SignatureDoesNotMatch", said):
        return f"AWS didn't accept the keys of the {profile} profile. Check them with aws configure --profile {profile}."
    if "AccessDenied" in said:
        return f"The {profile} profile isn't allowed to do that in CodeCommit. {text}"
    return f"AWS said: {text}"


def _quote(word: str) -> str:
    """A word for the shell git runs the credential helper in."""
    return word if re.fullmatch(r"[\w./:@+=,-]+", word) else "'" + word.replace("'", "'\\''") + "'"


def _https(region: str, name: str) -> str:
    return f"https://git-codecommit.{region}.amazonaws.com/v1/repos/{name}"


def _console(region: str, page: str) -> str:
    return f"https://{region}.console.aws.amazon.com/codesuite/codecommit/{page}?region={region}"


def _author(arn: str | None) -> str | None:
    """The person's name from their ARN: the user, or the session of an assumed role."""
    return arn.rsplit("/", 1)[-1] if arn else None


def _branch(ref: str | None) -> str | None:
    return ref.removeprefix("refs/heads/") if ref else None


def _when(value) -> str | None:
    """AWS CLI v2 writes times as ISO text; v1 writes seconds since 1970."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()
    return str(value)


def _saved() -> dict | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (SETTING,)).fetchone()
    conn.close()
    try:
        saved = json.loads(row["value"]) if row else None
    except json.JSONDecodeError:
        return None
    return saved if isinstance(saved, dict) and saved.get("profile") and saved.get("region") else None


def _save(saved: dict) -> None:
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (SETTING, json.dumps(saved)))
    conn.close()
