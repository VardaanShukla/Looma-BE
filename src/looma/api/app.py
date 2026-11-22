"""The local HTTP API the UI is built on. Listens on 127.0.0.1 only: it serves your code.
The response models below are the contract; FastAPI publishes them at /openapi.json."""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from looma import auth
from looma import compare as compare_module
from looma import digest as digest_module
from looma import explain as explain_module
from looma import ktdoc
from looma import flow as flow_module
from looma import functions as functions_module
from looma import preferences as preferences_module
from looma import retention
from looma import codecommit
from looma import git
from looma import gitops
from looma import github
from looma import projectmap
from looma import providers
from looma import summary as summary_module
from looma import undo as undo_module
from looma.capture import install
from looma.store import connect

# The built UI, served by the API itself so `looma serve` is all it takes. Missing in a
# checkout where it hasn't been built (scripts/build_ui.py builds it).
WEB = Path(__file__).resolve().parent.parent / "web"
# The UI from Vite's dev server (5173) and its preview of a build (4173), when working on it.
DEV_ORIGINS = [f"http://{host}:{port}" for port in (5173, 4173) for host in ("localhost", "127.0.0.1")]
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
# Answered without signing in: whether a password is set, signing in, and setting the
# first password (that one checks for itself). The page's own files are open too: they
# hold no data, and the page asks for the sign-in.
OPEN_PATHS = {"/api/auth", "/api/auth/login", "/api/auth/password"}
PRIVATE_PAGES = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


class Repo(BaseModel):
    id: int
    name: str
    path: str
    exists: bool  # the folder is still there
    git: bool  # it's a git repository; without git, branches, changes, PRs and AI recording are off
    branch: str | None
    claude: bool  # records an AI tool's changes (the one in `ai`)
    ai: Literal["claude", "codex", "antigravity"] | None


class HookError(BaseModel):
    at: str
    event: str
    detail: str


class RecordingHealth(BaseModel):
    errors: int  # hook calls that failed in the last `days` days
    days: int
    last_error: HookError | None
    last_call: dict[str, str]  # event -> when a hook last called it
    error_log: str
    hook_log: str
    problems: list[str]  # what needs fixing, in plain sentences; empty when recording works
    repairable: bool  # some of the problems are hooks that Repair reinstalls
    stuck: int  # prompts left open for hours by a session that ended; closed just now
    tools: list[str]  # the AI tools some project records


class Candidate(BaseModel):
    path: str
    name: str
    folder: str  # the remembered folder it was found in


class NewRepo(BaseModel):
    path: str
    ai: Literal["claude", "codex", "antigravity"] | None = None


class AiChoice(BaseModel):
    ai: Literal["claude", "codex", "antigravity"] | None


class AiTool(BaseModel):
    id: str
    name: str
    available: bool
    description: str


class SetupStep(BaseModel):
    id: str
    title: str
    detail: str
    command: str | None
    done: bool | None


class Setup(BaseModel):
    steps: list[SetupStep]


class RepoState(BaseModel):
    signature: str


class BranchInfo(BaseModel):
    name: str
    merged: bool
    gone: bool
    # When Looma stops listing a merged or deleted branch; None for an open one.
    removes_at: str | None = None


class Branches(BaseModel):
    current: str | None
    default_base: str | None
    branches: list[str]
    details: list[BranchInfo]


class Sync(BaseModel):
    synced_at: str | None
    error: str | None


class Commit(BaseModel):
    sha: str
    short_sha: str
    author: str
    authored_at: str
    subject: str
    ai: bool


class FlowCommit(BaseModel):
    sha: str
    short_sha: str
    parents: list[str]
    author: str
    authored_at: str
    subject: str
    ai: bool
    # Branches whose tip is this commit; remote ones as origin/name.
    branches: list[str]


class Flow(BaseModel):
    current: str | None
    base: str | None
    commits: list[FlowCommit]
    # More history exists beyond the commits returned.
    truncated: bool


class Language(BaseModel):
    name: str
    files: int


class MapFile(BaseModel):
    path: str
    bytes: int


class MapArea(BaseModel):
    # The folder, relative to the project root; "" for files at the root.
    id: str
    name: str
    path: str
    # What the folder is usually for, guessed from its name; null when unknown.
    role: str | None
    files: int
    bytes: int
    languages: list[Language]
    # Its biggest files, at most 150; files counts them all.
    file_list: list[MapFile]


class MapEdge(BaseModel):
    # Code in source uses code in target.
    source: str
    target: str
    imports: int


class ProjectMap(BaseModel):
    revision: str
    total_files: int
    areas: list[MapArea]
    edges: list[MapEdge]
    # False when the project was too big to read every file for imports.
    complete: bool


class Line(BaseModel):
    kind: Literal["add", "delete", "context"]
    text: str
    old: int | None
    new: int | None


class Hunk(BaseModel):
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    context: str | None
    lines: list[Line]


class Function(BaseModel):
    name: str
    change: Literal["added", "deleted", "modified"]


class FileChange(BaseModel):
    path: str
    old_path: str | None
    status: Literal["added", "deleted", "modified", "renamed"]
    binary: bool
    generated: bool
    truncated: bool
    additions: int
    deletions: int
    functions: list[Function]
    hunks: list[Hunk]
    # Differs from the last commit (only set when uncommitted work is included).
    uncommitted: bool


class Totals(BaseModel):
    files: int
    additions: int
    deletions: int


class Comparison(BaseModel):
    base: str
    # What was actually compared against: origin/Dev when it is newer than Dev.
    base_ref: str
    head: str
    # Where the branch's own work starts: where it left the base, or where it was made from
    # another branch that isn't merged yet.
    merge_base: str
    uncommitted: bool
    ahead: int
    behind: int
    commits: list[Commit]
    files: list[FileChange]
    totals: Totals


class UnitSide(BaseModel):
    start: int
    end: int
    lines: int
    code: str
    # The declaration line, as written.
    signature: str
    # The docstring or the comment right above it.
    doc: str | None
    params: list[str]
    returns: str | None
    is_async: bool
    # Functions and methods it calls, by name.
    calls: list[str]
    # Ifs, loops, catches and the like: the choices the code makes.
    decisions: int


class UnitFacts(BaseModel):
    params_added: list[str]
    params_removed: list[str]
    calls_added: list[str]
    calls_removed: list[str]
    # Each pair is [before, after].
    lines: list[int]
    decisions: list[int]
    is_async: list[bool]
    returns: list[str | None]
    doc_changed: bool
    signature_changed: bool


class Use(BaseModel):
    path: str
    line: int
    text: str


class FunctionExplanation(BaseModel):
    summary: str
    change: str
    improves: list[str]
    tradeoffs: list[str]
    check: str | None


class FunctionDetail(BaseModel):
    name: str
    # style: a rule in a style sheet; section: part of a doc; file: a whole file of
    # another kind (settings, HTML, SQL, a script), or edits outside every function.
    kind: Literal["function", "method", "component", "class", "type", "style", "section", "file"]
    change: Literal["added", "deleted", "modified"]
    before: UnitSide | None
    after: UnitSide | None
    # Only for a changed function: what differs between before and after.
    facts: UnitFacts | None
    # Other places its name appears, in the branch's files.
    used_by: list[Use]
    # Saved from an earlier Explain, if there is one.
    explanation: FunctionExplanation | None
    # For a whole file: the lines that changed, as a unified diff.
    diff: str | None = None


class FileFunctions(BaseModel):
    path: str
    functions: list[FunctionDetail]


class ExplainFunctionRequest(BaseModel):
    path: str
    old_path: str | None = None
    name: str
    base: str | None = None
    head: str | None = None
    uncommitted: bool = False


class InstallCommand(BaseModel):
    shell: str  # where to run it: Terminal, PowerShell or Command Prompt
    command: str


class AiModel(BaseModel):
    id: str
    label: str
    note: str = ""  # what it's good for, or its size


class HostedServer(BaseModel):
    url: str  # where the model is served, without any password in it
    style: Literal["openai", "ollama"]  # OpenAI-style /v1/chat/completions, or Ollama's /api/chat
    key: str | None  # only the key's last four characters, or "set"; never the key
    headers: list[str]  # the names of the headers sent with each request
    unencrypted: bool  # plain http to another computer: code goes over the network unencrypted


class HostedConnect(BaseModel):
    # The curl command that reaches the model, as pasted; it is read, never run.
    curl: str | None = None
    # Or the parts typed in: the address, an API key, and the model.
    url: str | None = None
    key: str | None = None
    model: str | None = None


class AiStatus(BaseModel):
    provider: str = "claude"  # claude, codex, ollama, antigravity or hosted (your own server)
    name: str = "Claude"
    tool: str
    available: bool  # installed and ready to answer (Ollama: running, with a model)
    signed_in: bool | None  # None when the tool couldn't say
    account: str | None = None  # the signed-in account's email (or organization)
    model: str  # the model explanations use: the one picked in Settings, or the default
    detail: str | None  # when not available, what to do: "Install …", "Start Ollama", "Download a model"
    install: list[InstallCommand] = []  # the official commands for that, for this computer
    login: Literal["button", "terminal", "none"] | None = None  # how it signs in
    login_command: str | None = None  # the command that signs in (for "terminal": run it yourself)
    models: list[AiModel] = []  # cheapest first; may be empty when the tool can't list them
    default_model: str | None = None
    server: HostedServer | None = None  # hosted only: the connected server


class ExplainerChoice(BaseModel):
    provider: Literal["claude", "codex", "ollama", "antigravity", "hosted"]
    models: dict[str, str]  # the model picked for each AI; missing: its default


class ExplainerChoiceChange(BaseModel):
    provider: Literal["claude", "codex", "ollama", "antigravity", "hosted"] | None = None
    models: dict[str, str] | None = None  # only the AIs to change; "" goes back to the default


class SignInStart(BaseModel):
    provider: Literal["claude", "codex", "ollama", "antigravity"] = "claude"


class SignIn(BaseModel):
    state: Literal["idle", "waiting", "signed_in", "failed"]
    url: str | None  # the sign-in page, in case the browser didn't open
    detail: str | None


class SignInCode(BaseModel):
    code: str


class Readme(BaseModel):
    title: str | None
    intro: str | None
    first_sentence: str | None
    sections: list[str]


class Tech(BaseModel):
    name: str
    what: str


class RunCommand(BaseModel):
    where: str
    name: str
    command: str


class Person(BaseModel):
    name: str
    commits: int


class History(BaseModel):
    commits: int
    people: list[Person]
    first: str | None
    last: str | None
    # Commits in each of the last 12 weeks, oldest first.
    weeks: list[int]
    branches: int


class PartExplanation(BaseModel):
    name: str
    what: str


class ProjectFlow(BaseModel):
    name: str
    steps: list[str]


class StartHere(BaseModel):
    path: str
    why: str


class ProjectExplanation(BaseModel):
    overview: str
    how_it_works: list[str]
    parts: list[PartExplanation]
    # Main things the project does, step by step, naming the files involved.
    flows: list[ProjectFlow] = []
    # The files a new team member should read first, in order.
    start_here: list[StartHere] = []
    state: str


class FunctionNote(BaseModel):
    name: str
    what: str


class FileExplanation(BaseModel):
    purpose: str
    functions: list[FunctionNote]
    connects: str
    notes: list[str]


class KtFile(BaseModel):
    path: str
    part: str
    language: str
    bytes: int
    explanation: FileExplanation | None


class KnowledgeTransfer(BaseModel):
    """Every file of the project a KT explains, with the notes written so far."""
    files: list[KtFile]
    explained: int


class KtDetailFile(BaseModel):
    path: str
    part: str  # the id of its part (see KtPart); tests are all in "checks"
    language: str
    lines: int
    bytes: int
    depends_on: list[str]  # project files this one imports
    used_by: list[str]  # project files that import this one, tests left out
    tests: list[str]  # test files that import this one
    flow: str  # where it sits, from its imports: "Starts the app", "Used by 3 files", ...
    role: str  # Starts the app, Screen, Logic, Data, Helper, Settings, Docs or Check
    own_note: str | None  # what the file says about itself at its top, word for word
    explanation: FileExplanation | None  # the AI's note, when one has been written


class KtPart(BaseModel):
    id: str  # its folder, "" for the main folder, "checks" for the tests
    folder: str
    name: str  # the AI's plain name once written, else the folder's
    what: str
    files: int
    lines: int
    languages: list[str]


class KtPartLink(BaseModel):
    source: str  # code in this part uses code in target
    target: str
    imports: int


class KtReadingStep(BaseModel):
    path: str
    why: str
    after: str | None  # "After this you will understand ...", once the AI has written it


class KtFlowStep(BaseModel):
    path: str
    what: str
    link: str | None  # how it joins the step before: "uses" or "used by", read from the imports


class KtFlow(BaseModel):
    name: str
    steps: list[KtFlowStep]


class KtTerm(BaseModel):
    term: str
    meaning: str


class KtFound(BaseModel):
    name: str
    files: list[str]


class KtRoute(BaseModel):
    method: str
    path: str
    file: str


class KtCommand(BaseModel):
    name: str
    does: str
    file: str


class KtCaution(BaseModel):
    path: str
    note: str


class KtSource(BaseModel):
    kind: str  # "branch" or "worktree" (the working folder, with changes not committed yet)
    branch: str | None
    commit: str | None


class KtSize(BaseModel):
    files: int
    lines: int
    languages: list[Language]


class KtDetail(BaseModel):
    """The detailed KT. Everything but the words is read from the code; the words are the AI's
    (summary, part names, reading reasons, flows, glossary), kept only where the code bears them
    out, and empty until written."""
    source: KtSource
    revision: str
    worktree: bool
    has_changes: bool  # the working folder differs from its last commit
    complete: bool  # false when the project had too many files to read every import
    written: bool  # the AI's words are written for the project as it is
    summary: str
    audience: str
    problem: str
    size: KtSize
    parts: list[KtPart]
    links: list[KtPartLink]
    reading: list[KtReadingStep]
    flows: list[KtFlow]
    glossary: list[KtTerm]
    tables: list[KtFound]
    settings: list[KtFound]
    storage: list[KtFound]
    routes: list[KtRoute]
    commands: list[KtCommand]
    cautions: list[KtCaution]
    files: list[KtDetailFile]
    explained: int


class KtDetailRequest(BaseModel):
    head: str | None = None
    worktree: bool | None = None  # read the working folder; by default when it has changes
    fresh: bool = False


class ExplainFileRequest(BaseModel):
    path: str
    head: str | None = None
    fresh: bool = False  # ask again even when a note is saved; the old one stays if this fails
    worktree: bool = False  # the file as it is in the working folder, not as committed


class Summary(BaseModel):
    revision: str
    name: str
    description: str | None
    readme: Readme | None
    license: str | None
    tech: list[Tech]
    other_dependencies: int
    run: list[RunCommand]
    entry_points: list[str]
    files: int
    bytes: int
    languages: list[Language]
    parts: list[MapArea]
    links: list[MapEdge]
    history: History
    explanation: ProjectExplanation | None


class CommandStep(BaseModel):
    step: str
    detail: str = ""  # what the step does and what to watch for
    command: str | None


class GuideNeed(BaseModel):
    name: str
    why: str
    check: str | None  # the command that shows it's installed


class RunCommand(BaseModel):
    what: str
    command: str


class GuideSetting(BaseModel):
    name: str
    what: str


class GuideImage(BaseModel):
    path: str
    caption: str


class GuideFunction(BaseModel):
    name: str
    path: str
    what: str
    use: str
    signature: str | None = None


class KtGuide(BaseModel):
    """The short KT: what the project is, how to get it running, and its major functions."""
    overview: str
    needs: list[GuideNeed]
    setup: list[CommandStep]
    run: list[RunCommand]
    how_to_use: list[str]
    settings: list[GuideSetting]
    start_here: list[StartHere]
    functions: list[GuideFunction]
    images: list[GuideImage]


class KtGuideSaved(BaseModel):
    guide: KtGuide | None


class ExplainProjectRequest(BaseModel):
    head: str | None = None
    # Write it again, as when the file-by-file notes have filled in since.
    fresh: bool = False


class GitHubStatus(BaseModel):
    configured: bool  # Looma's GitHub App is set up (its client ID is known)
    gh: bool  # GitHub's gh tool is installed, to sign in through
    connected: bool
    via: Literal["app", "gh", "token"] | None  # how the account was connected
    login: str | None
    name: str | None
    avatar: str | None
    install_url: str | None  # where to pick the repositories Looma may read
    # While signing in: the code to enter at verify_url, and when it runs out (Unix time).
    code: str | None
    verify_url: str | None
    expires_at: int | None


class GitHubRepo(BaseModel):
    full_name: str
    private: bool
    description: str | None
    default_branch: str | None
    url: str | None
    clone_url: str | None
    updated_at: str | None
    tracked: str | None  # the folder of the Looma project cloned from it


class GitHubConnect(BaseModel):
    method: Literal["app", "gh"] = "app"


class GitHubToken(BaseModel):
    token: str


class CloneRequest(BaseModel):
    full_name: str
    folder: str  # the folder to clone into; the clone gets its own folder inside it


class PullRequest(BaseModel):
    number: int
    title: str
    author: str | None
    branch: str | None
    base: str | None
    draft: bool
    url: str
    updated_at: str | None


class CodeCommitProfile(BaseModel):
    name: str
    region: str | None  # the region the profile is set to, if any


class CodeCommitStatus(BaseModel):
    cli: bool  # the AWS CLI is installed, to sign requests with
    connected: bool
    profile: str | None
    region: str | None
    account: str | None  # the AWS account the profile is in
    arn: str | None  # who the profile is, as AWS names it
    profiles: list[CodeCommitProfile]  # the profiles on this computer, to pick from


class CodeCommitConnect(BaseModel):
    profile: str
    region: str


class CodeCommitRepo(BaseModel):
    name: str
    description: str | None
    default_branch: str | None
    url: str | None  # the repository in the AWS console
    clone_url: str | None
    updated_at: str | None
    tracked: str | None  # the folder of the Looma project cloned from it


class CodeCommitClone(BaseModel):
    name: str
    folder: str  # the folder to clone into; the clone gets its own folder inside it


class PullRequests(BaseModel):
    repo: str | None  # owner/name on GitHub or the name on CodeCommit; None when the project has neither remote
    # Where the pull requests live; None when the project has neither remote.
    host: Literal["github", "codecommit"] | None = None
    connected: bool
    pulls: list[PullRequest]
    error: str | None


class AskTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class AskRequest(BaseModel):
    question: str
    # The conversation so far, oldest first; only the last few turns are sent to the AI.
    history: list[AskTurn] = []
    head: str | None = None


class AskAnswer(BaseModel):
    answer: str
    files: list[str]  # the files the answer is about
    read: list[str]  # the files whose code the AI read for it
    follow_ups: list[str] = []  # questions to ask next
    explained: int  # KT files with a note, which the AI also read
    total: int




class DigestFlag(BaseModel):
    kind: str  # auth, payments, database, tests, secret: worth a look; config, deleted, big: worth knowing
    label: str
    reason: str
    line: str | None = None  # the changed line behind it, when there is one; secrets are masked


class DigestFunction(BaseModel):
    name: str
    change: str  # added, deleted or modified


class DigestFile(BaseModel):
    path: str
    status: str
    additions: int
    deletions: int
    functions: list[DigestFunction]
    flags: list[DigestFlag]
    watched: bool  # changed during the prompt, not by Claude's Edit or Write: maybe by hand


class DigestCommit(BaseModel):
    sha: str
    message: str
    count: int  # commits the prompt's files landed in


class DigestRepo(BaseModel):
    id: int
    name: str


class DigestChange(BaseModel):
    id: str
    repo: DigestRepo
    branch: str | None
    prompt: str | None
    prompt_recorded: bool  # False: an AI-co-authored commit; prompt is its message
    origin: Literal["prompt", "commit", "cloud"]  # cloud: a Claude commit found on a branch, no hook saw it made
    tool: str | None = None  # the AI tool the prompt went to: Claude, Codex or Antigravity
    at: str
    commit: DigestCommit | None  # None while any of it isn't committed
    files: list[DigestFile]
    additions: int
    deletions: int
    flags: list[DigestFlag]
    risky: bool
    undone_at: str | None  # undone from the digest, and not put back since


class DigestClaude(BaseModel):
    prompts: int
    files: int
    functions: int
    additions: int
    deletions: int
    risky: int
    uncommitted: int
    undone: int  # listed but left out of the numbers above
    watched_files: int  # changed during a prompt but not by its Edit or Write


class DigestYou(BaseModel):
    files: int
    additions: int
    deletions: int
    commits: int


class ChangeRef(BaseModel):
    change: str  # a digest change's id


class GitFile(BaseModel):
    path: str
    old_path: str | None
    staged: str | None  # how it changed in the next commit: added, modified, deleted, renamed, copied
    unstaged: str | None  # how it changed since, not added yet; "new" for a file git doesn't track
    conflict: bool  # changed on both sides of a merge, rebase or stash


class GitStash(BaseModel):
    sha: str
    index: int
    message: str
    at: str


class GitBranch(BaseModel):
    name: str
    current: bool
    upstream: str | None
    gone: bool  # its remote copy was deleted
    unmerged: int  # commits not in the base branch


class GitLastAction(BaseModel):
    id: int
    action: str
    summary: str
    at: str
    undoable: bool
    why_not: str | None


class GitStatus(BaseModel):
    branch: str | None  # None on a detached HEAD
    head: str | None  # None before the first commit
    base: str | None
    remote: str | None
    upstream: str | None
    ahead: int  # commits to push
    behind: int  # commits to pull
    pushed: bool  # the last commit is on the remote
    midway: Literal["merge", "rebase", "cherry-pick", "revert"] | None  # a step that stopped on conflicts
    conflicts: list[str]
    files: list[GitFile]
    stashes: list[GitStash]
    branches: list[GitBranch]
    remote_branches: list[str]  # on the remote with no copy here
    last: GitLastAction | None  # the Git screen's last step, which Undo takes back


class GitRequest(BaseModel):
    preview: bool = False  # only say which commands it would run
    name: str | None = None
    new_name: str | None = None
    start: Literal["current", "base"] = "current"
    force: bool = False
    remote: bool = False  # deleting a branch: on the remote too
    paths: list[str] | None = None  # None: every file
    message: str | None = None
    how: Literal["merge", "rebase"] = "merge"
    base: str | None = None
    sha: str | None = None  # a stash, by its commit


class GitResult(BaseModel):
    commands: list[str]
    summary: str
    notes: list[str]
    done: bool  # false for a preview, and when it stopped on conflicts
    message: str
    conflicts: list[str]


class CommitSuggestion(BaseModel):
    message: str


class Undone(BaseModel):
    change: str
    undone_at: str | None


class DigestUnit(BaseModel):
    name: str
    kind: Literal["function", "file"]  # file: the whole file, when it has no functions
    change: Literal["added", "deleted", "modified"]
    diff: str  # only this part's lines, as a unified diff
    explanation: FunctionExplanation | None


class DigestChangeFile(DigestFile):
    hunks: list[Hunk]
    units: list[DigestUnit]


class DigestChangeDetail(DigestChange):
    files: list[DigestChangeFile]


class ExplainUnitRequest(BaseModel):
    change: str  # a digest change's id
    path: str
    name: str


class Digest(BaseModel):
    date: str
    claude: DigestClaude
    you: DigestYou
    changes: list[DigestChange]


class NewUsername(BaseModel):
    username: str
    current: str  # the current password


class OllamaModel(BaseModel):
    name: str
    size: int  # bytes on disk
    parameters: str | None  # e.g. "3.1B"
    family: str | None
    loaded: bool  # in memory now, so it answers at once
    until: str | None  # when it's unloaded if unused


class OllamaPull(BaseModel):
    name: str
    status: str  # Ollama's own words: "pulling manifest", "verifying sha256 digest", "success"
    completed: int  # bytes so far of the part downloading now
    total: int
    error: str | None
    done: bool


class OllamaState(BaseModel):
    installed: bool
    running: bool
    version: str | None
    host: str  # where Looma looks for it (OLLAMA_HOST, or 127.0.0.1:11434)
    started_here: bool  # started from Looma
    # What runs it, so Start and Stop go through the same thing: looma, service (Linux's
    # systemd service: Linux asks for your password), app (macOS, Windows) or command.
    managed_by: Literal["looma", "service", "app", "command"] | None
    # Starting or stopping takes an administrator's password (the Linux service): the page
    # asks first, and sends confirm.
    needs_admin: bool
    local: bool  # on this computer; one OLLAMA_HOST points elsewhere is started and stopped there
    models: list[OllamaModel]  # smallest first
    pulls: list[OllamaPull]  # downloads under way, or just finished
    install: list[InstallCommand]  # when it isn't installed


class GpuUse(BaseModel):
    name: str
    percent: float  # busy, 0-100
    memory_used: int  # bytes
    memory_total: int


class LoadedModel(BaseModel):
    name: str
    size: int  # bytes in memory
    on_gpu: float  # share on the GPU, 0-1, as `ollama ps` shows it


class MemoryUse(BaseModel):
    used: int
    total: int


class OllamaUsage(BaseModel):
    gpus: list[GpuUse]  # empty: no GPU Looma can read, so the CPU is shown
    cpu: float | None  # busy, 0-100
    cores: int
    memory: MemoryUse | None
    models: list[LoadedModel]  # loaded in memory now
    note: str | None  # why the CPU is shown instead of a GPU


class SuggestedModel(BaseModel):
    name: str
    size_gb: float  # Ollama's download size
    note: str
    fit: Literal["gpu", "mixed", "cpu", "too_big"]  # where it would run on this computer


class OllamaAdvice(BaseModel):
    cpu: str | None
    threads: int
    memory_gb: float | None
    gpu: str | None  # the biggest one Looma can read
    gpu_memory_gb: float | None
    recommended: str  # the suggested model that suits this computer best
    reason: str
    models: list[SuggestedModel]


class OllamaSwitch(BaseModel):
    # The person said yes, on the page, to Looma asking for their password (pkexec) to run
    # systemctl start or stop ollama. Without it, a service is never started or stopped.
    confirm: bool = False


class ModelName(BaseModel):
    name: str


class AuthStatus(BaseModel):
    password_set: bool
    signed_in: bool
    username: str | None = None  # only for a signed-in browser


class HomeWidget(BaseModel):
    id: str
    type: str  # what it shows: greeting, ai-today, total-changes, … (preferences.WIDGETS)
    x: int  # its column (0 to 11) and row on Home's 12-column grid, and its size in columns and rows
    y: int
    w: int
    h: int
    style: str = "glass"  # glass, solid, outline, tinted or bare
    accent: str = ""  # the colour its highlights take; "" is the theme's
    title: str = ""  # the person's own title; "" is the widget's usual one
    options: dict[str, bool | int | float | str] = {}  # the widget's own settings


class HomeLayout(BaseModel):
    widgets: list[HomeWidget]


class Preferences(BaseModel):
    theme: Literal["dark", "light"]
    uncommitted: bool | None  # show uncommitted work from the start; None: not picked yet
    keep_diffs_days: int  # days a change's lines are kept; older ones keep prompt and counts; 0: for good
    tour_done: bool  # the first-time walkthrough was finished or skipped
    shortcuts: dict[str, str]  # action (view:<screen>, theme, uncommitted, sync, next-file, previous-file) -> key
    ai: ExplainerChoice  # which AI writes explanations, and with which model
    home: HomeLayout  # Home's widgets, each placed on the grid with its style and settings


class PreferenceChange(BaseModel):
    theme: Literal["dark", "light"] | None = None
    uncommitted: bool | None = None
    keep_diffs_days: int | None = Field(None, ge=0, le=3650)
    tour_done: bool | None = None
    shortcuts: dict[str, str] | None = None  # only the actions to change; "" turns one off
    ai: ExplainerChoiceChange | None = None
    home: HomeLayout | None = None


class Login(BaseModel):
    username: str
    password: str


class NewPassword(BaseModel):
    # Needed the first time; leave it out to keep the current username.
    username: str | None = None
    password: str
    current: str | None = None


def create_app() -> FastAPI:
    app = FastAPI(title="Looma", version="0.1.0")
    sync_locks: dict[int, threading.Lock] = {}
    sync_results: dict[int, dict] = {}
    git_locks: dict[int, threading.Lock] = {}
    app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["*"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # The API can change git hooks and Claude settings, so it answers only requests
        # addressed to this machine (a rebound DNS name fails here) and, for changes,
        # only JSON from no page or the Looma UI.
        if (request.url.hostname or "") not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Looma only answers on localhost"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin not in DEV_ORIGINS and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse({"detail": "request from another site refused"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "send JSON"}, status_code=415)
        # With a password set, everything else needs a signed-in browser.
        path = request.url.path
        private = path == "/api" or path.startswith("/api/") or path in PRIVATE_PAGES
        if (private and path not in OPEN_PATHS and request.method != "OPTIONS" and auth.password_set()
                and not auth.signed_in(request.cookies.get(auth.COOKIE))):
            return JSONResponse({"detail": "sign in to Looma"}, status_code=401)
        return await call_next(request)

    @app.get("/api/auth", response_model=AuthStatus, response_model_exclude_none=True)
    def auth_status(request: Request):
        """Whether a password is set, and whether this browser is signed in (and as whom)."""
        signed_in = auth.signed_in(request.cookies.get(auth.COOKIE))
        return AuthStatus(password_set=auth.password_set(), signed_in=signed_in,
                          username=auth.username() if signed_in else None)

    @app.post("/api/auth/login", response_model=AuthStatus, response_model_exclude_none=True)
    def log_in(body: Login, response: Response):
        try:
            token = auth.log_in(body.username, body.password)
        except auth.TooManyTries as error:
            raise HTTPException(429, str(error))
        except auth.AuthError as error:
            raise HTTPException(401, str(error))
        _set_login(response, token)
        return AuthStatus(password_set=True, signed_in=True)

    @app.post("/api/auth/logout", response_model=AuthStatus, response_model_exclude_none=True)
    def log_out(request: Request, response: Response):
        auth.log_out(request.cookies.get(auth.COOKIE))
        response.delete_cookie(auth.COOKIE, path="/")
        return AuthStatus(password_set=auth.password_set(), signed_in=False)

    @app.put("/api/auth/password", response_model=AuthStatus, response_model_exclude_none=True)
    def set_password(body: NewPassword, request: Request, response: Response):
        """Sets the first username and password, or changes them from a signed-in browser with
        the current password. Signs this browser in, and every other one out."""
        if auth.password_set() and not auth.signed_in(request.cookies.get(auth.COOKIE)):
            raise HTTPException(401, "sign in to Looma")
        try:
            auth.set_password(body.password, body.current, name=body.username)
        except auth.AuthError as error:
            raise HTTPException(400, str(error))
        _set_login(response, auth.new_login())
        return AuthStatus(password_set=True, signed_in=True)

    @app.put("/api/auth/username", response_model=AuthStatus, response_model_exclude_none=True)
    def set_username(body: NewUsername):
        """Changes the username, with the current password."""
        try:
            auth.set_username(body.username, body.current)
        except auth.AuthError as error:
            raise HTTPException(400, str(error))
        return AuthStatus(password_set=True, signed_in=True, username=auth.username())

    @app.get("/api/repos", response_model=list[Repo])
    def list_repos():
        conn = connect()
        rows = conn.execute("SELECT id, name, path, claude, ai FROM repos ORDER BY name").fetchall()
        conn.close()
        return [_repo(row) for row in rows]

    @app.post("/api/repos", response_model=Repo, status_code=201)
    def add_repo(body: NewRepo):
        path = _folder(body.path)
        if not path.is_dir():
            raise HTTPException(400, f"No folder at {path}")
        # A folder without git is added without recording: that needs git's commit hook.
        # The reply's `git: false` lets the page say so.
        record = body.ai if git.toplevel(path) is not None else None
        root = _install((lambda p: install.connect_tool(p, record)) if record else install.init, path)
        return _repo(_row_for(root))

    @app.get("/api/recording/health", response_model=RecordingHealth)
    def recording_health():
        """Whether the hooks have been called lately, and how many failed, with the logs' paths."""
        from looma.capture import record

        return record.health()

    @app.post("/api/recording/repair", response_model=RecordingHealth)
    def repair_recording():
        """Reinstalls the hooks of every AI tool in use so they call this install of Looma."""
        from looma.capture import install, record

        problems = install.refresh()
        if problems:
            raise HTTPException(409, "; ".join(problems))
        return record.health()

    @app.get("/api/repos/candidates", response_model=list[Candidate])
    def repo_candidates():
        """Projects in the folders `looma init` remembered, not added yet, to pick from."""
        return install.candidates()

    @app.delete("/api/repos/{repo_id}", status_code=204)
    def remove_repo(repo_id: int):
        """Stops tracking the project. Its files and git history are not touched."""
        if not _install(lambda _: install.remove(repo_id), None):
            raise HTTPException(404, "repo not tracked")
        sync_results.pop(repo_id, None)

    @app.put("/api/repos/{repo_id}/ai", response_model=Repo)
    def set_ai(repo_id: int, body: AiChoice):
        path = _git_path(repo_id, "Recording an AI tool") if body.ai else _repo_path(repo_id)
        _install((lambda p: install.connect_tool(p, body.ai)) if body.ai else install.disconnect, path)
        return _repo(_row_for(path))

    @app.get("/api/repos/{repo_id}/setup", response_model=Setup)
    def setup(repo_id: int):
        path = _repo_path(repo_id)
        return Setup(steps=install.setup_steps(path, _ai(_row_for(path))))

    @app.get("/api/ai-tools", response_model=list[AiTool])
    def ai_tools():
        return install.AI_TOOLS

    @app.get("/api/repos/{repo_id}/state", response_model=RepoState)
    def repo_state(repo_id: int, uncommitted: bool = Query(False, description="Also watch uncommitted edits")):
        path = _repo_path(repo_id)
        if git.toplevel(path) is None:
            return RepoState(signature=projectmap.folder_state(path))
        return RepoState(signature=compare_module.state(path, uncommitted))

    @app.get("/api/repos/{repo_id}/branches", response_model=Branches)
    def list_branches(repo_id: int):
        path = _git_path(repo_id, "Branches")
        base = compare_module.default_base(path)
        current = git.branch(path)
        # Merged and deleted branches go after a while; see compare.retire.
        details = compare_module.retire(str(repo_id), compare_module.branch_details(path, base), current)
        shown = {branch["name"] for branch in details}
        return Branches(current=current, default_base=base,
                        branches=[name for name in compare_module.branches(path) if name in shown], details=details)

    @app.post("/api/repos/{repo_id}/sync", response_model=Sync)
    def sync(repo_id: int):
        """Fetches from the remote so merges and deleted branches on GitHub show up.
        Several open tabs share one fetch per repo."""
        path = _repo_path(repo_id)
        if git.toplevel(path) is None:
            return Sync(synced_at=None, error=None)  # nothing to fetch without git
        lock = sync_locks.setdefault(repo_id, threading.Lock())
        if lock.acquire(blocking=False):
            try:
                error = git.fetch(path)
                last = sync_results.get(repo_id, {})
                sync_results[repo_id] = {
                    "synced_at": last.get("synced_at") if error else datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "error": error,
                }
            finally:
                lock.release()
        else:
            with lock:  # another request is fetching; wait for its result
                pass
        return Sync(**sync_results.get(repo_id, {"synced_at": None, "error": None}))

    @app.get("/api/repos/{repo_id}/git", response_model=GitStatus)
    def git_status(repo_id: int):
        """What the Git screen shows: files, branches, stashes, whether anything stopped halfway."""
        return gitops.status(_git_path(repo_id, "The Git screen"), repo_id)

    @app.post("/api/repos/{repo_id}/git/suggest-message", response_model=CommitSuggestion)
    def git_suggest_message(repo_id: int):
        """A commit message for what's added to the next commit, written by the AI picked in Settings."""
        path = _git_path(repo_id, "The Git screen")
        try:
            diff, files = gitops.staged_diff(path)
            return CommitSuggestion(message=explain_module.commit_message(diff, files))
        except gitops.GitOpsError as error:
            raise HTTPException(409, str(error))
        except explain_module.ExplainError as error:
            raise _explain_failed(error)

    @app.post("/api/repos/{repo_id}/git/{action}", response_model=GitResult)
    def git_action(repo_id: int, action: str, body: GitRequest):
        """Runs one git step (or with preview, says which commands it would run). One at a
        time per project; 409 with a plain reason when git refuses."""
        path = _git_path(repo_id, "The Git screen")
        lock = git_locks.setdefault(repo_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "Another git step is still running in this project. Wait for it to finish.")
        try:
            return gitops.perform(path, repo_id, action, body.model_dump(), preview=body.preview)
        except gitops.GitOpsError as error:
            raise HTTPException(409, str(error))
        finally:
            lock.release()

    @app.get("/api/repos/{repo_id}/flow", response_model=Flow)
    def flow(repo_id: int, limit: int = Query(flow_module.LIMIT, ge=1, le=300)):
        """Recent commits across all branches, for drawing how the branches fork and merge."""
        path = _git_path(repo_id, "The branch flow")
        return Flow(current=git.branch(path), base=compare_module.default_base(path),
                    **flow_module.flow(path, limit))

    @app.get("/api/repos/{repo_id}/map", response_model=ProjectMap)
    def project_map(repo_id: int, head: str | None = Query(None, description="Defaults to the checked-out branch")):
        """The project's main parts and which parts use which, as committed on the branch."""
        path = _repo_path(repo_id)
        try:
            return projectmap.project_map(path, head or "HEAD")
        except git.GitError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/repos/{repo_id}/compare", response_model=Comparison)
    def compare(
        repo_id: int,
        base: str | None = Query(None, description="Defaults to main, or master"),
        head: str | None = Query(None, description="Defaults to the checked-out branch"),
        uncommitted: bool = Query(False, description="Include edits not committed yet"),
    ):
        path = _git_path(repo_id, "Comparing branches")
        try:
            return compare_module.compare(path, base, head, uncommitted)
        except compare_module.CompareError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/repos/{repo_id}/functions", response_model=FileFunctions)
    def file_functions(
        repo_id: int,
        path: str = Query(..., description="The file, relative to the project"),
        old_path: str | None = Query(None, description="Its name before a rename"),
        base: str | None = None,
        head: str | None = None,
        uncommitted: bool = False,
    ):
        """Each function the branch changed in one file, before and after, with the facts
        git can show and any explanation written earlier."""
        repo = _git_path(repo_id, "Changed functions")
        changes = _function_changes(repo, path, old_path, base, head, uncommitted)
        for item in changes:
            item["explanation"] = explain_module.cached("function", _function_subject(path, item))
        return FileFunctions(path=path, functions=changes)

    @app.post("/api/repos/{repo_id}/explain/function", response_model=FunctionExplanation)
    def explain_function(repo_id: int, request: ExplainFunctionRequest):
        """Asks Claude Code what a changed function does, what it improves and what it costs."""
        repo = _git_path(repo_id, "Explaining a changed function")
        changes = _function_changes(repo, request.path, request.old_path, request.base, request.head,
                                    request.uncommitted)
        item = next((c for c in changes if c["name"] == request.name), None)
        if item is None:
            raise HTTPException(404, f"{request.name} didn't change in {request.path}")
        head = request.head or git.branch(repo) or "HEAD"
        base_ref = compare_module.freshest(repo, request.base or compare_module.default_base(repo))
        start = compare_module.fork_point(repo, base_ref, head)
        context = {"commits": [c["subject"] for c in compare_module.commits(repo, f"{start}..{head}")],
                   "used_by": item["used_by"], "helpers": item.get("helpers") or []}
        try:
            return explain_module.explain("function", _function_subject(request.path, item), context)
        except explain_module.ExplainError as error:
            raise _explain_failed(error)

    @app.get("/api/ai", response_model=AiStatus)
    def ai_status():
        """The AI picked in Settings to write explanations (Claude unless changed): whether it's
        installed and signed in, how to set it up, and the model it uses."""
        return explain_module.active_status()

    @app.get("/api/ai/providers", response_model=list[AiStatus])
    def ai_providers():
        """Every AI that can write explanations, each with its state and models."""
        return explain_module.all_status()

    @app.post("/api/ai/hosted", response_model=AiStatus)
    def connect_hosted(body: HostedConnect):
        """Connects a model on your own server from the curl command that reaches it (or its
        address, key and model), once it has answered a test question, and picks it to write
        explanations. The curl command is only read, never run."""
        try:
            if body.curl and body.curl.strip():
                connection = providers.parse_curl(body.curl)
                if body.model and body.model.strip():
                    connection["model"] = body.model.strip()[:100]
            else:
                connection = providers.hosted_connection(body.url or "", model=body.model, key=body.key)
            checked = providers.check_hosted(connection)
        except providers.ProviderError as error:
            raise HTTPException(400, str(error))
        checked.pop("models")
        providers.save_hosted(checked)
        preferences_module.update({"ai": {"provider": "hosted", "models": {"hosted": checked["model"]}}})
        return explain_module.provider_status("hosted")

    @app.delete("/api/ai/hosted", response_model=AiStatus)
    def forget_hosted():
        """Forgets the server and its key; explanations go back to Claude if it wrote them."""
        providers.forget_hosted()
        if preferences_module.ai_choice()[0] == "hosted":
            preferences_module.update({"ai": {"provider": "claude", "models": {"hosted": ""}}})
        return explain_module.active_status()

    @app.get("/api/ollama", response_model=OllamaState)
    def ollama_state():
        """Ollama on this computer: running or not, its models, and downloads under way."""
        return providers.ollama_state()

    def _ollama(action, *args) -> dict:
        try:
            action(*args)
        except providers.NeedsPermission as error:
            raise HTTPException(409, str(error))
        except providers.ProviderError as error:
            raise HTTPException(400, str(error))
        return providers.ollama_state()

    @app.get("/api/ollama/usage", response_model=OllamaUsage)
    def ollama_usage():
        """The GPU's use and memory (or the CPU's, with no GPU), and how much of each loaded
        model is on the GPU. Read every couple of seconds by the chart in Settings."""
        return providers.ollama_usage()

    @app.get("/api/ollama/advice", response_model=OllamaAdvice)
    def ollama_advice():
        """This computer's CPU, memory and GPU, and the local model that suits them."""
        return providers.ollama_advice()

    @app.post("/api/ollama/start", response_model=OllamaState)
    def ollama_start(body: OllamaSwitch | None = None):
        """Starts Ollama the way it's set up here: its service (with your OK, then your
        password), its app, or `ollama serve`."""
        return _ollama(providers.start_ollama, (body or OllamaSwitch()).confirm)

    @app.post("/api/ollama/stop", response_model=OllamaState)
    def ollama_stop(body: OllamaSwitch | None = None):
        """Stops Ollama however it was started (its service with your OK, then your password)."""
        return _ollama(providers.stop_ollama, (body or OllamaSwitch()).confirm)

    @app.post("/api/ollama/pull", response_model=OllamaState)
    def ollama_pull(body: ModelName):
        """Starts downloading a model; GET /api/ollama shows its progress."""
        return _ollama(providers.pull_model, body.name)

    @app.post("/api/ollama/load", response_model=OllamaState)
    def ollama_load(body: ModelName):
        """Loads a model into memory, as `ollama run` does."""
        return _ollama(providers.load_model, body.name)

    @app.post("/api/ollama/unload", response_model=OllamaState)
    def ollama_unload(body: ModelName):
        """Frees a model's memory, as `ollama stop` does."""
        return _ollama(providers.unload_model, body.name)

    @app.post("/api/ollama/remove", response_model=OllamaState)
    def ollama_remove(body: ModelName):
        """Deletes a downloaded model from disk."""
        return _ollama(providers.remove_model, body.name)

    @app.post("/api/ai/sign-in", response_model=SignIn)
    def sign_in(body: SignInStart | None = None):
        """Starts signing in to an AI (Claude unless named): its sign-in page opens in the browser."""
        try:
            return explain_module.start_login((body or SignInStart()).provider)
        except explain_module.ExplainError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/ai/sign-in", response_model=SignIn)
    def sign_in_status():
        return explain_module.login_status()

    @app.post("/api/ai/sign-in/code", response_model=SignIn)
    def sign_in_code(body: SignInCode):
        """Finishes signing in with the code the sign-in page shows."""
        if not body.code.strip() or len(body.code) > 2000:
            raise HTTPException(400, "paste the code the sign-in page shows")
        return explain_module.finish_login(body.code)

    @app.get("/api/repos/{repo_id}/summary", response_model=Summary)
    def project_summary(repo_id: int, head: str | None = Query(None, description="Defaults to the checked-out branch")):
        """The whole project on one page: what it is, what it's built with, its parts and history."""
        repo = _repo_path(repo_id)
        try:
            facts = summary_module.summary(repo, head or "HEAD")
        except git.GitError as error:
            raise HTTPException(400, str(error))
        return Summary(**facts, explanation=explain_module.cached("project", _project_subject(facts)))

    @app.post("/api/repos/{repo_id}/explain/project", response_model=ProjectExplanation)
    def explain_project(repo_id: int, request: ExplainProjectRequest):
        """Asks the AI to describe the project, from the facts on the summary and the notes
        on each file written for the knowledge transfer so far."""
        repo = _repo_path(repo_id)
        try:
            facts = summary_module.summary(repo, request.head or "HEAD")
            context = _project_context(facts)
            context["file_notes"] = {f["path"]: f["explanation"]["purpose"]
                                     for f in _kt_files(repo, request.head) if f["explanation"]}
            return explain_module.explain("project", _project_subject(facts), context, fresh=request.fresh)
        except git.GitError as error:
            raise HTTPException(400, str(error))
        except explain_module.ExplainError as error:
            raise _explain_failed(error)

    @app.get("/api/repos/{repo_id}/kt/guide", response_model=KtGuideSaved)
    def kt_guide(repo_id: int, head: str | None = Query(None, description="Defaults to the checked-out branch")):
        """The short KT, when it's been written for the project as it stands."""
        repo = _repo_path(repo_id)
        try:
            subject = _guide_subject(repo, head)
            found = explain_module.cached("guide", subject)
        except git.GitError as error:
            raise HTTPException(400, str(error))
        return KtGuideSaved(guide=_only_real(found, subject) if found else None)

    @app.post("/api/repos/{repo_id}/kt/guide", response_model=KtGuide)
    def write_kt_guide(repo_id: int, request: ExplainProjectRequest):
        """Writes the short KT with one request to the AI."""
        repo = _repo_path(repo_id)
        try:
            subject = _guide_subject(repo, request.head)
            guide = explain_module.explain("guide", subject, fresh=request.fresh)
        except git.GitError as error:
            raise HTTPException(400, str(error))
        except explain_module.ExplainError as error:
            raise _explain_failed(error)
        return _only_real(guide, subject)

    @app.get("/api/repos/{repo_id}/kt/image")
    def kt_image(repo_id: int, path: str = Query(...), head: str | None = Query(None)):
        """One of the project's own pictures, for the short KT."""
        repo = _repo_path(repo_id)
        try:
            found = summary_module.guide_image(repo, head or "HEAD", path)
        except git.GitError as error:
            raise HTTPException(400, str(error))
        if found is None:
            raise HTTPException(404, "No such picture.")
        kinds = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
        return Response(found, media_type=kinds.get(Path(path).suffix.lower(), "image/jpeg"),
                        headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/repos/{repo_id}/kt", response_model=KnowledgeTransfer)
    def knowledge_transfer(repo_id: int, head: str | None = Query(None, description="Defaults to the checked-out branch")):
        """Every file the project's knowledge transfer explains, with the notes written so far."""
        repo = _repo_path(repo_id)
        try:
            files = _kt_files(repo, head)
        except git.GitError as error:
            raise HTTPException(400, str(error))
        return KnowledgeTransfer(files=files, explained=sum(1 for f in files if f["explanation"]))

    @app.get("/api/repos/{repo_id}/kt/detail", response_model=KtDetail)
    def kt_detail(repo_id: int, head: str | None = Query(None, description="Defaults to the checked-out branch"),
                  worktree: bool | None = Query(None, description="The working folder; by default when it has changes")):
        """The detailed KT: the project's parts, files, reading path, data, routes and commands,
        read from the code, with the AI's words where they've been written."""
        repo = _repo_path(repo_id)
        try:
            return _kt_detail(repo, head, worktree)
        except git.GitError as error:
            raise HTTPException(400, str(error))

    @app.post("/api/repos/{repo_id}/kt/detail", response_model=KtDetail)
    def write_kt_detail(repo_id: int, request: KtDetailRequest):
        """Asks the AI for the detailed KT's words (from the file notes written so far), then
        returns the KT with them, checked against the code."""
        repo = _repo_path(repo_id)
        try:
            return _kt_detail(repo, request.head, request.worktree, write=True, fresh=request.fresh)
        except git.GitError as error:
            raise HTTPException(400, str(error))
        except explain_module.ExplainError as error:
            raise _explain_failed(error)

    @app.post("/api/repos/{repo_id}/explain/file", response_model=FileExplanation)
    def explain_file(repo_id: int, request: ExplainFileRequest):
        """Asks the AI what one file is for and what each function in it does, for the KT."""
        repo = _repo_path(repo_id)
        try:
            revision = projectmap.WORKTREE if request.worktree else request.head or "HEAD"
            file = summary_module.kt_file(repo, revision, request.path)
            if file is None:
                raise HTTPException(404, f"{request.path} isn't one of the project's files the KT explains")
            facts = summary_module.summary(repo, revision)
            context = {"text": file["text"], "outline": file["outline"], "part": file["part"],
                       "project": {"name": facts["name"], "description": facts["description"]}}
            return explain_module.explain("file", _file_subject(file), context, fresh=request.fresh)
        except git.GitError as error:
            raise HTTPException(400, str(error))
        except explain_module.ExplainError as error:
            raise _explain_failed(error)

    @app.post("/api/repos/{repo_id}/ask", response_model=AskAnswer)
    def ask(repo_id: int, request: AskRequest):
        """Answers a question about the project in plain words, from its description, the
        KT's notes on each file and the code of the few files the question is about. It
        explains only: it never writes code."""
        question = request.question.strip()
        if not question:
            raise HTTPException(400, "ask a question")
        if len(question) > explain_module.MAX_QUESTION:
            raise HTTPException(400, f"keep the question under {explain_module.MAX_QUESTION:,} characters")
        repo = _repo_path(repo_id)
        revision = request.head or "HEAD"
        try:
            facts = summary_module.summary(repo, revision)
            files = _kt_files(repo, request.head)
            history = [turn.model_dump() for turn in request.history]
            # A follow-up such as "and what calls it?" is about the files of the turn before.
            recent = " ".join(t["text"] for t in history[-2:] if t["role"] == "user")
            # The KT's facts: its picks of the major functions, read from the code, help find files.
            subject = _guide_subject(repo, request.head)
            code = {}
            for function in subject["functions"]:
                code.setdefault(function["path"], set()).add(function["name"])
            picked = (explain_module.pick_files(question, files, functions=code)
                      or explain_module.pick_files(recent, files, functions=code))
            if not picked and not history:
                # A question about the whole project: the files the rest use most show how it works.
                picked = list(code)[:2]
            notes = {f["path"]: f["explanation"] for f in files}
            reading = []
            for path in picked:
                file = summary_module.kt_file(repo, revision, path)
                if file:
                    reading.append({"path": path, "text": file["text"], "explanation": notes.get(path)})
            guide = explain_module.cached("guide", subject)
            # How to set it up and run it, word for word from its readme and setup files.
            setup = {"readme": subject.get("readme") or "", "files": subject.get("setup_files") or [],
                     "scripts": subject.get("scripts") or []}
            context = {"project": _project_subject(facts), "files": files, "reading": reading, "setup": setup,
                       "explanation": explain_module.cached("project", _project_subject(facts)),
                       "guide": _only_real(guide, subject) if guide else None}
            found = explain_module.answer(question, history, context)
        except git.GitError as error:
            raise HTTPException(400, str(error))
        except explain_module.ExplainError as error:
            raise _explain_failed(error)
        return AskAnswer(**found, read=[f["path"] for f in reading],
                         explained=sum(1 for f in files if f["explanation"]), total=len(files))

    @app.get("/api/github", response_model=GitHubStatus)
    def github_status():
        return github.status()

    @app.post("/api/github/connect", response_model=GitHubStatus)
    def github_connect(body: GitHubConnect | None = None):
        """Starts signing in to GitHub, through Looma's app or the gh tool: the answer has
        the code to enter on GitHub, or the account when gh was already signed in."""
        try:
            return github.start(body.method if body else "app")
        except github.GitHubError as error:
            raise HTTPException(502, str(error))

    @app.post("/api/github/token", response_model=GitHubStatus)
    def github_token(body: GitHubToken):
        """Connects with a token pasted from github.com/settings/tokens."""
        try:
            return github.use_token(body.token)
        except github.GitHubError as error:
            raise HTTPException(400, str(error))

    @app.post("/api/github/poll", response_model=GitHubStatus)
    def github_poll():
        """Whether the code has been entered on GitHub yet; connected once it has."""
        try:
            return github.poll()
        except github.GitHubError as error:
            raise HTTPException(400, str(error))

    @app.delete("/api/github", response_model=GitHubStatus)
    def github_disconnect():
        return github.disconnect()

    @app.get("/api/github/repos", response_model=list[GitHubRepo])
    def github_repos():
        """Your repositories that Looma's GitHub App may read, with the project each is cloned as."""
        conn = connect()
        rows = conn.execute("SELECT path FROM repos").fetchall()
        conn.close()
        tracked = {}
        for row in rows:
            name = github.remote_name(Path(row["path"])) if Path(row["path"]).is_dir() else None
            if name:
                tracked[name] = row["path"]
        try:
            return github.repos(tracked)
        except github.GitHubError as error:
            raise HTTPException(409 if error.signed_out else 502, str(error))

    @app.post("/api/github/clone", response_model=Repo, status_code=201)
    def github_clone(body: CloneRequest):
        """Clones one of your repositories into a folder and adds it as a project."""
        try:
            target = github.clone(body.full_name, _folder(body.folder))
        except github.GitHubError as error:
            raise HTTPException(409 if error.signed_out else 400, str(error))
        return _repo(_row_for(_install(install.init, target)))

    @app.get("/api/repos/{repo_id}/pulls", response_model=PullRequests)
    def pull_requests(repo_id: int):
        """The project's open pull requests on GitHub or CodeCommit, when it has a remote
        there and that account is connected."""
        path = _repo_path(repo_id)
        name = github.remote_name(path)
        if name is None:
            found = codecommit.remote_name(path)
            if found is not None:
                return _codecommit_pulls(*found)
        if name is None or not github.status()["connected"]:
            return PullRequests(repo=name, host="github" if name else None, connected=github.status()["connected"],
                                pulls=[], error=None)
        try:
            return PullRequests(repo=name, host="github", connected=True, pulls=github.pulls(name), error=None)
        except github.GitHubError as error:
            return PullRequests(repo=name, host="github", connected=github.status()["connected"], pulls=[], error=str(error))

    def _codecommit_pulls(region: str | None, name: str) -> PullRequests:
        connected = codecommit.region()
        if connected is None:
            return PullRequests(repo=name, host="codecommit", connected=False, pulls=[], error=None)
        if region and region != connected:
            return PullRequests(repo=name, host="codecommit", connected=True, pulls=[],
                                error=f"This repository is in {region}, but CodeCommit is connected in {connected}. "
                                      "Change the region in Settings → CodeCommit to see its pull requests.")
        try:
            return PullRequests(repo=name, host="codecommit", connected=True, pulls=codecommit.pulls(name), error=None)
        except codecommit.CodeCommitError as error:
            return PullRequests(repo=name, host="codecommit", connected=codecommit.region() is not None, pulls=[],
                                error=str(error))

    @app.get("/api/codecommit", response_model=CodeCommitStatus)
    def codecommit_status():
        return codecommit.status()

    @app.post("/api/codecommit/connect", response_model=CodeCommitStatus)
    def codecommit_connect(body: CodeCommitConnect):
        """Connects with one of this computer's AWS profiles, once AWS accepts it."""
        try:
            return codecommit.use_profile(body.profile, body.region)
        except codecommit.CodeCommitError as error:
            raise HTTPException(400, str(error))

    @app.delete("/api/codecommit", response_model=CodeCommitStatus)
    def codecommit_disconnect():
        return codecommit.disconnect()

    @app.get("/api/codecommit/repos", response_model=list[CodeCommitRepo])
    def codecommit_repos():
        """The repositories in the connected region, with the project each is cloned as."""
        conn = connect()
        rows = conn.execute("SELECT path FROM repos").fetchall()
        conn.close()
        tracked = {}
        connected = codecommit.region()
        for row in rows:
            found = codecommit.remote_name(Path(row["path"])) if Path(row["path"]).is_dir() else None
            if found and found[0] in (None, connected):
                tracked[found[1]] = row["path"]
        try:
            return codecommit.repos(tracked)
        except codecommit.CodeCommitError as error:
            raise HTTPException(409 if error.signed_out else 502, str(error))

    @app.post("/api/codecommit/clone", response_model=Repo, status_code=201)
    def codecommit_clone(body: CodeCommitClone):
        """Clones one of the region's repositories into a folder and adds it as a project."""
        try:
            target = codecommit.clone(body.name, _folder(body.folder))
        except codecommit.CodeCommitError as error:
            raise HTTPException(409 if error.signed_out else 400, str(error))
        return _repo(_row_for(_install(install.init, target)))

    @app.get("/api/digest", response_model=Digest)
    def daily_digest(day: str | None = None,
                     repo: int | None = Query(None, description="Only this project's changes; every project when left out")):
        """What Claude changed on one day (today unless `day` is YYYY-MM-DD), prompt by
        prompt, newest first, with the functions touched and rule-based risk flags. Only
        projects connected to Claude are recorded."""
        threading.Thread(target=retention.maybe_prune, daemon=True).start()
        try:
            return digest_module.digest(digest_module.parse_day(day), repo)
        except digest_module.DigestError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/digest/change", response_model=DigestChangeDetail)
    def digest_change(id: str):
        """One prompt's change with its lines, file by file, and each function (or whole
        file) it touched with only its own lines and any explanation written earlier."""
        try:
            found = digest_module.change(id)
        except digest_module.NotFound as error:
            raise HTTPException(404, str(error))
        for file in found["files"]:
            for unit in file["units"]:
                unit["explanation"] = explain_module.cached("function", digest_module.unit_subject(file["path"], unit))
        return found

    @app.post("/api/digest/explain", response_model=FunctionExplanation)
    def explain_unit(body: ExplainUnitRequest):
        """Asks the AI picked in Settings what one function (or file) of a prompt's change
        does now and what the prompt changed about it."""
        try:
            found = digest_module.change(body.change)
        except digest_module.NotFound as error:
            raise HTTPException(404, str(error))
        file = next((f for f in found["files"] if f["path"] == body.path), None)
        unit = next((u for u in file["units"] if u["name"] == body.name), None) if file else None
        if unit is None:
            raise HTTPException(404, f"{body.name} didn't change in {body.path} in that prompt")
        context = {"prompt": found["prompt"]} if found["prompt"] else {}
        try:
            return explain_module.explain("function", digest_module.unit_subject(body.path, unit), context)
        except explain_module.ExplainError as error:
            raise _explain_failed(error)

    @app.post("/api/digest/undo", response_model=Undone)
    def undo_change(body: ChangeRef):
        """Undoes one of Claude's changes in the working tree, all files or none, and
        commits nothing. 409 when a file changed since, so it no longer fits."""
        try:
            return Undone(change=body.change, undone_at=undo_module.undo(body.change))
        except undo_module.NotFound as error:
            raise HTTPException(404, str(error))
        except undo_module.UndoError as error:
            raise HTTPException(409, str(error))

    @app.post("/api/digest/put-back", response_model=Undone)
    def put_back_change(body: ChangeRef):
        """Puts an undone change back, if its files haven't changed since the undo."""
        try:
            undo_module.put_back(body.change)
        except undo_module.NotFound as error:
            raise HTTPException(404, str(error))
        except undo_module.UndoError as error:
            raise HTTPException(409, str(error))
        return Undone(change=body.change, undone_at=None)

    @app.get("/api/preferences", response_model=Preferences)
    def get_preferences():
        """The dashboard's settings: theme, uncommitted work from the start, shortcut keys."""
        return preferences_module.get()

    @app.put("/api/preferences", response_model=Preferences)
    def change_preferences(body: PreferenceChange):
        """Changes only the settings given; shortcuts merge one action at a time."""
        try:
            saved = preferences_module.update(body.model_dump(exclude_none=True))
        except preferences_module.PreferenceError as error:
            raise HTTPException(400, str(error))
        if body.keep_diffs_days is not None:
            # A shorter time takes effect now, not at the next hourly look.
            threading.Thread(target=retention.maybe_prune, kwargs={"force": True}, daemon=True).start()
        return saved

    @app.post("/api/preferences/reset-shortcuts", response_model=Preferences)
    def reset_shortcuts():
        return preferences_module.reset_shortcuts()

    @app.post("/api/preferences/reset-home", response_model=Preferences)
    def reset_home():
        """Home's widgets back to the default order and selection."""
        return preferences_module.reset_home()

    if (WEB / "index.html").is_file():
        # Last, so every /api route above comes first.
        app.mount("/", StaticFiles(directory=WEB, html=True), name="ui")

    return app


def _folder(typed: str) -> Path:
    """A folder as people paste it: in quotes (Windows' "Copy as path"), with ~ or an
    environment variable such as %USERPROFILE% or $HOME, with either kind of slash, or
    relative to the home folder."""
    text = typed.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    path = Path(os.path.expandvars(text)).expanduser()
    return path if path.is_absolute() else Path.home() / path


def _set_login(response: Response, token: str) -> None:
    # HttpOnly keeps it from page scripts; Strict keeps other sites from sending it.
    response.set_cookie(auth.COOKIE, token, max_age=auth.LOGIN_DAYS * 86400, path="/", httponly=True,
                        samesite="strict")


def _explain_failed(error: "explain_module.ExplainError") -> HTTPException:
    # 424 tells the page to offer signing in to Claude (401 is for Looma's own sign-in);
    # anything else is Claude Code failing.
    return HTTPException(424 if error.signed_out else 502, str(error))


def _function_changes(repo: Path, path: str, old_path: str | None, base: str | None, head: str | None,
                      uncommitted: bool) -> list[dict]:
    head = head or git.branch(repo) or "HEAD"
    base = base or compare_module.default_base(repo)
    if base is None:
        raise HTTPException(400, "no main, master, develop or dev branch to compare against; pick one")
    try:
        base_ref = compare_module.freshest(repo, base)
        merge_base = compare_module.fork_point(repo, base_ref, head)
    except git.GitError as error:
        raise HTTPException(400, str(error))
    return functions_module.function_changes(repo, merge_base, None if uncommitted else head, path, old_path)


def _function_subject(path: str, item: dict) -> dict:
    # What the explanation depends on; the same code always gets the same one.
    subject = {"path": path, "name": item["name"], "kind": item["kind"], "change": item["change"],
               "before": (item["before"] or {}).get("code"), "after": (item["after"] or {}).get("code")}
    if item.get("diff"):
        # A whole file's code is cut short; its diff says what changed further down.
        subject["diff"] = item["diff"]
    return subject


def _file_subject(file: dict) -> dict:
    # The file's content, by its git blob: an edit to it gets a new note.
    return {"path": file["path"], "sha": file["sha"]}


def _kt_detail(repo: Path, head: str | None, worktree: bool | None, write: bool = False, fresh: bool = False) -> dict:
    changes = ktdoc.has_changes(repo)
    doc = ktdoc.document(repo, head or "HEAD", worktree=changes if worktree is None else worktree)
    for f in doc["files"]:
        f["explanation"] = explain_module.cached("file", _file_subject(f))
    subject = ktdoc.subject(doc)
    if write:
        facts = summary_module.summary(repo, doc["revision"])
        context = {**ktdoc.context(doc), "project": {"name": facts["name"], "description": facts["description"]}}
        explain_module.explain("ktdetail", subject, context, fresh=fresh)
    ai = ktdoc.verify(explain_module.cached("ktdetail", subject), doc) or {}
    names = {p["id"]: p for p in ai.get("parts", [])}
    why = {r["path"]: r for r in ai.get("reading", [])}
    return {
        **doc, "worktree": doc["revision"] == projectmap.WORKTREE, "has_changes": changes, "written": bool(ai),
        "summary": ai.get("summary", ""), "audience": ai.get("audience", ""), "problem": ai.get("problem", ""),
        "parts": [{**p, **({"name": names[p["id"]]["name"], "what": names[p["id"]]["what"]} if p["id"] in names else {})}
                  for p in doc["parts"]],
        "reading": [{**r, "why": why[r["path"]]["why"] if r["path"] in why else r["why"],
                     "after": why[r["path"]]["after"] if r["path"] in why else None} for r in doc["reading"]],
        "flows": ai.get("flows", []), "glossary": ai.get("glossary", []),
        **doc["data"], "cautions": ktdoc.cautions(doc["files"]),
        "explained": sum(1 for f in doc["files"] if f["explanation"]),
    }


def _kt_files(repo: Path, head: str | None) -> list[dict]:
    return [{**{k: f[k] for k in ("path", "part", "language", "bytes")},
             "explanation": explain_module.cached("file", _file_subject(f))}
            for f in summary_module.kt_files(repo, head or "HEAD")]


def _project_subject(facts: dict) -> dict:
    """What the project description depends on. Counts and history change with every
    commit, so they go in `_project_context` instead, or the description would be lost
    each time."""
    names = {area["id"]: area["name"] for area in facts["parts"]}
    return {
        "name": facts["name"],
        "description": facts["description"],
        "readme": facts["readme"],
        "tech": [f"{t['name']}: {t['what']}" for t in facts["tech"]],
        "run": [f"{r['name']}: {r['command']} ({r['where']})" for r in facts["run"]],
        "entry_points": facts["entry_points"],
        "parts": [{"name": a["name"], "folder": a["path"] or "(top level)", "guessed_role": a["role"],
                   "languages": [l["name"] for l in a["languages"]]} for a in facts["parts"]],
        "uses": [f"{names[e['source']]} uses {names[e['target']]}" for e in facts["links"]],
    }


def _only_real(guide: dict, subject: dict) -> dict:
    """Only the pictures and functions the project really has, whatever the AI named."""
    offered = {image["path"] for image in subject["images"]}
    code = {(f["path"], f["name"]): f["signature"] for f in subject["functions"]}
    return {
        **guide,
        "images": [image for image in guide["images"] if image["path"] in offered],
        "functions": [{**f, "signature": _whole_signature(code[(f["path"], f["name"])])}
                      for f in guide["functions"] if (f["path"], f["name"]) in code and f["what"]],
    }


def _whole_signature(line: str) -> str | None:
    """The line a function starts on, unless its parameters carry on to the next lines."""
    line = line.rstrip(" :{")
    return line if line.count("(") == line.count(")") else None


def _guide_subject(repo: Path, head: str | None) -> dict:
    """What the short KT is written from: when any of it changes, it's written again."""
    facts = summary_module.summary(repo, head or "HEAD")
    guide = summary_module.guide_facts(repo, head or "HEAD")
    return {
        "name": facts["name"],
        "description": facts["description"],
        "built_with": [t["name"] for t in facts["tech"]],
        "package_commands": [f"{r['name']}: {r['command']} ({r['where']})" for r in facts["run"]],
        "entry_points": facts["entry_points"],
        "parts": [a["path"] or "(top level)" for a in facts["parts"]][:12],
        **guide,
    }


def _project_context(facts: dict) -> dict:
    history = facts["history"]
    return {
        "files": facts["files"],
        "languages": [f"{l['name']} ({l['files']} files)" for l in facts["languages"]],
        "files_per_part": {a["name"]: a["files"] for a in facts["parts"]},
        "history": {"commits": history["commits"], "people": len(history["people"]),
                    "first_commit": history["first"], "last_commit": history["last"],
                    "commits_per_week_last_12": history["weeks"]},
        "recent_commit_messages": facts.get("recent_commits", []),
        # Only when there's no readme or package description: the opening lines of the code.
        "code_samples": facts.get("samples", []),
    }


def _repo(row) -> Repo:
    path = Path(row["path"])
    exists = path.is_dir()
    is_git = exists and git.toplevel(path) is not None
    return Repo(id=row["id"], name=row["name"], path=row["path"], exists=exists, git=is_git,
                branch=git.branch(path) if is_git else None, claude=bool(row["claude"]), ai=_ai(row))


def _ai(row) -> str | None:
    """The AI tool the project records, if any."""
    return (row["ai"] or "claude") if row["claude"] else None


def _row_for(root: Path):
    conn = connect()
    row = conn.execute("SELECT id, name, path, claude, ai FROM repos WHERE path = ?", (str(root),)).fetchone()
    conn.close()
    return row


def _install(action, path: Path | None):
    try:
        return action(path)
    except install.InitError as error:
        raise HTTPException(400, str(error))
    except json.JSONDecodeError:  # kept for callers outside install's own checks
        raise HTTPException(400, "A settings file isn't valid JSON, so Looma can't add its hooks")


def _repo_path(repo_id: int) -> Path:
    conn = connect()
    row = conn.execute("SELECT path FROM repos WHERE id = ?", (repo_id,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(404, "repo not tracked; run looma init in it")
    path = Path(row["path"])
    if not path.is_dir():
        raise HTTPException(410, f"repo is no longer at {path}")
    return path


def _git_path(repo_id: int, feature: str) -> Path:
    """The project's folder, for what only works in a git repository."""
    path = _repo_path(repo_id)
    if git.toplevel(path) is None:
        raise HTTPException(409, f"{feature} needs git, and {path.name} isn't a git repository. "
                                 "Run git init in it to turn this on.")
    return path
