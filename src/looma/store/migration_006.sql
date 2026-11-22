-- Version 6: a to-do list, each item for everywhere, one project, or one branch of it;
-- and plain settings, such as the private address of the user's calendar.

CREATE TABLE todos (
    id          INTEGER PRIMARY KEY,
    repo_id     INTEGER REFERENCES repos(id) ON DELETE CASCADE,  -- NULL: not tied to a project
    branch      TEXT,                                            -- NULL: the whole project
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    done_at     TEXT                                             -- NULL while open
);

CREATE TABLE settings (
    name   TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
