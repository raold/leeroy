-- library.db schema v1: the guild's reference shelf. Short chunks of WotLK 3.3.5 facts and lore
-- from the world database, AtlasLoot and warcraft.wiki.gg, searchable by FTS5 and by vector.
-- TIME UNITS: every time column is unix SECONDS from the library's clock.
-- Wiki chunks are CC BY-SA: each carries its page url and license.

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE chunks (
    id        INTEGER PRIMARY KEY,
    source    TEXT NOT NULL CHECK (source IN ('world','atlasloot','wiki')),
    key       TEXT NOT NULL,                 -- stable within its source: item:50415, wiki:4242:0
    kind      TEXT NOT NULL
              CHECK (kind IN ('item','loot','quest','lore','strategy','class','zone','event')),
    title     TEXT NOT NULL,
    section   TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL,
    url       TEXT,
    license   TEXT,
    map_id    INTEGER,
    instance  TEXT,
    ref_ids   TEXT NOT NULL DEFAULT '[]',   -- JSON list: "item:<id>", "npc:<id>", "go:<id>", "quest:<id>"
    t_created INTEGER NOT NULL,             -- unix seconds
    embedding BLOB,                         -- 512 float32, NULL until embedded
    UNIQUE (source, key)
);
CREATE INDEX chunks_kind_title ON chunks(kind, title COLLATE NOCASE);
CREATE INDEX chunks_unembedded ON chunks(id) WHERE embedding IS NULL;

CREATE TABLE chunk_ref (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    ref      TEXT NOT NULL,
    PRIMARY KEY (chunk_id, ref)
);
CREATE INDEX chunk_ref_ref ON chunk_ref(ref, chunk_id);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    title, section, text, content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, title, section, text)
    VALUES (new.id, new.title, new.section, new.text);
END;
CREATE TRIGGER chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, section, text)
    VALUES ('delete', old.id, old.title, old.section, old.text);
END;
CREATE TRIGGER chunks_fts_au AFTER UPDATE OF title, section, text ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, section, text)
    VALUES ('delete', old.id, old.title, old.section, old.text);
    INSERT INTO chunks_fts(rowid, title, section, text)
    VALUES (new.id, new.title, new.section, new.text);
END;

CREATE VIRTUAL TABLE chunks_vec USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    kind TEXT PARTITION KEY,
    embedding float[512] distance_metric=cosine
);

-- Structured rows behind lookup_item() and quest_chain(), written by the world source.
CREATE TABLE item (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    quality   INTEGER NOT NULL,
    ilvl      INTEGER NOT NULL,
    req_level INTEGER NOT NULL,
    slot      TEXT NOT NULL DEFAULT ''      -- e.g. 'two-hand axe', 'plate legs'
);
CREATE INDEX item_name ON item(name COLLATE NOCASE);

CREATE TABLE quest (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    level           INTEGER NOT NULL,
    zone            TEXT NOT NULL DEFAULT '',
    prev_id         INTEGER NOT NULL DEFAULT 0,  -- the quest that must come first, 0 if none
    next_id         INTEGER NOT NULL DEFAULT 0,  -- the quest offered next, 0 if none
    exclusive_group INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX quest_title ON quest(title COLLATE NOCASE);
CREATE INDEX quest_prev ON quest(prev_id);
