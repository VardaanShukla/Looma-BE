-- Version 1: capture tables. Analyzer and summarizer tables arrive in later migrations.

CREATE TABLE repos (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,      -- git top-level directory
    name        TEXT NOT NULL,
    added_at    TEXT NOT NULL
);

CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,          -- Claude Code session_id
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    tool        TEXT NOT NULL DEFAULT 'claude-code',
    started_at  TEXT NOT NULL
);

-- One prompt and the change it caused.
CREATE TABLE turns (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    branch      TEXT,
    prompt      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT                       -- NULL while Claude is still working
);

-- Each file's content before the AI first touched it in a turn, so the
-- turn's diff excludes edits made earlier or by hand.
CREATE TABLE turn_files (
    turn_id         INTEGER NOT NULL REFERENCES turns(id),
    file_path       TEXT NOT NULL,         -- relative to the repo root
    before_content  TEXT,                  -- NULL when the file did not exist
    PRIMARY KEY (turn_id, file_path)
);

CREATE TABLE commits (
    sha         TEXT PRIMARY KEY,
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    branch      TEXT,
    message     TEXT NOT NULL,
    authored_at TEXT NOT NULL
);

-- One file's diff. source is 'ai' (from a turn) or 'human' (only seen in a commit).
CREATE TABLE changes (
    id          INTEGER PRIMARY KEY,
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    turn_id     INTEGER REFERENCES turns(id),
    commit_sha  TEXT REFERENCES commits(sha),
    file_path   TEXT NOT NULL,
    diff        TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('ai', 'human')),
    created_at  TEXT NOT NULL
);

CREATE INDEX turns_by_session ON turns(session_id, started_at);
CREATE INDEX changes_by_repo ON changes(repo_id, created_at);
CREATE INDEX changes_uncommitted ON changes(repo_id, file_path) WHERE commit_sha IS NULL;
