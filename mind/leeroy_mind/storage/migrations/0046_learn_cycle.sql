-- The weekly self-improvement cycle: learning-state versions and its promotions.
-- A version holds ids and numbers only (no lesson text, no chat text).
CREATE TABLE learn_version (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL CHECK(created >= 0),
    reason TEXT NOT NULL CHECK(reason IN ('cycle','rollback')),
    week TEXT,
    restored_from INTEGER,
    state TEXT NOT NULL CHECK(json_valid(state) AND json_type(state) = 'object')
);
-- An arm is promoted per bandit context; a lesson with context ''.
CREATE TABLE learn_promotion (
    kind TEXT NOT NULL CHECK(kind IN ('arm','lesson')),
    ident TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('promoted','rolled_back','retired')),
    week TEXT NOT NULL,
    promoted REAL NOT NULL,
    changed REAL NOT NULL,
    PRIMARY KEY(kind, ident, context)
);
