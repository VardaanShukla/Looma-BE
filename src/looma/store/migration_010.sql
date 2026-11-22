-- Version 10: commits found by looking through the branches, rather than recorded by the
-- post-commit hook as they were made. A Claude co-authored one with no recorded prompt
-- came from somewhere the hooks don't reach, such as a cloud session that pushed it.

ALTER TABLE commits ADD COLUMN found_by TEXT;  -- NULL: the post-commit hook; 'scan': a branch scan
