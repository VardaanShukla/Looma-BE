-- Version 4: the developer's own git history is the core and needs no hooks. Claude
-- tracking is opt-in per repo (`devdash connect claude`). Repos added before this
-- version had the hooks installed, so they stay connected.

ALTER TABLE repos ADD COLUMN claude INTEGER NOT NULL DEFAULT 0;
UPDATE repos SET claude = 1;
