"""Configuration templates are literal, complete reference sources, never live settings."""
from dataclasses import asdict
import hashlib

import pytest

from leeroy_mind.library.azeroth import (
    CONFIG_TEMPLATE_NOTICE, AzerothDocsSource, AzerothModulesSource, _Document, _parse_document,
)
from leeroy_mind.library.chunks import SourceFile
from leeroy_mind.library.core import Library
from leeroy_mind.library.snapshots import OfflineSnapshot
from leeroy_mind.storage.rows import pack_vec
from library_snapshot_helpers import snapshot


TEMPLATE_PATH = "mod-companions/conf/companions.conf.dist"
TEMPLATE = (
    "[worldserver]\r\n\r\n"
    "########################################\r\n"
    "# Companion template; defaults can be overridden per run.\r\n"
    "# Companions.SmartRest\r\n"
    "# Rest together after a threshold, then recover fully.\r\n"
    "# Default: 0\r\n\r\n"
    "Companions.SmartRest = 0\r\n\r\n"
    "# Disabled example, not an active default:\r\n"
    "# Companions.SmartRest.Heroic = 1\r\n\r\n"
    "unknown directive {keep exactly}\r\n"
    "# Deprecated footer with a literal tab:\tretained\r\n"
)


def parse(text, path=TEMPLATE_PATH):
    raw = text.encode("utf-8")
    file = SourceFile(path, hashlib.sha256(raw).hexdigest(), len(raw),
                      "https://example.invalid/modules/blob/pinned/" + path,
                      version="b" * 40)
    return _parse_document(_Document(raw, "acmodules", file, "unknown", "a" * 40))


def raw(chunk):
    assert chunk.text.startswith(CONFIG_TEMPLATE_NOTICE + "\n")
    return chunk.text[len(CONFIG_TEMPLATE_NOTICE) + 1:]


def assignment(chunk):
    meta = chunk.metadata
    lines = raw(chunk).splitlines(keepends=True)
    start = meta["assignment_line_start"] - meta["source_line_start"]
    end = meta["assignment_line_end"] - meta["source_line_start"] + 1
    return "".join(lines[start:end])


def test_setting_comments_defaults_and_disabled_examples_remain_literal():
    chunks, status = parse(TEMPLATE)
    assert "".join(raw(chunk) for chunk in chunks) == TEMPLATE
    active = next(chunk for chunk in chunks if chunk.section == "Companions.SmartRest")
    assert "# Rest together after a threshold, then recover fully.\r\n" in active.text
    assert assignment(active) == "Companions.SmartRest = 0\r\n"
    assert active.metadata["assignment_state"] == "active"
    assert active.metadata["assignment_line_start"] == active.metadata["assignment_line_end"] == 9
    example = next(chunk for chunk in chunks if chunk.section == "Companions.SmartRest.Heroic")
    assert assignment(example) == "# Companions.SmartRest.Heroic = 1\r\n"
    assert example.metadata["assignment_state"] == "commented"
    assert chunks[0].section == "Template preamble"
    assert chunks[-1].section == "Template notes"
    assert status.status == "imported" and status.chunks == len(chunks)
    for chunk in chunks:
        meta = chunk.metadata
        assert chunk.title == "Configuration template: companions.conf.dist"
        assert chunk.kind == "documentation" and chunk.license == "unknown"
        assert meta["document_format"] == "configuration-template"
        assert meta["template_only"] is True and meta["reference_only"] is True
        assert meta["version"] == "b" * 40
        assert chunk.source_sha256 == status.sha256 and chunk.source_path == TEMPLATE_PATH
        assert len(raw(chunk).splitlines()) == meta["source_line_end"] - meta["source_line_start"] + 1
        if meta["assignment_state"] == "none":
            assert meta["assignment_line_start"] is meta["assignment_line_end"] is None


@pytest.mark.parametrize("value", [
    '"first line\nOther.Key = still inside the value\nlast line"',
    '"escaped \\\" quote and \\\\ slash\nПример {name}"',
    "[\n  1, 2,\n  3\n]",
    "one \\\n  two \\\n  three",
    "", '""', '"https://example.invalid/{path}?a=b#fragment"',
    r'"C:\\path\\to\\file"', "don't change the unquoted apostrophe",
])
def test_multiline_and_special_values_are_indivisible_without_evaluation(value):
    source = "# Keep value syntax.\nCompanions.Value = " + value + "\nCompanions.After = 2\n"
    chunks, _ = parse(source)
    assert "".join(raw(chunk) for chunk in chunks) == source
    assert [chunk.section for chunk in chunks] == ["Companions.Value", "Companions.After"]
    assert assignment(chunks[0]) == "Companions.Value = " + value + "\n"
    assert assignment(chunks[1]) == "Companions.After = 2\n"


def test_multiline_commented_example_keeps_markers_and_does_not_swallow_next_active_setting():
    source = ('# Companions.Example = "first\n# Other.Key = quoted example\n# last"\n'
              '# Companions.Unclosed = "example\nCompanions.Active = 2\n')
    chunks, _ = parse(source)
    assert "".join(raw(chunk) for chunk in chunks) == source
    assert [chunk.section for chunk in chunks] == [
        "Companions.Example", "Companions.Unclosed", "Companions.Active"]
    assert assignment(chunks[0]) == source[:source.index("# Companions.Unclosed")]
    assert chunks[-1].metadata["assignment_state"] == "active"


def test_large_commentary_splits_without_splitting_the_complete_assignment():
    comments = "".join(f"# explanation {i} " + "detail " * 35 + "\n" for i in range(30))
    value = 'Companions.Prompt = "' + "phrase " * 280 + '"\n'
    source = comments + value + "# Footer remains.\n"
    chunks, _ = parse(source)
    assert "".join(raw(chunk) for chunk in chunks) == source
    carrying = [chunk for chunk in chunks if chunk.metadata["assignment_state"] == "active"]
    assert len(carrying) == 1 and assignment(carrying[0]) == value
    assert raw(carrying[0]) == value
    assert all(len(raw(chunk).split()) <= 240 for chunk in chunks if chunk not in carrying)
    assert all("```" not in chunk.text for chunk in chunks)


@pytest.mark.parametrize("source", ["", " \r\n\t", "[worldserver]\n# only comments\n??? unknown\n"])
def test_empty_preamble_and_unrecognized_syntax_are_accounted_for(source):
    chunks, status = parse(source)
    assert "".join(raw(chunk) for chunk in chunks) == source
    assert status.status == ("imported" if source else "empty")
    assert all(chunk.metadata["assignment_state"] == "none" for chunk in chunks)


@pytest.mark.parametrize("source,path", [
    ("acwiki", "sample.conf.dist"), ("acdoxygen", "sample.conf.dist"),
    ("acmodules", "sample.conf"), ("acmodules", "sample.conf.dist.bak"),
])
def test_only_module_template_suffix_is_accepted_as_a_document(tmp_path, source, path):
    root = tmp_path / "snapshot"
    snapshot(root, source, {path: "Companions.Enabled = 1\n"})
    with pytest.raises(ValueError, match="cover every documentation file"):
        AzerothDocsSource(root, name=source).load(None)


def test_unlisted_live_config_is_never_read(tmp_path, monkeypatch):
    root = tmp_path / "snapshot"
    snapshot(root, "acmodules", {TEMPLATE_PATH: TEMPLATE})
    (root / "private.conf").write_text("Never import this live file.", encoding="utf-8")
    read = OfflineSnapshot.read
    observed = []

    def checked_read(self, file):
        observed.append(file.path)
        assert file.path.endswith(".conf.dist")
        return read(self, file)

    monkeypatch.setattr(OfflineSnapshot, "read", checked_read)
    result = AzerothModulesSource(root).load(None)
    assert result.provenance.expected_files == 1
    assert observed == [TEMPLATE_PATH, TEMPLATE_PATH]


@pytest.mark.parametrize("workers,processes", [(8, 0), (2, 2)])
def test_threads_and_processes_keep_exact_template_order_and_provenance(tmp_path, workers, processes):
    root = tmp_path / "snapshot"
    snapshot(root, "acmodules", {TEMPLATE_PATH: TEMPLATE, "readme.md": "# Module\nDocumentation.\n",
                                  "nested/EXAMPLE.CONF.DIST": "# Remark\nExample.Enabled = 1\n"})
    expected = AzerothModulesSource(root, workers=1).load(None)
    actual = AzerothModulesSource(root, workers=workers, parse_processes=processes).load(None)
    assert asdict(actual) == asdict(expected)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unlisted", "second_read"])
def test_failed_template_refresh_preserves_the_previous_complete_corpus(tmp_path, clock, damage):
    root = tmp_path / "snapshot"
    snapshot(root, "acmodules", {TEMPLATE_PATH: TEMPLATE})
    library = Library(tmp_path / "library.db", clock=clock)
    tables = ("chunks", "chunk_ref", "source_snapshot", "source_inventory")
    try:
        library.build([AzerothModulesSource(root)])
        before = {table: [tuple(row) for row in library.conn.execute(f"SELECT * FROM {table}")]
                  for table in tables}
        path = root / TEMPLATE_PATH
        if damage == "missing":
            path.unlink()
        elif damage == "corrupt":
            path.write_text("Changed = 1\n", encoding="utf-8")
        elif damage == "unlisted":
            (root / "forgotten.conf.dist").write_text("Forgotten = 1\n", encoding="utf-8")

        def progress(message):
            if damage == "second_read" and ": parsing " in message and " files with " in message:
                path.write_text("Changed = 1\n", encoding="utf-8")

        with pytest.raises((ValueError, FileNotFoundError)):
            library.build([AzerothModulesSource(root, progress=progress)])
        assert {table: [tuple(row) for row in library.conn.execute(f"SELECT * FROM {table}")]
                for table in tables} == before
    finally:
        library.close()


def test_added_templates_preserve_all_markdown_keys_text_metadata_and_vectors(tmp_path, clock):
    root = tmp_path / "snapshot"
    markdown = {"guide.md": "# Guide\n## Section\nNormal Markdown text.\n",
                "code.markdown": "# Code\n```ini\nCompanions.Old = 1\n```\n",
                "unicode.md": "# 文档\n原始说明。\n"}
    snapshot(root, "acmodules", markdown)
    original = AzerothModulesSource(root).load(None)
    library = Library(tmp_path / "library.db", clock=clock)
    try:
        library.build([AzerothModulesSource(root)])
        with library._tx():
            for row in library.conn.execute("SELECT id,kind FROM chunks").fetchall():
                vector = pack_vec([float(row["id"])] + [0.0] * 511)
                library.conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (vector, row["id"]))
                library.conn.execute("INSERT INTO chunks_vec(chunk_id,kind,embedding) VALUES(?,?,?)",
                                     (row["id"], row["kind"], vector))
        before = [tuple(row) for row in library.conn.execute("SELECT * FROM chunks ORDER BY id")]
        snapshot(root, "acmodules", {**markdown, TEMPLATE_PATH: TEMPLATE})
        extended = AzerothModulesSource(root).load(None)
        assert [asdict(chunk) for chunk in extended.chunks if chunk.source_path in markdown] == [
            asdict(chunk) for chunk in original.chunks]
        report = library.build([AzerothModulesSource(root)])[0]
        assert report.unchanged == len(original.chunks) and report.updated == 0 and report.deleted == 0
        assert [tuple(row) for row in library.conn.execute(
            "SELECT * FROM chunks WHERE source_path<>? ORDER BY id", (TEMPLATE_PATH,))] == before
        matches = library.search_reference_text("Companions.SmartRest", kinds=("documentation",))
        assert any(chunk.section == "Companions.SmartRest" and "recover fully" in chunk.text
                   and "Companions.SmartRest = 0" in chunk.text for chunk in matches)
    finally:
        library.close()
