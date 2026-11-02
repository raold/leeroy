-- mind.db: voice comms (phase 2). Who sat in which voice room, which lines were spoken and
-- whether he heard them, and his transcribed lines' recognition details. Times: unix seconds.
CREATE TABLE voice_presence (
    id         INTEGER PRIMARY KEY,
    guid       INTEGER NOT NULL,            -- a cast member or the human
    room       TEXT NOT NULL,               -- a voice.rooms.ROOMS id
    since      REAL NOT NULL,
    until      REAL,                        -- NULL while there
    by         INTEGER NOT NULL DEFAULT 0,  -- who moved them; 0: the rules or themselves
    session_id INTEGER REFERENCES session(id)
);
CREATE INDEX voice_presence_open ON voice_presence(guid) WHERE until IS NULL;
CREATE TABLE voice_line (
    speak_id TEXT PRIMARY KEY,              -- "<run_id>.<turn_id>"
    line_id  INTEGER NOT NULL,              -- chat_line.id
    room     TEXT NOT NULL,                 -- the room, or "whisper"
    state    TEXT NOT NULL CHECK (state IN ('unheard','sent','started','done','dropped','cut')),
    reason   TEXT,
    ts       REAL NOT NULL
);
CREATE INDEX voice_line_line ON voice_line(line_id);
CREATE TABLE voice_heard (
    line_id    INTEGER PRIMARY KEY,         -- chat_line.id of his transcribed line
    raw        TEXT NOT NULL,
    confidence REAL NOT NULL,
    speech_ms  INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL
);
