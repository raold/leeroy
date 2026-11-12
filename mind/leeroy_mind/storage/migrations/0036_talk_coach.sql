CREATE TABLE talk_lesson (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid INTEGER NOT NULL CHECK(guid>0),
    scope TEXT NOT NULL CHECK(scope IN ('whisper','guild','group','any')),
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','proposed')),
    reinforced INTEGER NOT NULL DEFAULT 0 CHECK(reinforced>=0),
    expires REAL NOT NULL,
    origin_night TEXT NOT NULL,
    UNIQUE(guid,scope,text)
);
CREATE INDEX talk_lesson_member ON talk_lesson(guid,expires);
