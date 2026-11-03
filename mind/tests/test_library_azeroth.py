import json
from dataclasses import replace

import pytest

from leeroy_mind.library.azeroth import AzerothDoxygenSource, AzerothWikiSource
from leeroy_mind.library.chunks import SourceResult
from leeroy_mind.library.core import Library
from leeroy_mind.library.snapshots import OfflineSnapshot
from leeroy_mind.storage.rows import pack_vec
from library_snapshot_helpers import rewrite, snapshot


@pytest.fixture
def lib(tmp_path, clock):
    library = Library(tmp_path / "library.db", clock=clock)
    yield library
    library.close()


def test_all_markdown_languages_archives_and_exact_fenced_syntax(tmp_path, lib):
    root = tmp_path / "wiki"
    snapshot(root, files={
        "Commands.md": "# GM reference\n## Exact syntax\n```sql\nSELECT id, name FROM game_tele;\n```\n",
        "zh/docs.markdown": "# 文档\n服务器参考与命令。\n",
        "archive/old.md": "# Historical settings\nFor a previous software version.\n",
        "empty.md": " \n"}, entries={"zh/docs.markdown": {"language": "zh"},
                                   "archive/old.md": {"archived": True, "version": "old-core"}})
    report = lib.build([AzerothWikiSource(root)])[0]
    assert report.inserted == 3
    rows = lib.conn.execute("SELECT * FROM chunks ORDER BY source_path").fetchall()
    assert "SELECT id, name FROM game_tele;" in rows[0]["text"]
    assert "```sql\n" in rows[0]["text"]
    assert json.loads(rows[1]["metadata"])["archived"] is True
    assert json.loads(rows[2]["metadata"])["language"] == "zh"
    inv = lib.conn.execute("SELECT * FROM source_inventory WHERE path='empty.md'").fetchone()
    assert inv["status"] == "empty" and inv["chunks"] == 0 and inv["note"]
    provenance = lib.conn.execute("SELECT * FROM source_snapshot").fetchone()
    assert provenance["expected_files"] == 4 and provenance["license"] == "MIT"
    chunk = lib.chunks_by_id([rows[0]["id"]])[0]
    assert "revision " + "a" * 40 in chunk.cite()
    assert chunk.source_path == "Commands.md"


def test_html_import_retains_api_code_and_tables_omits_scripts_and_navigation(tmp_path, lib):
    root = tmp_path / "doxygen"
    snapshot(root, "acdoxygen", {"docs/classMap.html": """<html><head><title>Map reference</title>
    <style>.hidden { color:red; }</style></head><body><nav>navigation noise</nav>
    <h1>Map</h1><h2>Methods</h2><p>Enter a map.</p><pre>void Enter(Player* p);\nreturn;</pre>
    <table><tr><th>Field</th><th>Meaning</th></tr><tr><td>mapId</td><td>Map identifier</td></tr></table>
    <script>maliciousInstruction()</script></body></html>"""}, license="unknown")
    lib.build([AzerothDoxygenSource(root)])
    rows = lib.conn.execute("SELECT * FROM chunks").fetchall()
    text = "\n".join(r["text"] for r in rows)
    assert "void Enter(Player* p);\nreturn;" in text
    assert "mapId" in text and "Map identifier" in text
    assert "maliciousInstruction" not in text and "navigation noise" not in text
    assert all(r["kind"] == "api" and r["license"] == "unknown" for r in rows)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unlisted", "partial", "errors", "inventory",
                                    "duplicate", "license", "unpinned", "traversal"])
def test_failed_refresh_preserves_previous_complete_corpus(tmp_path, lib, damage):
    root = tmp_path / "wiki"
    manifest = snapshot(root)
    lib.build([AzerothWikiSource(root)])
    before = tuple(lib.conn.execute("SELECT * FROM chunks").fetchone())
    receipt = tuple(lib.conn.execute("SELECT * FROM source_snapshot").fetchone())
    if damage == "missing":
        (root / "intro.md").unlink()
    elif damage == "corrupt":
        (root / "intro.md").write_text("corrupt", encoding="utf-8")
    elif damage == "unlisted":
        (root / "forgotten.md").write_text("# Forgotten\nNot in the manifest.", encoding="utf-8")
    elif damage == "partial":
        manifest["complete"] = False
    elif damage == "errors":
        manifest["errors"] = ["download failed"]
    elif damage == "inventory":
        manifest["expected_files"] += 1
    elif damage == "duplicate":
        manifest["files"].append(dict(manifest["files"][0]))
        manifest["expected_files"] += 1
    elif damage == "license":
        (root / ".github/LICENSE").write_text("changed license", encoding="utf-8")
    elif damage == "unpinned":
        manifest["revision"] = "latest"
    elif damage == "traversal":
        manifest["files"][0]["path"] = "../intro.md"
    rewrite(root, manifest)
    with pytest.raises((ValueError, FileNotFoundError)):
        lib.build([AzerothWikiSource(root)])
    assert tuple(lib.conn.execute("SELECT * FROM chunks").fetchone()) == before
    assert tuple(lib.conn.execute("SELECT * FROM source_snapshot").fetchone()) == receipt


def test_missing_provenance_and_forged_chunk_inventory_cannot_replace_source(tmp_path, lib):
    root = tmp_path / "wiki"
    snapshot(root)
    source = AzerothWikiSource(root)
    lib.build([source])
    with pytest.raises(ValueError, match="provenance"):
        lib._apply(SourceResult("acwiki"))
    result = source.load(lib)
    result.chunks[0] = replace(result.chunks[0], source_sha256="0" * 64)
    with pytest.raises(ValueError, match="inventory"):
        lib._apply(result)
    assert lib.conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


def test_revision_and_attribution_refresh_preserves_ids_and_vector_bytes(tmp_path, lib):
    root = tmp_path / "wiki"
    manifest = snapshot(root)
    lib.build([AzerothWikiSource(root)])
    row = lib.conn.execute("SELECT id,kind FROM chunks").fetchone()
    vec = pack_vec([1.0] + [0.0] * 511)
    lib.conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (vec, row["id"]))
    lib.conn.execute("INSERT INTO chunks_vec(chunk_id,kind,embedding) VALUES(?,?,?)",
                     (row["id"], row["kind"], vec))
    lib.conn.commit()
    assert lib.build([AzerothWikiSource(root)])[0].unchanged == 1
    manifest["revision"] = "b" * 40
    manifest["files"][0]["url"] = "https://www.azerothcore.org/wiki/new-location"
    rewrite(root, manifest)
    assert lib.build([AzerothWikiSource(root)])[0].updated == 1
    after = lib.conn.execute("SELECT * FROM chunks").fetchone()
    assert after["id"] == row["id"] and after["embedding"] == vec
    assert lib.conn.execute("SELECT embedding FROM chunks_vec").fetchone()[0] == vec
    assert after["source_revision"] == "b" * 40


def test_new_section_keeps_unmodified_chunk_id_and_doxygen_is_separate_source(tmp_path, lib):
    root = tmp_path / "wiki"
    snapshot(root, files={"intro.md": "# Introduction\n## First\nOriginal reference.\n"})
    lib.build([AzerothWikiSource(root)])
    original = lib.conn.execute("SELECT id,key FROM chunks").fetchone()
    snapshot(root, files={"intro.md": "# Introduction\n## First\nOriginal reference.\n## Second\nAdded reference.\n"})
    report = lib.build([AzerothWikiSource(root)])[0]
    assert report.inserted == 1 and report.deleted == 0
    assert lib.conn.execute("SELECT id FROM chunks WHERE key=?", (original["key"],)).fetchone()[0] == original["id"]
    docs = tmp_path / "doxygen"
    snapshot(docs, "acdoxygen", {"page.html": "<html><body>API description.</body></html>"}, license="unknown")
    lib.build([AzerothDoxygenSource(docs)])
    assert lib.conn.execute("SELECT count(*) FROM chunks WHERE source='acwiki'").fetchone()[0] == 2


def test_known_license_requires_verified_evidence_and_duplicate_json_rejected(tmp_path):
    root = tmp_path / "wiki"
    manifest = snapshot(root)
    manifest["license_evidence"] = []
    rewrite(root, manifest)
    with pytest.raises(ValueError, match="license"):
        OfflineSnapshot(root, "acwiki")
    (root / "manifest.json").write_text('{"complete":true,"complete":false}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON"):
        OfflineSnapshot(root, "acwiki")


def test_hand_built_empty_or_unprocessed_receipt_cannot_erase_complete_source(tmp_path, lib):
    root = tmp_path / "wiki"
    snapshot(root)
    source = AzerothWikiSource(root)
    lib.build([source])
    result = source.load(lib)
    for provenance in (replace(result.provenance, files=(), expected_files=0),
                       OfflineSnapshot(root, "acwiki").provenance):
        with pytest.raises(ValueError, match="incomplete"):
            lib._apply(SourceResult("acwiki", provenance=provenance))
    assert lib.conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


@pytest.mark.parametrize("status,chunks", [("imported", 0), ("empty", 1), ("evidence", 1)])
def test_processed_inventory_status_must_match_chunk_count(tmp_path, lib, status, chunks):
    root = tmp_path / "wiki"
    snapshot(root)
    result = AzerothWikiSource(root).load(lib)
    file = replace(result.provenance.files[0], status=status, chunks=chunks)
    result.provenance = replace(result.provenance, files=(file,))
    with pytest.raises(ValueError, match="incomplete"):
        lib._apply(result)
