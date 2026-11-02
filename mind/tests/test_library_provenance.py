import sqlite3

import pytest
import sqlite_vec

from leeroy_mind.library.chunks import ChunkDraft, SourceResult
from leeroy_mind.library.core import Library
from leeroy_mind.library.db import MIGRATIONS_DIR, latest_version
from leeroy_mind.storage.rows import pack_vec


def test_schema_three_migration_preserves_ids_refs_fts_and_exact_vector_bytes(tmp_path, clock):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path, autocommit=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys=ON")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(migration.name[:4]) <= 3:
            conn.executescript(migration.read_text(encoding="utf-8"))
    vec = pack_vec([0.25, -0.75, 0.5] + [0.0] * 509)
    conn.execute("INSERT INTO chunks(id,source,key,kind,title,section,text,url,license,map_id,instance,"
                 "ref_ids,t_created,embedding,weight) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (81, "world", "item:77", "item", "Ancient Compass", "Navigation", "The compass points northward.",
                  "https://example.invalid/compass", "old license", 0, "old instance", '["item:77"]', 123, vec, 0.7))
    conn.execute("INSERT INTO chunk_ref(chunk_id,ref) VALUES(81,'item:77')")
    conn.execute("INSERT INTO chunks_vec(chunk_id,kind,embedding) VALUES(81,'item',?)", (vec,))
    conn.execute("INSERT INTO item(id,name,quality,ilvl,req_level,slot) VALUES(77,'Ancient Compass',2,30,20,'trinket')")
    conn.execute("PRAGMA user_version=3")
    before = tuple(conn.execute("SELECT * FROM chunks").fetchone())
    fts_before = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'northward'").fetchall()
    conn.close()
    library = Library(path, clock=clock)
    try:
        assert library.conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() == 4
        row = library.conn.execute("SELECT * FROM chunks WHERE id=81").fetchone()
        assert tuple(row)[:len(before)] == before
        assert row["embedding"] == vec and row["metadata"] == "{}" and row["source_revision"] is None
        assert [tuple(r) for r in library.conn.execute("SELECT chunk_id,ref FROM chunk_ref")] == [(81, "item:77")]
        assert library.conn.execute("SELECT embedding FROM chunks_vec WHERE chunk_id=81").fetchone()[0] == vec
        assert [tuple(r) for r in library.conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'northward'")] == fts_before
        assert library.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert library.conn.execute("SELECT name FROM item WHERE id=77").fetchone()[0] == "Ancient Compass"
        # Recreated triggers must update and delete normally after the table swap.
        library._apply(SourceResult("world", [ChunkDraft("item:77", "item", "Ancient Compass",
                               "The compass points southward.", ref_ids=("item:78",))]))
        assert library.conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 0
        assert library.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'northward'").fetchall() == []
        assert library.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'southward'").fetchone()[0] == 81
        assert library.conn.execute("SELECT ref FROM chunk_ref").fetchone()[0] == "item:78"
        library._apply(SourceResult("world"))
        assert library.conn.execute("SELECT count(*) FROM chunk_ref").fetchone()[0] == 0
        assert library.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'southward'").fetchall() == []
    finally:
        library.close()


def test_migration_is_idempotent_and_existing_readonly_library_requires_explicit_upgrade(tmp_path, clock):
    path = tmp_path / "library.db"
    first = Library(path, clock=clock)
    first._apply(SourceResult("wiki", [ChunkDraft("one", "lore", "The coast", "A coastline.")]))
    row = tuple(first.conn.execute("SELECT * FROM chunks").fetchone())
    first.close()
    second = Library(path, clock=clock)
    assert tuple(second.conn.execute("SELECT * FROM chunks").fetchone()) == row
    second.close()
    readonly = Library(path, clock=clock, readonly=True)
    assert readonly.chunks_by_id([row[0]])[0].title == "The coast"
    with pytest.raises(sqlite3.OperationalError):
        readonly._apply(SourceResult("wiki"))
    readonly.close()
