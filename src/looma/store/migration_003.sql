-- Version 3: commits with an AI co-author trailer count as AI changes, even when no
-- Claude Code turn captured the edit. Reclassifies commits recorded before this rule.

UPDATE changes SET source = 'ai'
WHERE turn_id IS NULL AND source = 'human' AND commit_sha IN (
    SELECT sha FROM commits
    WHERE message LIKE '%Co-Authored-By:%Claude%'
       OR message LIKE '%Co-Authored-By:%noreply@anthropic.com%'
       OR message LIKE '%Co-Authored-By:%Copilot%'
       OR message LIKE '%Co-Authored-By:%Cursor%'
);
