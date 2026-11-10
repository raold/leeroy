CREATE TABLE photo_extra (
    guid INTEGER PRIMARY KEY,
    kind_digest TEXT NOT NULL,
    light INTEGER NOT NULL CHECK(light BETWEEN 0 AND 5),
    updated REAL NOT NULL
);
CREATE TABLE photo_style_history (
    id INTEGER PRIMARY KEY,
    guid INTEGER NOT NULL,
    created REAL NOT NULL,
    outfit TEXT NOT NULL,
    setting TEXT NOT NULL
);
CREATE INDEX photo_style_history_created ON photo_style_history(created);
