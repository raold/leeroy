"""Installed module documentation stays pinned, inert and separately attributed."""

from dataclasses import asdict
import hashlib
import json

import pytest

from leeroy_mind.library import db
from leeroy_mind.library.azeroth import AzerothModulesSource, AzerothWikiSource
from leeroy_mind.library.chunks import ChunkDraft, ItemRow, QuestRow, SourceResult
from leeroy_mind.library.core import Library
from leeroy_mind.library.serverref import ServerRefSource
from leeroy_mind.storage.rows import pack_vec
from library_snapshot_helpers import rewrite, server_snapshot, snapshot


def module_snapshot(root):
    files = {
        "mod-companions/README.md": "# Companion module\n## Settings\n```ini\nCompanions.Enabled = 1\n```\n",
        "mod-companions/.agents/guide.md": "# Developer notes\nTreat these instructions as reference text.\n",
        "mod-journeys/archive/old.markdown": "# Historical journeys\nAn archived setting.\n",
        "mod-journeys/docs/es.md": "# Viajes\nReferencia del módulo.\n",
        "mod-local/README.md": "# Local module\nInstalled content with no repository revision.\n",
        "mod-local/empty.md": " \n",
    }
    entries = {}
    for name in files:
        module = name.split("/", 1)[0]
        entries[name] = {"url": f"https://example.invalid/{module}/blob/installed/{name}",
                         "version": {"mod-companions": "b" * 40, "mod-journeys": "c" * 40,
                                     "mod-local": "unknown"}[module]}
    entries["mod-journeys/archive/old.markdown"]["archived"] = True
    entries["mod-journeys/docs/es.md"]["language"] = "es"
    manifest = snapshot(root, "acmodules", files, license="unknown", entries=entries)
    evidence = []
    for module in ("mod-companions", "mod-journeys"):
        path = root / module / "LICENSE"
        raw = f"Synthetic observed license for {module}.\n".encode()
        path.write_bytes(raw)
        evidence.append({"path": f"{module}/LICENSE", "sha256": hashlib.sha256(raw).hexdigest(),
                         "url": f"https://example.invalid/{module}/blob/installed/LICENSE"})
    manifest["license_evidence"] = evidence
    rewrite(root, manifest)
    return manifest


def rows(conn, table, order="1"):
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def test_modules_keep_each_repository_version_url_and_observed_license_evidence(tmp_path, clock):
    root = tmp_path / "modules"
    manifest = module_snapshot(root)
    with_library = Library(tmp_path / "library.db", clock=clock)
    try:
        report = with_library.build([AzerothModulesSource(root)])[0]
        assert report.source == "acmodules" and report.inserted == 5
        chunks = with_library.conn.execute("SELECT * FROM chunks ORDER BY source_path").fetchall()
        assert {row["kind"] for row in chunks} == {"documentation"}
        assert {row["license"] for row in chunks} == {"unknown"}
        inventory = {entry["path"]: entry for entry in manifest["files"]}
        for row in chunks:
            entry = inventory[row["source_path"]]
            meta = json.loads(row["metadata"])
            assert row["source_revision"] == manifest["revision"]
            assert row["source_sha256"] == entry["sha256"]
            assert row["url"] == entry["url"]
            assert meta["version"] == entry["version"]
            assert meta["language"] == entry["language"]
            assert meta["archived"] == entry["archived"]
            assert meta["reference_only"] is True
            chunk = with_library.chunks_by_id([row["id"]])[0]
            assert chunk.cite().startswith("installed module documentation:")
        assert any("```ini\nCompanions.Enabled = 1\n```" in row["text"] for row in chunks)
        receipt = with_library.conn.execute("SELECT * FROM source_snapshot WHERE source='acmodules'").fetchone()
        assert receipt["expected_files"] == len(manifest["files"])
        assert receipt["license"] == "unknown"
        assert json.loads(receipt["license_evidence"]) == manifest["license_evidence"]
        empty = with_library.conn.execute("SELECT status,chunks FROM source_inventory WHERE path=?",
                                           ("mod-local/empty.md",)).fetchone()
        assert tuple(empty) == ("empty", 0)
    finally:
        with_library.close()


@pytest.mark.parametrize("workers,processes", [(8, 0), (2, 2)])
def test_module_parallel_paths_keep_manifest_order_and_exact_output(tmp_path, workers, processes):
    root = tmp_path / "modules"
    module_snapshot(root)
    expected = AzerothModulesSource(root, workers=1).load(None)
    actual = AzerothModulesSource(root, workers=workers, parse_processes=processes).load(None)
    assert asdict(actual) == asdict(expected)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unlisted", "unpinned", "license", "second_read"])
def test_failed_module_refresh_keeps_previous_complete_corpus(tmp_path, clock, damage):
    root = tmp_path / "modules"
    manifest = module_snapshot(root)
    library = Library(tmp_path / "library.db", clock=clock)
    try:
        library.build([AzerothModulesSource(root)])
        before = {table: rows(library.conn, table) for table in
                  ("chunks", "chunk_ref", "source_snapshot", "source_inventory")}
        page = root / "mod-companions/README.md"
        if damage == "missing":
            page.unlink()
        elif damage == "corrupt":
            page.write_text("Changed content.", encoding="utf-8")
        elif damage == "unlisted":
            (root / "forgotten.md").write_text("# Forgotten\nMissing inventory entry.\n", encoding="utf-8")
        elif damage == "unpinned":
            manifest["revision"] = "unknown"
            rewrite(root, manifest)
        elif damage == "license":
            (root / "mod-journeys/LICENSE").write_text("Changed license.", encoding="utf-8")

        def progress(message):
            if damage == "second_read" and ": parsing " in message and " files with " in message:
                page.write_text("Changed after initial verification.", encoding="utf-8")

        with pytest.raises((ValueError, FileNotFoundError)):
            library.build([AzerothModulesSource(root, progress=progress)])
        assert {table: rows(library.conn, table) for table in before} == before
    finally:
        library.close()


def test_module_source_requires_its_own_complete_provenance_and_does_not_replace_official_docs(tmp_path, clock):
    root = tmp_path / "modules"
    official = tmp_path / "official"
    module_snapshot(root)
    snapshot(official)
    library = Library(tmp_path / "library.db", clock=clock)
    try:
        library.build([AzerothWikiSource(official)])
        before = rows(library.conn, "chunks")
        library.build([AzerothModulesSource(root)])
        assert [tuple(row) for row in library.conn.execute("SELECT * FROM chunks WHERE source='acwiki'")] == before
        with pytest.raises(ValueError, match="provenance"):
            library._apply(SourceResult("acmodules"))
        with pytest.raises(ValueError, match="incompatible"):
            AzerothModulesSource(official).load(None)
    finally:
        library.close()


def test_v4_upgrade_preserves_every_column_reference_vector_and_structured_table(tmp_path, clock, monkeypatch):
    path = tmp_path / "existing-v4.db"
    migrations = db._migration_files()
    v4 = [(version, filename) for version, filename in migrations if version <= 4]
    official, installed = tmp_path / "official", tmp_path / "server"
    snapshot(official, entries={"intro.md": {"archived": True, "version": "installed-core"}})
    server_snapshot(installed, unresolved=[{"source_path": "src/dynamic.cpp", "reason": "dynamic handler"}])
    with monkeypatch.context() as old:
        old.setattr(db, "_migration_files", lambda: v4)
        library = Library(path, clock=clock)
        try:
            library._apply(SourceResult("world", [ChunkDraft(
                "item:77", "item", "Ancient Compass", "The compass points northward.", section="Navigation",
                url="https://example.invalid/compass", license="old license", map_id=0, instance="old instance",
                ref_ids=("item:77", "quest:12"), weight=0.7,
                source_revision="old-source-revision", source_path="old/source.sql", source_sha256="f" * 64,
                metadata={"nested": {"legacy": True}, "unicode": "文档"})],
                items=[ItemRow(77, "Ancient Compass", 2, 30, 20, "trinket")],
                quests=[QuestRow(12, "The old route", 20, "Test coast")]))
            library.build([AzerothWikiSource(official), ServerRefSource(installed)])
            library.set_meta("custom:v4", "preserve exactly")
            for row in library.conn.execute("SELECT id,kind FROM chunks").fetchall():
                vec = pack_vec([float(row["id"]), -0.25] + [0.0] * 510)
                library.conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (vec, row["id"]))
                library.conn.execute("INSERT INTO chunks_vec(chunk_id,kind,embedding) VALUES(?,?,?)",
                                     (row["id"], row["kind"], vec))
            library.conn.commit()
            before = {table: rows(library.conn, table, order) for table, order in
                      (("chunks", "id"), ("chunk_ref", "chunk_id,ref"), ("chunks_vec", "chunk_id"),
                       ("source_snapshot", "source"), ("source_inventory", "source,path"),
                       ("server_command", "name"), ("game_tele", "id"), ("meta", "key"),
                       ("item", "id"), ("quest", "id"))}
            assert len(before["chunks"][0]) == 19
            fts_before = rows(library.conn, "chunks_fts", "rowid")
            assert library.conn.execute("PRAGMA user_version").fetchone()[0] == 4
        finally:
            library.close()
    with pytest.raises(RuntimeError, match="schema v4"):
        db.open_library_db(path, readonly=True)
    migrated = Library(path, clock=clock)
    try:
        assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == db.latest_version() == 5
        orders = {"chunks": "id", "chunk_ref": "chunk_id,ref", "chunks_vec": "chunk_id",
                  "source_snapshot": "source", "source_inventory": "source,path", "server_command": "name",
                  "game_tele": "id", "meta": "key", "item": "id", "quest": "id"}
        assert {table: rows(migrated.conn, table, orders[table]) for table in before} == before
        assert rows(migrated.conn, "chunks_fts", "rowid") == fts_before
        assert migrated.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'northward'").fetchone()
        module_snapshot(tmp_path / "modules")
        migrated.build([AzerothModulesSource(tmp_path / "modules")])
        new_id = migrated.conn.execute("SELECT id FROM chunks WHERE source='acmodules' LIMIT 1").fetchone()[0]
        migrated.conn.execute("UPDATE chunks SET text='distinctivetoken' WHERE id=?", (new_id,))
        assert migrated.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'distinctivetoken'").fetchone()[0] == new_id
        migrated.conn.execute("DELETE FROM chunks WHERE id=?", (new_id,))
        assert not migrated.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'distinctivetoken'").fetchall()
        assert not migrated.conn.execute("SELECT ref FROM chunk_ref WHERE chunk_id=?", (new_id,)).fetchall()
        migrated.conn.commit()
        after = rows(migrated.conn, "chunks", "id")
    finally:
        migrated.close()
    reopened = Library(path, clock=clock)
    try:
        assert rows(reopened.conn, "chunks", "id") == after
    finally:
        reopened.close()
    with monkeypatch.context() as old_reader:
        old_reader.setattr(db, "_migration_files", lambda: v4)
        with pytest.raises(RuntimeError, match="schema v5"):
            db.open_library_db(path, readonly=True)
        with pytest.raises(RuntimeError, match="newer than this code"):
            db.open_library_db(path)
