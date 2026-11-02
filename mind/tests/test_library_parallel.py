"""Bounded offline loading; synthetic corpus only, never live SQLite or models."""

from collections import Counter
from dataclasses import asdict
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest

from leeroy_mind.library import azeroth
from leeroy_mind.library.azeroth import AzerothDoxygenSource, AzerothWikiSource
from leeroy_mind.library.core import Library
from leeroy_mind.library.snapshots import MAX_SNAPSHOT_WORKERS, OfflineSnapshot, _bounded_map
from library_snapshot_helpers import rewrite, snapshot


class NoDatabase:
    def __getattribute__(self, name):
        raise AssertionError(f"source loader accessed library.{name}")


@pytest.mark.parametrize("kind,source_type", [("acwiki", AzerothWikiSource), ("acdoxygen", AzerothDoxygenSource)])
def test_one_and_eight_workers_produce_identical_ordered_results(tmp_path, kind, source_type):
    if kind == "acwiki":
        files = {"z.md": "# Last\n## Same\nRepeated.\n## Same\nRepeated.\n",
                 "a.md": "# First\n```sql\nSELECT id, name FROM game_tele;\n```\n",
                 "nested/unicode.md": "# 文档\n服务器参考。\n", "empty.md": " \n"}
    else:
        files = {"z.html": "<title>Last</title><h1>Last</h1><p>Repeated.</p><p>Repeated.</p>",
                 "a.html": "<h1>First</h1><pre>void Enter(Player* p);\nreturn;</pre>",
                 "nested/unicode.html": "<h1>文档</h1><table><tr><td>字段</td><td>说明</td></tr></table>",
                 "empty.html": "<script>hidden()</script>"}
    root = tmp_path / "docs"
    snapshot(root, kind, files, license="unknown")
    one = source_type(root, workers=1).load(NoDatabase())
    eight = source_type(root, workers=8).load(NoDatabase())
    assert asdict(eight) == asdict(one)
    assert [file.path for file in eight.provenance.files] == list(files)
    assert eight.provenance.files[-1].status == "empty"
    assert eight.provenance.files[-1].chunks == 0
    assert all(f.status == "imported" and f.chunks > 0 for f in eight.provenance.files[:-1])


def test_each_document_is_revalidated_and_hashed_in_both_passes(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    names = {f"page{i}.md": f"# Page {i}\nReference text.\n" for i in range(12)}
    snapshot(root, files=names)
    calls, paths, lock = Counter(), Counter(), threading.Lock()
    original_read, original_path = OfflineSnapshot.read, OfflineSnapshot.path

    def read(self, entry):
        with lock:
            calls[entry.path] += 1
        return original_read(self, entry)

    def path(self, relative):
        with lock:
            paths[relative] += 1
        return original_path(self, relative)

    monkeypatch.setattr(OfflineSnapshot, "read", read)
    monkeypatch.setattr(OfflineSnapshot, "path", path)
    AzerothWikiSource(root, workers=8).load(NoDatabase())
    assert calls == Counter({name: 2 for name in names})
    assert {name: paths[name] for name in names} == {name: 2 for name in names}


def test_bounded_window_limits_concurrent_work_and_input_prefetch():
    workers = 3
    lock = threading.Lock()
    release, started, window_full = threading.Event(), threading.Event(), threading.Event()
    state = {"active": 0, "peak": 0, "produced": 0}
    outputs, errors = [], []

    def values():
        for number in range(25):
            with lock:
                state["produced"] += 1
                if state["produced"] == workers * 2:
                    window_full.set()
            yield number

    def work(number):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            if state["active"] == workers:
                started.set()
        try:
            if not release.wait(5):
                raise AssertionError("test release was never signaled")
            return number * 2
        finally:
            with lock:
                state["active"] -= 1

    def consume():
        try:
            outputs.extend(_bounded_map(work, values(), workers=workers))
        except BaseException as exc:
            errors.append(exc)

    coordinator = threading.Thread(target=consume)
    coordinator.start()
    try:
        assert started.wait(5)
        assert window_full.wait(5)
        with lock:
            assert state["produced"] == workers * 2
            assert state["active"] == state["peak"] == workers
    finally:
        release.set()
        coordinator.join(5)
    assert not coordinator.is_alive()
    assert errors == []
    assert outputs == [number * 2 for number in range(25)]
    assert state["peak"] == workers and state["active"] == 0


def test_generator_close_joins_workers_without_consuming_whole_inventory():
    produced = []

    def values():
        for number in range(1000):
            produced.append(number)
            yield number

    results = _bounded_map(lambda number: number, values(), workers=2)
    assert next(results) == 0
    results.close()
    assert produced == list(range(4))


def test_worker_failure_is_propagated_and_cancels_the_remaining_window():
    produced = []

    def values():
        for number in range(1000):
            produced.append(number)
            yield number

    def work(number):
        if number == 1:
            raise ValueError("synthetic late verification failure")
        return number

    with pytest.raises(ValueError, match="verification failure"):
        list(_bounded_map(work, values(), workers=2))
    assert len(produced) <= 5


@pytest.mark.parametrize("workers", [1, 8])
def test_late_parse_failure_keeps_the_previous_complete_corpus(tmp_path, clock, workers):
    root = tmp_path / "docs"
    snapshot(root)
    library = Library(tmp_path / "library.db", clock=clock)
    try:
        library.build([AzerothWikiSource(root, workers=workers)])
        before = {table: [tuple(row) for row in library.conn.execute(f"SELECT * FROM {table}")]
                  for table in ("chunks", "source_snapshot", "source_inventory")}
        # The last page verifies against its manifest hash, then fails UTF-8 parsing.
        files = {f"page{i}.md": f"# Page {i}\nFresh text.\n" for i in range(12)}
        files["last.md"] = b"\xff\xfeinvalid utf8"
        snapshot(root, files=files)
        (root / "intro.md").unlink()
        with pytest.raises(UnicodeDecodeError):
            library.build([AzerothWikiSource(root, workers=workers)])
        after = {table: [tuple(row) for row in library.conn.execute(f"SELECT * FROM {table}")]
                 for table in before}
        assert after == before
    finally:
        library.close()


@pytest.mark.parametrize("workers", [1, 8])
def test_consumption_rechecks_content_changed_after_initial_verification(tmp_path, workers):
    root = tmp_path / "docs"
    snapshot(root, files={"first.md": "# First\nOriginal text.\n", "last.md": "# Last\nOriginal text.\n"})

    def progress(message):
        if ": parsing " in message and " files with " in message:
            (root / "last.md").write_text("# Last\nModified text.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed or is corrupt"):
        AzerothWikiSource(root, workers=workers, progress=progress).load(NoDatabase())


def test_all_manifest_paths_are_lexically_validated_before_worker_reads(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    manifest = snapshot(root, files={"first.md": "# First\nText.", "last.md": "# Last\nText."})
    manifest["files"][-1]["path"] = "../escape.md"
    rewrite(root, manifest)
    reads = []
    monkeypatch.setattr(OfflineSnapshot, "read", lambda self, entry: reads.append(entry.path))
    with pytest.raises(ValueError, match="unsafe snapshot path"):
        OfflineSnapshot(root, "acwiki", workers=8)
    assert reads == []


@pytest.mark.parametrize("component", ["directory", "file"])
@pytest.mark.parametrize("link_kind", ["symlink", "reparse"])
def test_every_read_rejects_new_symlink_or_windows_reparse_components(tmp_path, monkeypatch, component, link_kind):
    root = tmp_path / "docs"
    snapshot(root, files={"nested/page.md": "# Page\nReference.\n"})
    loaded = OfflineSnapshot(root, "acwiki", workers=1)
    target = root / "nested" if component == "directory" else root / "nested/page.md"
    original_lstat = Path.lstat

    def lstat(path):
        value = original_lstat(path)
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFLNK if link_kind == "symlink" else value.st_mode,
                                   st_file_attributes=0x400 if link_kind == "reparse" else 0)
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="linked snapshot path"):
        loaded.read(loaded.files[0])


def test_containment_is_not_cached_after_initial_verification(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    snapshot(root)
    loaded = OfflineSnapshot(root, "acwiki", workers=1)
    target, outside = root / "intro.md", tmp_path / "elsewhere.md"
    outside.write_text("Unrelated content.", encoding="utf-8")
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == target else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="contained file"):
        loaded.read(loaded.files[0])


def test_html_parsers_are_per_file_and_progress_stays_on_the_coordinator(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    files = {f"page{i}.html": f"<h1>Page {i}</h1><p>Text {i}.</p>" for i in range(18)}
    snapshot(root, "acdoxygen", files, license="unknown")
    parsers, messages, lock = [], [], threading.Lock()
    coordinator = threading.get_ident()
    original = azeroth._HTMLText

    class OwnedParser(original):
        def __init__(self):
            super().__init__()
            self.owner = threading.get_ident()
            with lock:
                parsers.append(self)

        def feed(self, data):
            assert self.owner == threading.get_ident()
            return super().feed(data)

    def progress(message):
        assert threading.get_ident() == coordinator
        messages.append(message)

    monkeypatch.setattr(azeroth, "_HTMLText", OwnedParser)
    result = AzerothDoxygenSource(root, workers=8, progress=progress).load(NoDatabase())
    assert len(parsers) == len(files)
    assert len({id(parser) for parser in parsers}) == len(files)
    assert all(parser.owner != coordinator for parser in parsers)
    assert len(result.chunks) == len(files)
    assert any("verified 18/18 files" in message for message in messages)
    assert any("parsed 18/18 files" in message for message in messages)
    assert any("verification complete" in message for message in messages)
    assert any("parsing complete" in message for message in messages)
    assert all("<h1>" not in message and "page0.html" not in message for message in messages)


@pytest.mark.parametrize("value", [0, -1, MAX_SNAPSHOT_WORKERS + 1, True, 3.5, "8"])
def test_worker_bounds_are_validated_before_filesystem_access(tmp_path, value):
    missing = tmp_path / "does-not-exist"
    for constructor in (AzerothWikiSource, AzerothDoxygenSource):
        with pytest.raises(ValueError, match="snapshot workers"):
            constructor(missing, workers=value)
    with pytest.raises(ValueError, match="snapshot workers"):
        OfflineSnapshot(missing, "acwiki", workers=value)
