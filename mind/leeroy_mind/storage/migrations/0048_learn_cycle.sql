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
-- Owner and guard decisions on arms and lessons, in order: a rollback applies the later ones again.
CREATE TABLE learn_decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('arm','lesson')),
    ident TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('accept','reject','retire','guard_retire','approve','veto','expire'))
);
-- What a rollback reverted (the diversity floor's or the owner's) waits for new evidence.
CREATE TABLE learn_hold (
    kind TEXT NOT NULL CHECK(kind IN ('arm','lesson')),
    ident TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL CHECK(reason IN ('diversity','rollback')),
    since REAL NOT NULL,
    PRIMARY KEY(kind, ident, context)
);
