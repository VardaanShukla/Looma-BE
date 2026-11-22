"""How to use Looma itself, for Ask: every screen, where it is in the menu and what its
controls do, the settings, the keys and the `looma` commands. Ask is given this with every
question, so "how do I write the KT?" or "where do I see uncommitted work?" is answered as
well as a question about the project. Keep it in step with the page when a screen changes."""

GUIDE = """\
Looma runs on your own computer (`looma serve`, then http://127.0.0.1:8765) and shows what changed \
in your code in plain words. The menu on the left: Home, AI changes, Code (Changes, Commits, This \
branch, Branch flow), Understand (Summary, Project map, KT, Ask), Projects, then Settings and Help \
at the bottom. The project on show is picked at the top of the menu; + adds one. The menu folds to \
icons with the button beside the logo.

Screens:
- Home: the dashboard. A greeting with what needs you, the project and branch beside a clock, AI \
today (the AI picked in Settings, today's prompts as bars; Know more opens AI changes), Total \
changes (a gauge of lines changed on the branch with files, functions and commits), Latest commit, \
AI prompts today, Changed files, Branches (pick one to show it everywhere) and Today's activity. \
Edit Home (top right) moves widgets by their bar, resizes them by the corner (arrow keys, Shift to \
resize), adds or removes widgets and sets each one's title, style and options; Reset brings back \
the starting layout.
- AI changes: every request you gave your AI coding tool (Claude Code, Codex, Antigravity) on a \
day, with the files and functions it changed and whether that's committed. Risky ones (sign-in, \
payments, database, tests, keys) come first under Needs a look. A change can be opened, explained \
or undone from there.
- Changes (Code): a page of its own, without the menu. Files are listed on the left with a filter \
and their added and removed lines; the right shows the picked file By function or as Lines, Split \
(before and after side by side) or Unified. Explain with <AI> explains a function's change; What is \
this file for? asks about the file. j and k move between files; the back arrow at the top leaves.
- Commits (Code): the branch's commits newest first, and open pull requests from GitHub or \
CodeCommit.
- This branch (Code): the size of the change on a gauge, how much was made with AI, latest commits.
- Branch flow (Code): branches as lines leaving and joining the base; point at a dot for its commit.
- Summary (Understand): what the project is, its languages, how its parts connect, what it's built \
with, who works on it. Describe it with <AI> writes a plain-English overview.
- Project map (Understand): each box is a part of the project; an arrow means one part uses \
another's code; an orange dot marks parts changed on this branch. Click a box for its files, what \
it uses and what uses it.
- KT (Understand): a knowledge-transfer guide for a new developer. Pick Brief KT or Detailed KT at \
the top. Brief KT is one page (what you need, setup, run commands, how to use it, major functions): \
press Write the KT with <AI>, then Download PDF; Write again rewrites it. Detailed KT explains every \
file and how they connect: press Write it with <AI> (it notes each file, then the whole project; Stop \
halts it), then Download PDF or Download interactive page; Write it again rewrites all of it. Both \
keep going if you leave the page, and use the AI picked in Settings > AI.
- Ask (Understand): this chat. Questions about the project or about Looma; New chat clears it.
- Projects: add a git folder, connect an AI coding tool to record the request behind each change, \
or remove a project.
- Settings: General (account, password, sign out), Appearance (dark or light, Show uncommitted \
work, which includes edits not yet committed in Changes, This branch and the map), AI (which AI \
writes explanations: Claude, ChatGPT Codex, a local model with Ollama, Antigravity or your own \
server, and its model), Recording (whether AI tools' hooks are working, Repair), GitHub and \
CodeCommit (connect to clone repositories and see pull requests), Shortcuts (click a key to change \
it).
- Help: setup steps for the project with what's left to do, a guide to each screen, and the tour.

Keys (changeable in Settings > Shortcuts): 1 Home, 2 AI changes, 3 Summary, 4 KT, 5 Ask, \
6 Changes, 7 Commits, 8 Projects, comma Settings, t dark or light, u uncommitted work on or off \
for a while, s sync with GitHub, j and k next and previous file.

Commands (in a terminal):
- `looma serve` starts Looma (add `--port 9000` for another port).
- `looma init <folder>` tracks a git project, or remembers a folder of projects to pick from.
- `looma connect claude <folder>` (or codex, or antigravity) records that AI tool's changes there; \
`looma disconnect claude <folder>` stops it.
- `looma password` sets the username and password, and is the way back in if it's forgotten.
- `looma turns` lists recent AI requests; `looma instructions` prints the text to give a Claude \
app chat so its changes are recorded.
- `looma remove-hooks` takes Looma's hooks out of every project and AI tool before uninstalling.
- `bash scripts/looma.sh` (Linux, macOS) or `scripts\\looma.bat` (Windows), from the Looma \
backend folder: install, repair, uninstall or start Looma; it reinstalls by itself when the code \
changed.
"""
