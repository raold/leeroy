"""Synthetic readers only. Blocking gates are always released before owned teardown."""
import asyncio
import sqlite3
import threading

import pytest

from conftest import run
from library_snapshot_helpers import snapshot
from leeroy_mind.library.azeroth import AzerothWikiSource
from leeroy_mind.library.chunks import ChunkDraft, SourceResult
from leeroy_mind.library.core import Library
from leeroy_mind.library.search_worker import LibrarySearchWorker, SearchBusy, SearchClosed
from leeroy_mind.talk.facts import LibraryFacts
from leeroy_mind.talk.questions import game_question
from leeroy_mind.testing.fake_llm import FakeLLM


class Source:
    name = "wiki"

    def load(self, library):
        return SourceResult(self.name, [
            ChunkDraft("wiki:1:0", "lore", "Sindragosa", "Sindragosa was raised as a frost "
                       "wyrm by the Lich King. She guards his citadel.", license="CC BY-SA 4.0"),
            ChunkDraft("wiki:2:0", "lore", "The Frost Queen", "Sindragosa guards Icecrown "
                       "Citadel as a frost wyrm.", license="CC BY-SA 4.0"),
            ChunkDraft("wiki:3:0", "zone", "Icecrown Citadel", "Icecrown Citadel is the "
                       "fortress of the Lich King in Icecrown.", license="CC BY-SA 4.0")])


@pytest.fixture
def library_path(tmp_path, clock):
    root = tmp_path / "docs"
    snapshot(root, files={
        "settings.md": "# Settings\nPlayerLimit controls the player limit.\n",
        "installation.md": "# Core Installation\nConfigure the build with CMake before compiling.\n"})
    path = tmp_path / "library.db"
    library = Library(path, clock=clock)
    try:
        library.build([Source(), AzerothWikiSource(root)])
        run(library.embed_pending(llm=FakeLLM()))
    finally:
        library.close()
    return path


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 3)


@pytest.mark.parametrize("mode", ["text", "vector", "mismatch", "failure", "malformed"])
def test_results_match_direct_library(library_path, clock, mode):
    class Malformed:
        async def embed(self, *args, **kwargs):
            return [[1.0]]

    def embedder():
        if mode == "text":
            return None
        if mode == "malformed":
            return Malformed()
        llm = FakeLLM()
        if mode == "failure":
            llm.fail_next(100)
        return llm

    async def main():
        model = "different-model" if mode == "mismatch" else "qwen3-embedding:0.6b"
        direct = Library(library_path, clock=clock, readonly=True, embed_model=model)
        worker = LibrarySearchWorker(library_path, embed_model=model)
        try:
            for query, k, kinds in [("Sindragosa frost wyrm", 5, None), ("", 5, None),
                                    ("Sindragosa", 0, ["lore"]), ("Sindragosa", -1, "lore"),
                                    ("PlayerLimit", 5, ["documentation"]),
                                    ("Sindragosa", 1, ["lore"]), ("unknown", 5, [])]:
                assert await worker.search(query, k, kinds, llm=embedder()) == await direct.search(
                    query, k, kinds, llm=embedder())
                assert await worker.search_text(query, k, kinds) == direct.search_text(query, k, kinds)
            assert await worker.titled(["Sindragosa"], ["lore"]) == direct.titled(
                ["Sindragosa"], ["lore"])
            assert await worker.reference_matches("PlayerLimit", ["documentation"]) == (
                direct.reference_matches("PlayerLimit", ["documentation"]))
            for query in ("PlayerLimit", "UnknownSetting", "CMake build"):
                assert await worker.search_reference_text(query, kinds=["documentation"],
                                                          language="en") == (
                    direct.search_reference_text(query, kinds=["documentation"], language="en"))
            with pytest.raises(ValueError, match="unknown chunk kinds"):
                await worker.search("", kinds=["unknown"])
        finally:
            await worker.aclose()
            direct.close()
    run(main())


def test_blocking_reader_allows_loop_progress_and_owns_open_read_close(library_path, monkeypatch):
    async def main():
        loop = asyncio.get_running_loop()
        entered, progressed = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        owners = []
        original_init, original_search, original_close = Library.__init__, Library.search_text, Library.close

        def opened(self, *args, **kwargs):
            owners.append(("open", threading.get_ident()))
            original_init(self, *args, **kwargs)
            assert self.readonly and self.llm is None

        def blocked(self, *args, **kwargs):
            owners.append(("read", threading.get_ident()))
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release reader"
            return original_search(self, *args, **kwargs)

        def closed(self):
            owners.append(("close", threading.get_ident()))
            original_close(self)

        monkeypatch.setattr(Library, "__init__", opened)
        monkeypatch.setattr(Library, "search_text", blocked)
        monkeypatch.setattr(Library, "close", closed)
        worker = LibrarySearchWorker(library_path)
        assert worker._thread is None
        search = asyncio.create_task(worker.search_text("Sindragosa"))
        try:
            await asyncio.wait_for(entered.wait(), 3)

            async def other_loop_work():
                await asyncio.sleep(0)
                progressed.set()
            await asyncio.wait_for(other_loop_work(), 0.5)
            assert progressed.is_set() and not search.done()
            release.set()
            assert await search
        finally:
            release.set()
            await asyncio.gather(search, return_exceptions=True)
            await worker.aclose()
        assert [name for name, _ in owners] == ["open", "read", "close"]
        assert len({owner for _, owner in owners}) == 1
        assert owners[0][1] != threading.get_ident()
        assert not worker._thread.is_alive()
    run(main())


def test_cancelled_active_and_queued_jobs_keep_capacity_until_discarded(library_path, monkeypatch):
    async def main():
        loop, entered, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        original = Library.search_text
        calls = []

        def blocked(self, query, *args, **kwargs):
            calls.append(query)
            if query == "first":
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
            return original(self, query, *args, **kwargs)

        monkeypatch.setattr(Library, "search_text", blocked)
        worker = LibrarySearchWorker(library_path, max_pending=2)
        first = asyncio.create_task(worker.search_text("first"))
        queued = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            queued = asyncio.create_task(worker.search_text("cancelled queue"))
            await asyncio.sleep(0)
            with pytest.raises(SearchBusy):
                await worker.search_text("full")
            facts = LibraryFacts(object(), None, search=worker)
            q = game_question("Who is Sindragosa?")
            assert q is not None
            facts.start(9, q)
            assert await facts.for_line(9) == []  # overload cannot fall back on the main reader
            await facts.reset_session()
            first.cancel()
            queued.cancel()
            await asyncio.gather(first, queued, return_exceptions=True)
            assert len(worker._requests) == 2
            with pytest.raises(SearchBusy):
                await worker.search_text("still full")
            release.set()
            await until(lambda: not worker._requests)
            assert await worker.search_text("Sindragosa")
            assert calls == ["first", "Sindragosa"]
        finally:
            release.set()
            await asyncio.gather(*(t for t in (first, queued) if t), return_exceptions=True)
            await worker.aclose()
    run(main())


def test_embedding_keeps_logical_admission_and_cancellation_skips_rank(library_path, monkeypatch):
    async def main():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        worker = LibrarySearchWorker(library_path, max_pending=1)
        ranked = []
        original = Library._finish_search

        def finish(self, *args):
            ranked.append(True)
            return original(self, *args)

        monkeypatch.setattr(Library, "_finish_search", finish)
        task = asyncio.create_task(worker.search("Sindragosa", llm=llm))
        try:
            await until(lambda: llm.inflight == 1)
            with pytest.raises(SearchBusy):
                await worker.search_text("Sindragosa")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not worker._requests and not ranked and llm.inflight == 0
            assert await worker.search_text("Sindragosa")
        finally:
            gate.set()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()
    run(main())


def test_close_is_shared_shielded_and_joins_running_work(library_path, monkeypatch):
    async def main():
        loop, entered, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        closes = []
        original_close = Library.close

        def blocked(self, *args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return []

        def close(self):
            closes.append(threading.get_ident())
            original_close(self)

        monkeypatch.setattr(Library, "search_text", blocked)
        monkeypatch.setattr(Library, "close", close)
        worker = LibrarySearchWorker(library_path, max_pending=1)
        search = asyncio.create_task(worker.search_text("blocked"))
        closers = []
        try:
            await asyncio.wait_for(entered.wait(), 3)
            closers = [asyncio.create_task(worker.aclose()) for _ in range(2)]
            await asyncio.sleep(0)
            closers[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await closers[0]
            assert not closers[1].done() and not closes
            with pytest.raises(SearchClosed):
                await worker.search_text("late")
            with pytest.raises(SearchClosed):
                await search
            release.set()
            await closers[1]
            await worker.aclose()
            assert len(closes) == 1 and closes[0] != threading.get_ident()
            assert not worker._thread.is_alive()
        finally:
            release.set()
            await asyncio.gather(search, *closers, return_exceptions=True)
            await worker.aclose()
    run(main())


@pytest.mark.parametrize("kind", ["missing", "garbage", "schema"])
def test_failed_open_never_creates_or_migrates_and_does_not_retry(tmp_path, kind):
    path = tmp_path / "unavailable.db"
    if kind == "garbage":
        path.write_bytes(b"not a database")
    elif kind == "schema":
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA user_version=9999")
    before = path.read_bytes() if path.exists() else None

    async def main():
        worker = LibrarySearchWorker(path)
        try:
            with pytest.raises((sqlite3.Error, RuntimeError)):
                await asyncio.wait_for(worker.search_text("anything"), 3)
            await until(lambda: worker._closed)
            with pytest.raises(SearchClosed):
                await worker.search_text("again")
        finally:
            await worker.aclose()
        assert not worker._thread.is_alive()
    run(main())
    assert (path.read_bytes() if path.exists() else None) == before


def test_close_before_first_use_does_not_open_a_reader(tmp_path):
    async def main():
        worker = LibrarySearchWorker(tmp_path / "missing.db")
        await worker.aclose()
        await worker.aclose()
        assert worker._thread is None
        with pytest.raises(SearchClosed):
            await worker.search_text("anything")
    run(main())


def test_cancelled_sql_is_interrupted_and_reader_is_reusable(library_path):
    async def main():
        loop, entered = asyncio.get_running_loop(), asyncio.Event()
        worker = LibrarySearchWorker(library_path, max_pending=1)

        def long_query(reader):
            def began():
                loop.call_soon_threadsafe(entered.set)
                return 0
            reader.conn.create_function("began", 0, began)
            return reader._all("WITH RECURSIVE n(x) AS (SELECT began() UNION ALL "
                               "SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n")

        task = asyncio.create_task(worker._call(long_query))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await until(lambda: not worker._requests)
            assert await worker.search_text("Sindragosa")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()
    run(main())


def test_close_during_open_joins_reader_on_its_thread(library_path, monkeypatch):
    async def main():
        loop, entered, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        original_init, original_close = Library.__init__, Library.close
        owners = []

        def opened(self, *args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            original_init(self, *args, **kwargs)
            owners.append(threading.get_ident())

        def closed(self):
            owners.append(threading.get_ident())
            original_close(self)

        monkeypatch.setattr(Library, "__init__", opened)
        monkeypatch.setattr(Library, "close", closed)
        worker = LibrarySearchWorker(library_path)
        task = asyncio.create_task(worker.search_text("Sindragosa"))
        closer = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            closer = asyncio.create_task(worker.aclose())
            await asyncio.sleep(0)
            with pytest.raises(SearchClosed):
                await task
            assert not closer.done()
            release.set()
            await closer
            assert len(owners) == 2 and len(set(owners)) == 1
            assert owners[0] != threading.get_ident()
        finally:
            release.set()
            await asyncio.gather(*(t for t in (task, closer) if t), return_exceptions=True)
            await worker.aclose()
    run(main())


def test_close_waits_for_embedding_cancellation_cleanup(library_path):
    async def main():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Embedder:
            async def embed(self, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.set()
                    await release.wait()

        worker = LibrarySearchWorker(library_path)
        task = asyncio.create_task(worker.search("Sindragosa", llm=Embedder()))
        closer = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            closer = asyncio.create_task(worker.aclose())
            await asyncio.wait_for(cleanup.wait(), 3)
            assert not closer.done()
            release.set()
            await closer
            with pytest.raises(SearchClosed):
                await task
        finally:
            release.set()
            await asyncio.gather(*(t for t in (task, closer) if t), return_exceptions=True)
            await worker.aclose()
    run(main())


def test_clear_drains_old_lookup_and_reusing_line_id_cannot_publish_old_facts(library_path,
                                                                            clock, monkeypatch):
    async def main():
        loop, entered, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        original = Library.search_reference_text
        direct = Library(library_path, clock=clock, readonly=True)
        worker = LibrarySearchWorker(library_path)

        def blocked(self, query, *args, **kwargs):
            if "PlayerLimit" in query:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
            return original(self, query, *args, **kwargs)

        monkeypatch.setattr(Library, "search_reference_text", blocked)
        facts = LibraryFacts(direct, None, search=worker)
        old = game_question("What does PlayerLimit mean in AzerothCore?")
        new = game_question("What does UnknownSetting mean in AzerothCore?")
        assert old is not None and new is not None
        facts.start(7, old)
        old_reply = asyncio.create_task(facts.for_line(7))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await facts.reset_session()  # does not block on the abandoned physical job
            assert not facts._tasks and await old_reply == []
            facts.start(7, new)
            new_task = facts._lines[7]
            release.set()
            assert await facts.for_line(7) == []
            assert facts._lines[7] is new_task
            assert await facts.for_line(7) == []
        finally:
            release.set()
            await facts.reset_session()
            await asyncio.gather(old_reply, return_exceptions=True)
            await worker.aclose()
            direct.close()
    run(main())


@pytest.mark.parametrize("route", ["reference", "fallback"])
def test_facts_reference_and_text_fallback_wait_off_loop(library_path, clock, monkeypatch, route):
    async def main():
        loop, entered, release = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
        direct = Library(library_path, clock=clock, readonly=True)
        worker = LibrarySearchWorker(library_path)
        operation = "search_reference_text" if route == "reference" else "search_text"
        original = getattr(Library, operation)
        formatted = []

        def blocked(self, *args, **kwargs):
            assert self is not direct
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return original(self, *args, **kwargs)

        def failed_prepare(self, *args, **kwargs):
            raise RuntimeError("synthetic hybrid failure")

        def forbidden(*args, **kwargs):
            raise AssertionError("production facts must not query the main reader")

        from leeroy_mind.talk import facts as facts_module
        formatter_name = "_reference_fact_lines" if route == "reference" else "_lore_fact_lines"
        original_formatter = getattr(facts_module, formatter_name)

        def format_facts(*args, **kwargs):
            formatted.append(threading.get_ident())
            return original_formatter(*args, **kwargs)

        monkeypatch.setattr(Library, operation, blocked)
        monkeypatch.setattr(facts_module, formatter_name, format_facts)
        if route == "fallback":
            monkeypatch.setattr(Library, "_prepare_search", failed_prepare)
        for method in ("search", "search_text", "search_reference_text", "reference_matches", "titled"):
            monkeypatch.setattr(direct, method, forbidden)
        factions = []

        def faction():
            factions.append(threading.get_ident())
            return None

        facts = LibraryFacts(direct, None, search=worker, faction=faction, cap_s=3)
        q = game_question("What does PlayerLimit mean in AzerothCore?" if route == "reference"
                          else "Who is Sindragosa?")
        assert q is not None
        facts.start(1, q)
        reply = asyncio.create_task(facts.for_line(1))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            progressed = []
            loop.call_soon(progressed.append, True)
            await asyncio.sleep(0)
            assert progressed and not reply.done()
            release.set()
            assert await reply
            assert formatted and all(t != threading.get_ident() for t in formatted)
            assert factions == ([] if route == "reference" else [threading.get_ident()])
        finally:
            release.set()
            await facts.reset_session()
            await asyncio.gather(reply, return_exceptions=True)
            await worker.aclose()
            direct.close()
    run(main())
