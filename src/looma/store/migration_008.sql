-- Version 8: Claude changes undone from the daily digest. A row means the change is undone
-- in the working tree; putting it back deletes the row.

CREATE TABLE undone (
    change_key  TEXT PRIMARY KEY,          -- the digest's id: turn-<id>-<repo> or commit-<sha>-<repo>
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    turn_id     INTEGER REFERENCES turns(id),
    commit_sha  TEXT,
    undone_at   TEXT NOT NULL
);
