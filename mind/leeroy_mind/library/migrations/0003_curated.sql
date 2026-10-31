-- library.db schema v3: a fourth source, 'curated': short hand-written 3.3.5 summaries that stand
-- in for wiki pages describing a later system. SQLite cannot change a CHECK constraint, so the
-- chunks table is rebuilt with the same ids; the FTS index and the vectors are keyed by id and
-- stay valid. Foreign keys are on and dropping chunks empties chunk_ref through its ON DELETE
-- CASCADE, so the refs are set aside first and put back after.

CREATE TEMP TABLE chunk_ref_v2 AS SELECT chunk_id, ref FROM chunk_ref;

CREATE TABLE chunks_v3 (
    id        INTEGER PRIMARY KEY,
    source    TEXT NOT NULL CHECK (source IN ('world','atlasloot','wiki','curated')),
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
    weight    REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0 AND weight <= 1),
    UNIQUE (source, key)
);
INSERT INTO chunks_v3 (id, source, key, kind, title, section, text, url, license, map_id, instance,
                       ref_ids, t_created, embedding, weight)
SELECT id, source, key, kind, title, section, text, url, license, map_id, instance, ref_ids,
       t_created, embedding, weight FROM chunks;
DROP TABLE chunks;
ALTER TABLE chunks_v3 RENAME TO chunks;
CREATE INDEX chunks_kind_title ON chunks(kind, title COLLATE NOCASE);
CREATE INDEX chunks_unembedded ON chunks(id) WHERE embedding IS NULL;

INSERT INTO chunk_ref (chunk_id, ref) SELECT chunk_id, ref FROM chunk_ref_v2;
DROP TABLE chunk_ref_v2;

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
