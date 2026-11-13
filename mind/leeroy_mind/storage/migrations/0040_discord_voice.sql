CREATE TABLE discord_voice (
    message INTEGER PRIMARY KEY REFERENCES discord_message(id),
    origin TEXT CHECK(origin IN ('mind','human')),
    path TEXT,
    ms INTEGER CHECK(ms BETWEEN 200 AND 65000),
    bytes INTEGER CHECK(bytes BETWEEN 1 AND 2097152),
    transcript_ok INTEGER NOT NULL CHECK(transcript_ok IN (0,1))
);
