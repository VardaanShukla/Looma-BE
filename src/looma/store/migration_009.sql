-- Version 9: each project's first commit, which stays the same wherever the folder is.
-- It lets a project that was moved, renamed or cloned again keep its history.

ALTER TABLE repos ADD COLUMN root_commit TEXT;
