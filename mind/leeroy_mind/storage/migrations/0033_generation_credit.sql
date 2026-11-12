-- Each delivered fragment belongs to the first delivered line of its generation.
CREATE TABLE talk_recipe_line (
    line_id INTEGER PRIMARY KEY REFERENCES chat_line(id) ON DELETE CASCADE,
    recipe_line_id INTEGER NOT NULL REFERENCES talk_recipe(line_id) ON DELETE CASCADE
);
CREATE INDEX talk_recipe_line_generation ON talk_recipe_line(recipe_line_id, line_id);
INSERT INTO talk_recipe_line SELECT line_id,line_id FROM talk_recipe;

-- Wall time of completed reward units, kept apart by screening path.
CREATE TABLE reward_timing (
    night TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('lexical','embedding')),
    units INTEGER NOT NULL CHECK(units > 0),
    seconds REAL NOT NULL CHECK(seconds >= 0),
    PRIMARY KEY(night, mode)
);
