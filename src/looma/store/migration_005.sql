-- Version 5: plain-English explanations written by Claude, kept so each one is asked
-- for once. The key is a hash of what was explained, so changed code gets a new one.

CREATE TABLE explanations (
    key         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,             -- 'function' or 'project'
    result      TEXT NOT NULL,             -- JSON
    created_at  TEXT NOT NULL
);
