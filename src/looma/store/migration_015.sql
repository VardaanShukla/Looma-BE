-- Version 15: what the Git screen did in each project, so its last step can be undone.

CREATE TABLE git_actions (
    id          INTEGER PRIMARY KEY,
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    action      TEXT NOT NULL,             -- commit, amend, update, pull, branch-delete, ...
    branch      TEXT,                      -- the branch checked out when it ran
    before_sha  TEXT,                      -- where that branch (or the deleted one) pointed before
    after_sha   TEXT,                      -- and after
    detail      TEXT NOT NULL,             -- JSON: what undoing it needs (names, a stash's message)
    summary     TEXT NOT NULL,             -- what it did, in plain words
    at          TEXT NOT NULL,
    undone_at   TEXT
);

CREATE INDEX git_actions_repo ON git_actions (repo_id, id);
