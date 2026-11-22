-- Version 11: a prompt's changes include files changed by any tool, not only Claude Code's
-- Edit and Write. When a prompt starts, each connected project's commit and its files that
-- already differ from it are noted; when it stops, every file that differs since counts.

-- The commit each project was on when the prompt started, for the projects it watches.
CREATE TABLE turn_repos (
    turn_id     INTEGER NOT NULL REFERENCES turns(id),
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    head        TEXT,                      -- NULL before the project's first commit
    PRIMARY KEY (turn_id, repo_id)
);

-- 1 when an Edit or Write hook saw Claude change the file; 0 when it was only noted
-- because it already differed from the commit as the prompt started; -1 when it was
-- noted but too large or not text, so it is left out rather than guessed at.
ALTER TABLE turn_files ADD COLUMN seen INTEGER NOT NULL DEFAULT 1;

-- How a prompt's change was found: 'edit' by an Edit or Write hook, 'watch' because the
-- file changed while the prompt ran with no such hook seeing it (a shell command, or an
-- edit by hand at the same time). NULL for changes recorded before this version.
ALTER TABLE changes ADD COLUMN via TEXT;
