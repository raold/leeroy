-- Numeric credit and generation identifiers only; enabling learning owns all data writes.
CREATE TABLE learn_weights (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL CHECK(created >= 0),
    weights TEXT NOT NULL CHECK(json_valid(weights) AND json_type(weights) = 'object')
);
CREATE TABLE talk_recipe (
    line_id INTEGER PRIMARY KEY REFERENCES chat_line(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    template_version INTEGER NOT NULL CHECK(template_version > 0),
    dials_hash TEXT NOT NULL CHECK(length(dials_hash) = 64),
    model_id TEXT NOT NULL,
    guard_verdict TEXT NOT NULL CHECK(guard_verdict IN
        ('not_run','send','rewrite','react','drop','failed_open')),
    arm_ids TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(arm_ids) AND json_type(arm_ids) = 'array'),
    lesson_ids TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(lesson_ids) AND json_type(lesson_ids) = 'array')
);
CREATE TABLE talk_reward (
    line_id INTEGER PRIMARY KEY REFERENCES chat_line(id) ON DELETE CASCADE,
    guid INTEGER NOT NULL CHECK(guid > 0),
    value REAL NOT NULL CHECK(value BETWEEN -1 AND 1),
    parts TEXT NOT NULL CHECK(json_valid(parts) AND json_type(parts) = 'object'),
    weights_version INTEGER NOT NULL REFERENCES learn_weights(version),
    scored_at REAL NOT NULL CHECK(scored_at >= 0)
);
CREATE INDEX talk_reward_guid ON talk_reward(guid, line_id);
CREATE TRIGGER talk_reward_numeric_insert BEFORE INSERT ON talk_reward
WHEN EXISTS (SELECT 1 FROM json_each(NEW.parts) WHERE type NOT IN ('integer','real'))
BEGIN SELECT RAISE(ABORT, 'reward_parts_not_numeric'); END;
CREATE TRIGGER talk_reward_numeric_update BEFORE UPDATE OF parts ON talk_reward
WHEN EXISTS (SELECT 1 FROM json_each(NEW.parts) WHERE type NOT IN ('integer','real'))
BEGIN SELECT RAISE(ABORT, 'reward_parts_not_numeric'); END;
