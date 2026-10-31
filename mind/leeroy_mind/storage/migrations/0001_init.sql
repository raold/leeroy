-- mind.db schema v1.
-- TIME UNITS: every timestamp the mind stores or compares is mind-clock unix SECONDS, stamped
-- when the mind received the thing. The server's millisecond `ts` is kept only inside
-- game_event.payload (JSON). Columns written by the store itself (chat_line.ts, game_event.ts)
-- may be fractional seconds; every other time column holds whatever seconds the caller passed.
-- session_id is NULL when the row arrived outside a session (human offline, backlog, BG deaths).

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE session (
    id      INTEGER PRIMARY KEY,
    started REAL NOT NULL,          -- unix seconds
    ended   REAL                    -- unix seconds, NULL while open
);

CREATE TABLE cast_member (
    guid     INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    race     INTEGER NOT NULL,
    class    INTEGER NOT NULL,
    gender   INTEGER NOT NULL,
    level    INTEGER NOT NULL,
    rank     INTEGER NOT NULL,
    online   INTEGER NOT NULL DEFAULT 0,
    map      INTEGER NOT NULL DEFAULT 0,
    zone     INTEGER NOT NULL DEFAULT 0,
    spec_tab INTEGER NOT NULL DEFAULT 0,
    updated  REAL NOT NULL          -- unix seconds
);

CREATE TABLE chat_thread (
    id     INTEGER PRIMARY KEY,
    key    TEXT NOT NULL,
    opened REAL NOT NULL,           -- unix seconds
    closed REAL,                    -- unix seconds
    status TEXT NOT NULL DEFAULT 'open'
           CHECK (status IN ('open','pending','extracted','skipped','failed'))
);
CREATE INDEX chat_thread_status ON chat_thread(status);

CREATE TABLE chat_line (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,       -- unix seconds, mind clock at receipt
    channel    TEXT NOT NULL,
    from_guid  INTEGER NOT NULL,
    from_name  TEXT NOT NULL,
    to_guid    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    map        INTEGER NOT NULL DEFAULT 0,
    zone       INTEGER NOT NULL DEFAULT 0,
    session_id INTEGER REFERENCES session(id),
    thread_id  INTEGER REFERENCES chat_thread(id),
    origin     TEXT NOT NULL CHECK (origin IN ('human','mind','game'))
);
CREATE INDEX chat_line_ts ON chat_line(ts);
CREATE INDEX chat_line_thread ON chat_line(thread_id);

CREATE TABLE game_event (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,       -- unix seconds, mind clock at receipt
    type       TEXT NOT NULL,
    guid       INTEGER,
    payload    TEXT NOT NULL,       -- JSON; its "ts" is the server's unix MILLISECONDS
    session_id INTEGER REFERENCES session(id)
);
CREATE INDEX game_event_ts ON game_event(ts);

CREATE TABLE memory (
    id            INTEGER PRIMARY KEY,
    owner         INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('observation','conversation','event','reflection','plan','story')),
    text          TEXT NOT NULL CHECK (length(text) <= 200),
    t_event       INTEGER NOT NULL, -- unix seconds
    t_created     INTEGER NOT NULL, -- unix seconds
    t_accessed    INTEGER NOT NULL, -- unix seconds
    access_n      INTEGER NOT NULL DEFAULT 0,
    actors        TEXT NOT NULL DEFAULT '[]',   -- JSON list of guids
    place         TEXT,
    boss_id       INTEGER,
    raid_id       INTEGER,
    importance    INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 10),
    valence       REAL NOT NULL CHECK (valence BETWEEN -1 AND 1),
    source        TEXT NOT NULL CHECK (source IN ('game','chat','extract','reflection','offscreen','hearsay','owner')),
    evidence      TEXT NOT NULL DEFAULT '[]',   -- JSON list of "chat:<id>" / "event:<id>" / "memory:<id>"
    confidence    REAL NOT NULL DEFAULT 1.0,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','archived','rejected')),
    superseded_by INTEGER REFERENCES memory(id),
    key           TEXT,
    merged_n      INTEGER NOT NULL DEFAULT 1,
    embedding     BLOB
);
CREATE INDEX memory_owner_status ON memory(owner, status, t_created);
CREATE INDEX memory_unembedded ON memory(id) WHERE embedding IS NULL AND status = 'active';
CREATE UNIQUE INDEX memory_owner_key ON memory(owner, key) WHERE key IS NOT NULL;

CREATE TABLE memory_actor (
    memory_id INTEGER NOT NULL REFERENCES memory(id),
    guid      INTEGER NOT NULL,
    PRIMARY KEY (memory_id, guid)
);
CREATE INDEX memory_actor_guid ON memory_actor(guid, memory_id);

CREATE VIRTUAL TABLE memory_vec USING vec0(
    memory_id INTEGER PRIMARY KEY,
    owner INTEGER PARTITION KEY,
    embedding float[512] distance_metric=cosine
);

CREATE VIRTUAL TABLE memory_fts USING fts5(
    text, content='memory', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER memory_fts_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER memory_fts_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER memory_fts_au AFTER UPDATE OF text ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE relationship (
    a             INTEGER NOT NULL,
    b             INTEGER NOT NULL,
    affinity      REAL NOT NULL DEFAULT 0,
    trust         REAL NOT NULL DEFAULT 0,
    respect       REAL NOT NULL DEFAULT 0,
    attraction    REAL NOT NULL DEFAULT 0,
    familiarity   REAL NOT NULL DEFAULT 0,
    label         TEXT NOT NULL DEFAULT '',
    romance_stage TEXT NOT NULL DEFAULT 'none'
                  CHECK (romance_stage IN ('none','notices','banter','whispers','vulnerable','invitation','acknowledged','ex')),
    notes         TEXT NOT NULL DEFAULT '',
    pinned        TEXT NOT NULL DEFAULT '[]',   -- JSON list of memory ids
    last_seen     INTEGER NOT NULL DEFAULT 0,   -- unix seconds
    interactions  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (a, b)
);

CREATE TABLE rel_delta (
    id       INTEGER PRIMARY KEY,
    a        INTEGER NOT NULL,
    b        INTEGER NOT NULL,
    dim      TEXT NOT NULL,         -- a REL_DIMS value or 'romance_stage'
    delta    REAL NOT NULL,
    reason   TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    ts       INTEGER NOT NULL,      -- unix seconds
    applied  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX rel_delta_pending ON rel_delta(applied, id);

CREATE TABLE persona_core (
    guid        INTEGER PRIMARY KEY,
    json        TEXT NOT NULL,
    card        TEXT NOT NULL,
    card_tokens INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('seed','generated','owner')),
    updated     INTEGER NOT NULL    -- unix seconds
);

CREATE TABLE persona_state (
    guid          INTEGER PRIMARY KEY,
    mood          REAL NOT NULL DEFAULT 0,
    mood_baseline REAL NOT NULL DEFAULT 0,
    goals         TEXT NOT NULL DEFAULT '[]',
    self_notes    TEXT NOT NULL DEFAULT '[]',   -- JSON list of {"text","evidence","added"(unix seconds)}
    altered_state TEXT NOT NULL DEFAULT 'sober',
    state_since   INTEGER NOT NULL DEFAULT 0,   -- unix seconds
    state_session INTEGER NOT NULL DEFAULT 0,
    life_thread   TEXT NOT NULL DEFAULT '',
    updated       INTEGER NOT NULL DEFAULT 0    -- unix seconds
);

CREATE TABLE story (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    participants TEXT NOT NULL DEFAULT '[]',
    evidence     TEXT NOT NULL DEFAULT '[]',
    retold_n     INTEGER NOT NULL DEFAULT 0,
    t_created    INTEGER NOT NULL   -- unix seconds
);

CREATE TABLE bit_use (
    id         INTEGER PRIMARY KEY,
    guid       INTEGER NOT NULL,
    bit_id     TEXT NOT NULL,
    ts         INTEGER NOT NULL,    -- unix seconds
    night      TEXT NOT NULL,       -- night_of() date, YYYY-MM-DD
    voice_bit  INTEGER NOT NULL DEFAULT 0,
    session_id INTEGER REFERENCES session(id)
);
CREATE INDEX bit_use_guid ON bit_use(guid, bit_id, ts);
CREATE INDEX bit_use_night ON bit_use(night, voice_bit);

CREATE TABLE utterance (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,    -- unix seconds
    guid       INTEGER NOT NULL,
    channel    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    memory_ids TEXT NOT NULL DEFAULT '[]',
    dropped    TEXT,
    session_id INTEGER REFERENCES session(id)
);
CREATE INDEX utterance_ts ON utterance(ts);
