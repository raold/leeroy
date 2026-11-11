CREATE TABLE learn_arm (
    id TEXT PRIMARY KEY, family TEXT NOT NULL, params TEXT NOT NULL CHECK(json_valid(params)),
    version INTEGER NOT NULL CHECK(version>0),
    status TEXT NOT NULL CHECK(status IN ('control','active','proposed','retired')),
    origin TEXT NOT NULL CHECK(origin IN ('builtin','coach')), created REAL NOT NULL
);
CREATE UNIQUE INDEX learn_arm_control ON learn_arm(family) WHERE status='control';
ALTER TABLE talk_recipe ADD COLUMN bandit_context TEXT;
CREATE TABLE bandit_credit (
    line_id INTEGER NOT NULL REFERENCES chat_line(id) ON DELETE CASCADE,
    arm_id TEXT NOT NULL REFERENCES learn_arm(id), guid INTEGER NOT NULL, context TEXT NOT NULL,
    revision INTEGER NOT NULL, ts REAL NOT NULL, success REAL NOT NULL, failure REAL NOT NULL,
    bad INTEGER NOT NULL CHECK(bad IN (0,1)), PRIMARY KEY(line_id,arm_id)
);
CREATE INDEX bandit_credit_context ON bandit_credit(guid,context,arm_id);
CREATE TABLE bandit_turn (
    guid INTEGER PRIMARY KEY, turns INTEGER NOT NULL DEFAULT 0,
    exploratory INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE bandit_share (
    guid INTEGER NOT NULL, context TEXT NOT NULL, arm_id TEXT NOT NULL REFERENCES learn_arm(id),
    turns INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guid,context,arm_id)
);
CREATE TABLE bandit_rejection (
    attempt_id TEXT NOT NULL, arm_id TEXT NOT NULL REFERENCES learn_arm(id),
    guid INTEGER NOT NULL, context TEXT NOT NULL, reason TEXT NOT NULL CHECK(reason IN ('guard','content')),
    PRIMARY KEY(attempt_id,arm_id)
);
