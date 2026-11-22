-- Version 13: old diffs can be cleared to save space. A cleared change keeps its prompt,
-- files and line counts; only its lines (the diff text) are gone.

ALTER TABLE changes ADD COLUMN additions INTEGER;
ALTER TABLE changes ADD COLUMN deletions INTEGER;
ALTER TABLE changes ADD COLUMN pruned_at TEXT;
CREATE INDEX changes_by_time ON changes(created_at) WHERE pruned_at IS NULL;
