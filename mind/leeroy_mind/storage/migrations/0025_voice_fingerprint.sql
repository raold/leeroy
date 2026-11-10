-- Only local profile numbers and references; no text is emitted to logs or prompts.
CREATE TABLE phrase (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('opener','closer','emoji','emoticon','laughter')),
    text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 80),
    UNIQUE(guid, kind, text)
);
CREATE TABLE voice_fingerprint (
    guid INTEGER PRIMARY KEY,
    profile TEXT NOT NULL CHECK(json_valid(profile)),
    version INTEGER NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX chat_line_fingerprint ON chat_line(from_guid, ts DESC, id DESC)
    WHERE origin='mind';
