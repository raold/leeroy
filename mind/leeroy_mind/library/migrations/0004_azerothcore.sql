-- The reference library has its own schema; this does not migrate mind.db.
-- Keep every chunk id and embedding byte. vec0 and external-content FTS are keyed
-- by those ids. Foreign-key cascades require saving chunk_ref before the rebuild.
CREATE TEMP TABLE chunk_ref_v3 AS SELECT chunk_id, ref FROM chunk_ref;
CREATE TABLE chunks_v4 (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN
        ('world','atlasloot','wiki','curated','acwiki','acdoxygen','serverref')),
    key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN
        ('item','loot','quest','lore','strategy','class','zone','event',
         'documentation','api','command','teleport')),
    title TEXT NOT NULL,
    section TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    url TEXT,
    license TEXT,
    map_id INTEGER,
    instance TEXT,
    ref_ids TEXT NOT NULL DEFAULT '[]',
    t_created INTEGER NOT NULL,
    embedding BLOB,
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0 AND weight <= 1),
    source_revision TEXT,
    source_path TEXT,
    source_sha256 TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, key)
);
INSERT INTO chunks_v4(id,source,key,kind,title,section,text,url,license,map_id,instance,
                      ref_ids,t_created,embedding,weight)
SELECT id,source,key,kind,title,section,text,url,license,map_id,instance,
       ref_ids,t_created,embedding,weight FROM chunks;
DROP TABLE chunks;
ALTER TABLE chunks_v4 RENAME TO chunks;
CREATE INDEX chunks_kind_title ON chunks(kind, title COLLATE NOCASE);
CREATE INDEX chunks_unembedded ON chunks(id) WHERE embedding IS NULL;
CREATE INDEX chunks_source_path ON chunks(source, source_path);
INSERT INTO chunk_ref(chunk_id,ref) SELECT chunk_id,ref FROM chunk_ref_v3;
DROP TABLE chunk_ref_v3;
CREATE TRIGGER chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid,title,section,text) VALUES(new.id,new.title,new.section,new.text);
END;
CREATE TRIGGER chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts,rowid,title,section,text)
    VALUES('delete',old.id,old.title,old.section,old.text);
END;
CREATE TRIGGER chunks_fts_au AFTER UPDATE OF title,section,text ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts,rowid,title,section,text)
    VALUES('delete',old.id,old.title,old.section,old.text);
    INSERT INTO chunks_fts(rowid,title,section,text) VALUES(new.id,new.title,new.section,new.text);
END;

CREATE TABLE source_snapshot (
    source TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    revision TEXT NOT NULL,
    retrieved_at INTEGER NOT NULL,
    imported_at INTEGER NOT NULL,
    license TEXT NOT NULL,
    license_evidence TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    inventory_sha256 TEXT NOT NULL,
    expected_files INTEGER NOT NULL,
    complete INTEGER NOT NULL CHECK (complete = 1),
    coverage TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE source_inventory (
    source TEXT NOT NULL REFERENCES source_snapshot(source) ON DELETE CASCADE,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    url TEXT NOT NULL,
    language TEXT NOT NULL,
    archived INTEGER NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('imported','empty','evidence')),
    chunks INTEGER NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY(source,path)
);
CREATE TABLE server_command (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    syntax TEXT NOT NULL,
    help TEXT NOT NULL,
    handler TEXT NOT NULL,
    permission TEXT NOT NULL,
    allow_console INTEGER CHECK (allow_console IN (0,1)),
    source_path TEXT NOT NULL,
    source_line INTEGER,
    verification TEXT NOT NULL CHECK (verification IN ('source-observed','runtime-verified')),
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE game_tele (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    map INTEGER NOT NULL,
    position_x TEXT NOT NULL,
    position_y TEXT NOT NULL,
    position_z TEXT NOT NULL,
    orientation TEXT NOT NULL,
    map_name TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX game_tele_name ON game_tele(name COLLATE NOCASE);
