-- Version 2: Claude Code hooks move to user settings, so a session can start in any
-- folder (for example the parent of several repos). Sessions no longer belong to one
-- repo; each snapshotted file records the repo it lives in.

CREATE TABLE sessions_new (
    id          TEXT PRIMARY KEY,
    repo_id     INTEGER REFERENCES repos(id),   -- repo of the starting folder, if any
    tool        TEXT NOT NULL DEFAULT 'claude-code',
    started_at  TEXT NOT NULL
);
INSERT INTO sessions_new SELECT id, repo_id, tool, started_at FROM sessions;
DROP TABLE sessions;
ALTER TABLE sessions_new RENAME TO sessions;

CREATE TABLE turn_files_new (
    turn_id         INTEGER NOT NULL REFERENCES turns(id),
    repo_id         INTEGER NOT NULL REFERENCES repos(id),
    file_path       TEXT NOT NULL,         -- relative to the repo root
    before_content  TEXT,                  -- NULL when the file did not exist
    PRIMARY KEY (turn_id, repo_id, file_path)
);
INSERT INTO turn_files_new
    SELECT f.turn_id, s.repo_id, f.file_path, f.before_content
    FROM turn_files f JOIN turns t ON t.id = f.turn_id JOIN sessions s ON s.id = t.session_id;
DROP TABLE turn_files;
ALTER TABLE turn_files_new RENAME TO turn_files;
