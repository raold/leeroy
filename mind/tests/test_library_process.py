"""Optional spawned CPU parsing uses only synthetic offline documents."""

from concurrent.futures import Future
from dataclasses import asdict
import hashlib
import multiprocessing
from pathlib import Path
import threading

import pytest

from leeroy_mind.library import azeroth
from leeroy_mind.library.azeroth import AzerothDoxygenSource, AzerothWikiSource, _Document, _parse_document
from leeroy_mind.library.chunks import SourceFile
from leeroy_mind.library.core import Library
from leeroy_mind.library.snapshots import (MAX_SNAPSHOT_PARSE_PROCESSES, _bounded_executor_map,
                                         snapshot_parse_processes)
from library_snapshot_helpers import snapshot


class NoDatabase:
    def __getattribute__(self, name):
        raise AssertionError(f"parser touched library.{name}")


@pytest.mark.parametrize("name,source_type", [("acwiki", AzerothWikiSource), ("acdoxygen", AzerothDoxygenSource)])
def test_spawned_processes_preserve_exact_chunks_provenance_and_manifest_order(tmp_path, monkeypatch, name, source_type):
    root = tmp_path / "docs"
    if name == "acwiki":
        files = {"z.md": "# Last\n## Repeat\nSame.\n## Repeat\nSame.\n",
                 "a.md": "# First\n```cpp\nvoid Tick(uint32 diff);\n```\n",
                 "folder/unicode.md": "# 文档\nExact reference text.\n", "empty.md": " \n"}
    else:
        files = {"z.html": "<head><title>Last</title></head><h1>Last</h1><p>Same.</p><p>Same.</p>",
                 "a.html": "<h1>First</h1><pre>void Tick(uint32 diff);</pre><script>hidden()</script>",
                 "folder/unicode.html": "<h1>文档</h1><table><tr><td>名称</td><td>説明</td></tr></table>",
                 "empty.html": "<style>.hidden{}</style>"}
    snapshot(root, name, files, license="unknown")
    expected = source_type(root, workers=1).load(NoDatabase())
    messages, contexts = [], []
    coordinator = threading.get_ident()
    original = azeroth.ProcessPoolExecutor
    previous_children = {child.pid for child in multiprocessing.active_children()}

    def executor(*args, **kwargs):
        contexts.append(kwargs["mp_context"].get_start_method())
        return original(*args, **kwargs)

    def progress(message):
        assert threading.get_ident() == coordinator
        messages.append(message)

    monkeypatch.setattr(azeroth, "ProcessPoolExecutor", executor)
    actual = source_type(root, workers=3, parse_processes=2, progress=progress).load(NoDatabase())
    assert asdict(actual) == asdict(expected)
    assert [file.path for file in actual.provenance.files] == list(files)
    assert contexts == ["spawn"]
    assert any("2 processes (3 read workers)" in message for message in messages)
    assert {child.pid for child in multiprocessing.active_children()} <= previous_children


def test_default_thread_path_never_constructs_a_process_pool(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    snapshot(root)

    def forbidden(*args, **kwargs):
        pytest.fail("default parsing must not start CPU processes")

    monkeypatch.setattr(azeroth, "ProcessPoolExecutor", forbidden)
    result = AzerothWikiSource(root, workers=8).load(NoDatabase())
    assert result.chunks and result.provenance.complete


@pytest.mark.parametrize("global_license,file_license", [("unknown", "MIT"), ("MIT", "unknown")])
def test_undocumented_file_license_cannot_override_validated_snapshot_attribution(tmp_path,
                                                                              global_license, file_license):
    root = tmp_path / "docs"
    snapshot(root, license=global_license, entries={"intro.md": {"license": file_license}})
    result = AzerothWikiSource(root, workers=1).load(NoDatabase())
    assert result.provenance.license == global_license
    assert result.chunks and all(chunk.license == global_license for chunk in result.chunks)
    if global_license == "unknown":
        assert result.provenance.license_evidence == ()


def test_cpu_entry_point_reads_only_supplied_bytes_not_source_files(tmp_path, monkeypatch):
    raw = b"<h1>Exact API</h1><pre>bool Ready();</pre>"
    file = SourceFile("nonexistent/api.html", hashlib.sha256(raw).hexdigest(), len(raw), "https://example.invalid/api")

    def forbidden(*args, **kwargs):
        pytest.fail("pure CPU parser attempted filesystem access")

    for method in ("open", "stat", "lstat", "resolve", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, method, forbidden)
    chunks, processed = _parse_document(_Document(raw, "acdoxygen", file, "unknown", "a" * 40))
    assert processed.status == "imported" and processed.chunks == len(chunks)
    assert "bool Ready();" in "\n".join(chunk.text for chunk in chunks)
    assert all(chunk.source_path == "nonexistent/api.html" for chunk in chunks)


@pytest.mark.parametrize("failure", ["read", "parse"])
def test_process_path_failure_joins_children_and_preserves_previous_corpus(tmp_path, clock, failure):
    root = tmp_path / "docs"
    snapshot(root)
    library = Library(tmp_path / "library.db", clock=clock)
    previous_children = {child.pid for child in multiprocessing.active_children()}
    try:
        library.build([AzerothWikiSource(root, workers=1)])
        before = {table: [tuple(row) for row in library.conn.execute(f"SELECT * FROM {table}")]
                  for table in ("chunks", "source_snapshot", "source_inventory")}
        files = {f"page{i}.md": f"# Page {i}\nReference.\n" for i in range(8)}
        files["last.md"] = b"\xffinvalid utf8" if failure == "parse" else b"# Last\nOriginal.\n"
        snapshot(root, files=files)
        (root / "intro.md").unlink()

        def progress(message):
            if failure == "read" and ": parsing " in message and " files with " in message:
                (root / "last.md").write_bytes(b"# Last\nChanged.\n")

        with pytest.raises(UnicodeDecodeError if failure == "parse" else ValueError):
            library.build([AzerothWikiSource(root, workers=2, parse_processes=2, progress=progress)])
        after = {table: [tuple(row) for row in library.conn.execute(f"SELECT * FROM {table}")]
                 for table in before}
        assert after == before
        assert {child.pid for child in multiprocessing.active_children()} <= previous_children
    finally:
        library.close()


def test_borrowed_executor_window_retains_order_and_bounds_submissions():
    class ManualExecutor:
        def __init__(self):
            self.submitted = []

        def submit(self, function, value):
            future = Future()
            future.set_result(function(value))
            self.submitted.append((value, future))
            return future

    executor = ManualExecutor()
    results = _bounded_executor_map(executor, lambda value: value * 2, range(1000), limit=8)
    assert next(results) == 0
    assert [value for value, _ in executor.submitted] == list(range(8))
    assert next(results) == 2
    assert [value for value, _ in executor.submitted] == list(range(9))
    results.close()
    assert len(executor.submitted) == 9


def test_processes_are_created_only_during_load_not_source_construction(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("construction created a process pool")

    monkeypatch.setattr(azeroth, "ProcessPoolExecutor", forbidden)
    assert AzerothWikiSource(tmp_path / "missing", parse_processes=4).parse_processes == 4
    assert AzerothDoxygenSource(tmp_path / "missing", parse_processes=8).parse_processes == 8


@pytest.mark.parametrize("value", [-1, MAX_SNAPSHOT_PARSE_PROCESSES + 1, True, 2.5, "4", None])
def test_process_count_is_explicit_and_bounded(tmp_path, value):
    with pytest.raises(ValueError, match="snapshot parse processes"):
        snapshot_parse_processes(value)
    with pytest.raises(ValueError, match="snapshot parse processes"):
        AzerothDoxygenSource(tmp_path / "missing", parse_processes=value)


def test_zero_and_maximum_process_counts_are_valid_options():
    assert snapshot_parse_processes(0) == 0
    assert snapshot_parse_processes(MAX_SNAPSHOT_PARSE_PROCESSES) == MAX_SNAPSHOT_PARSE_PROCESSES
