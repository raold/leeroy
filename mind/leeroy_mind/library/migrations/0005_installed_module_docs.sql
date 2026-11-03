-- Installed module documentation is an explicit snapshot source. Preserve every
-- v4 column, id, vector byte and FTS row while extending only the source CHECK.
-- Dropping chunks cascades to chunk_ref, so retain and restore those rows.
CREATE TEMP TABLE chunk_ref_v4 AS SELECT chunk_id, ref FROM chunk_ref;
CREATE TABLE chunks_v5 (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN
        ('world','atlasloot','wiki','curated','acwiki','acdoxygen','serverref','acmodules')),
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
INSERT INTO chunks_v5(id,source,key,kind,title,section,text,url,license,map_id,instance,
                      ref_ids,t_created,embedding,weight,source_revision,source_path,source_sha256,metadata)
SELECT id,source,key,kind,title,section,text,url,license,map_id,instance,
       ref_ids,t_created,embedding,weight,source_revision,source_path,source_sha256,metadata FROM chunks;
DROP TABLE chunks;
ALTER TABLE chunks_v5 RENAME TO chunks;
CREATE INDEX chunks_kind_title ON chunks(kind, title COLLATE NOCASE);
CREATE INDEX chunks_unembedded ON chunks(id) WHERE embedding IS NULL;
CREATE INDEX chunks_source_path ON chunks(source, source_path);
INSERT INTO chunk_ref(chunk_id,ref) SELECT chunk_id,ref FROM chunk_ref_v4;
DROP TABLE chunk_ref_v4;
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
