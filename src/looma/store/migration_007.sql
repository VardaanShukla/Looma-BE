-- Version 7: signing in to the dashboard. The password's hash lives in settings; each
-- signed-in browser holds a random token whose hash is kept here until it expires.

CREATE TABLE logins (
    token_hash  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
