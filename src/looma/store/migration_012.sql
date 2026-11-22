-- Version 12: Codex and Antigravity can be recorded too. `claude` stays the switch for
-- "record this project's AI changes"; `ai` names the tool picked for it.

ALTER TABLE repos ADD COLUMN ai TEXT;
UPDATE repos SET ai = 'claude' WHERE claude = 1;
