"""The Library tab: a read-only search of library.db (BM25 in the console, opened per search in the
worker thread), and "Search deeper" through the mind's `library` op. A scratch library built with
the library's own schema from synthetic chunks; the mind link is a fake. No live data, no Ollama."""
import asyncio
import logging
import sqlite3
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from console.rig import FakeMind, http, make_app, running
from leeroy_mind import endpoint
from leeroy_mind.console import libraryview, mindlink
from leeroy_mind.console.control import Controls
from leeroy_mind.console.mindlink import LinkError
from leeroy_mind.library.chunks import KINDS, ChunkDraft
from leeroy_mind.library.core import Library
from leeroy_mind.library.serverref import ServerRefSource
from library_snapshot_helpers import command, server_snapshot, teleport
from test_library_core import ListSource

LONG = "\n".join(f"line {i:02d} of a long reference page about wands and staves" for i in range(40))
SECRET = "xyzzyplugh"


def chunks():
    return [
        ChunkDraft("cmd:learn", "command", ".learn", ".learn #spell\nTeaches the selected "
                   "player a spell."),
        ChunkDraft("item:1", "item", "Rune of Fate", "Rune of Fate: epic trinket. Drops from "
                   "the weapon vendor.", ref_ids=("item:1",)),
        ChunkDraft("doc:long", "documentation", "Long page", LONG, section="Wands",
                   url="https://example.invalid/doc", license="GPL-2.0"),
    ]


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "mind-data"
    d.mkdir()
    lib = Library(d / "library.db", clock=SimpleNamespace(now=lambda: 1.0))
    snap = d / "snapshot"
    server_snapshot(snap, commands=[command(name="setskill", syntax=".setskill #skill #level [#max]",
                                            help="Sets a weapon skill of the selected player.")],
                    teleports=[teleport(id=7, name="Hyjal")])
    lib.build([ListSource("world", chunks()), ServerRefSource(snap)])
    lib.close()
    return d


class FakeLink:
    def __init__(self, reply=None, *, error=None):
        self.calls, self.timeouts, self.reply, self.error = [], [], reply, error

    def locate(self, pids):
        return {"pid": 101, "port": 1, "key": "x"}

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append((tuple(pids), op, args))
        self.timeouts.append(timeout)
        if self.error:
            raise LinkError(self.error)
        return self.reply


async def get(data, path, **kw):
    app = make_app(data, **kw)
    await app.start()
    try:
        return await http(app, "GET", path)
    finally:
        await app.close()


def ask(data, q, kind=None, **kw):
    path = "/api/library?q=" + quote(q) + (f"&kind={kind}" if kind else "")
    r = asyncio.run(get(data, path, **kw))
    return r.status, r.json()


def test_a_search_finds_the_command(data):
    status, v = ask(data, "weapon skill")
    assert status == 200 and v["setup"] is True and v["why"] is None
    first = v["hits"][0]
    assert first["kind"] == "command" and ".setskill" in first["text"]
    assert set(first) == {"id", "kind", "title", "section", "text", "source", "cite", "score"}
    assert first["title"] == ".setskill"
    assert first["source"] == "serverref" and first["cite"]
    assert len(v["hits"]) <= libraryview.K


def test_without_a_question_the_answer_is_the_stats_only(data):
    r = asyncio.run(get(data, "/api/library"))
    v = r.json()
    assert r.status == 200 and v["setup"] is True and v["hits"] == []
    assert v["stats"]["total"] == 5 and v["stats"]["kinds"]["command"] == 2
    assert set(v["stats"]["kinds"]) <= set(KINDS)


def test_exact_commands_and_teleports_come_first_without_a_kind(data):
    _, v = ask(data, "setskill")
    assert v["hits"][0]["title"] == ".setskill" and v["hits"][0]["kind"] == "command"
    _, v = ask(data, "wands staves lines .setskill")  # BM25 alone would put the long page first
    assert v["hits"][0]["title"] == ".setskill"
    _, v = ask(data, "Hyjal")
    assert v["hits"][0]["kind"] == "teleport" and v["hits"][0]["title"] == "Hyjal"
    assert len({h["id"] for h in v["hits"]}) == len(v["hits"])
    _, v = ask(data, "Hyjal", kind="item")  # a chosen kind is only that kind
    assert all(h["kind"] == "item" for h in v["hits"])


def test_stats_are_computed_only_without_a_question(data, monkeypatch):
    calls = []
    real = Library.stats
    monkeypatch.setattr(Library, "stats", lambda self: calls.append(1) or real(self))
    _, v = ask(data, "weapon skill")
    assert v["stats"] is None and calls == []
    r = asyncio.run(get(data, "/api/library"))
    assert r.json()["stats"]["total"] == 5 and calls == [1]


def test_the_kind_filter(data):
    _, v = ask(data, "Rune Fate", kind="item")
    assert [h["kind"] for h in v["hits"]] == ["item"]
    _, v = ask(data, "Rune Fate", kind="command")
    assert v["hits"] == []


def test_the_text_is_cut_at_the_last_line_break(data):
    _, v = ask(data, "wands staves")
    text = v["hits"][0]["text"]
    assert len(text) <= libraryview.TEXT_MAX and LONG.startswith(text)
    assert LONG[len(text)] == "\n" and not text.endswith("\n")
    assert libraryview.cut("short") == "short"
    assert libraryview.cut("x" * 3000) == "x" * libraryview.TEXT_MAX


def test_punctuation_never_breaks_a_search(data):
    for q in ('"', "*", "NEAR(a b)", "a -b", "((", "\U0001F389", "a" * 200):
        status, v = ask(data, q)
        assert status == 200 and isinstance(v["hits"], list), q


def test_a_query_the_index_rejects_is_retried_with_its_words(data, monkeypatch):
    seen = []
    real = Library.search_text

    def picky(self, query, k=5, kinds=None):
        seen.append(query)
        if any(c in query for c in '"*('):
            raise sqlite3.OperationalError("fts5: syntax error")
        return real(self, query, k, kinds)
    monkeypatch.setattr(Library, "search_text", picky)
    _, v = ask(data, '"weapon" *skill* ((')
    assert seen == ['"weapon" *skill* ((', "weapon skill"]
    assert v["hits"] and v["hits"][0]["kind"] == "command"
    seen.clear()
    _, v = ask(data, "\U0001F389 ((")
    assert v["hits"] == [] and seen == ["\U0001F389 (("]


def test_a_bad_question_is_400(data):
    for path in ("/api/library?q=", "/api/library?q=%20%20", "/api/library?q=" + "a" * 201,
                 "/api/library?q=a&q=b", "/api/library?q=a&kind=nope",
                 "/api/library?q=a&kind=item&kind=lore", "/api/library?kind=item",
                 "/api/library?q=a&deeper=2", "/api/library?q=a&deeper=1&deeper=1",
                 "/api/library?deeper=1"):
        r = asyncio.run(get(data, path))
        assert r.status == 400 and r.json()["ok"] is False, path


def test_whitespace_collapses_before_the_length_rule(data):
    status, _ = ask(data, "weapon" + " " * 300 + "skill")
    assert status == 200


def test_no_library_is_not_set_up(data):
    (data / "library.db").unlink()
    status, v = ask(data, "weapon skill")
    assert status == 200 and v["setup"] is False and v["hits"] == [] and v["stats"] is None
    assert v["why"] == "there is no library.db yet: build it with the library CLI while the mind is stopped."


def test_another_schema_is_not_set_up(data):
    c = sqlite3.connect(data / "library.db")
    c.execute("PRAGMA user_version = 999")
    c.commit()
    c.close()
    status, v = ask(data, "weapon skill")
    assert status == 200 and v["setup"] is False and v["hits"] == []
    assert v["why"] == "library.db is from another version: migrate it with the library CLI."


def test_a_library_that_cannot_be_read_is_503_naming_the_class_only(data):
    (data / "library.db").write_bytes(b"this is not a database" * 100)
    r = asyncio.run(get(data, "/api/library?q=weapon"))
    assert r.status == 503 and r.json()["ok"] is False
    assert "weapon" not in r.json()["error"]


def hit(**over):
    h = {"id": 7, "kind": "command", "title": ".tele", "section": "", "text": "teleports",
         "source": "serverref", "cite": "installed server reference", "score": 0.5}
    return {**h, **over}


def test_deeper_asks_the_mind_and_says_when_it_cannot(data):
    link = FakeLink({"ok": True, "hits": [hit(), hit(id=8, kind="item"), {"junk": 1}]})
    r = asyncio.run(get(data, "/api/library?q=go%20to%20%20places&deeper=1", mind=running(data), link=link))
    v = r.json()
    assert r.status == 200 and v["setup"] is True and v["deeper"] is True and v["deeper_why"] is None
    assert [h["id"] for h in v["hits"]] == [7, 8]
    assert link.calls == [((101, 100), "library", {"query": "go to places", "k": libraryview.K})]
    assert link.timeouts == [mindlink.LIBRARY_S]
    assert mindlink.LIBRARY_S == endpoint.LIBRARY_READ_S + 5.0
    # the kind filter applies to what the mind sent
    link = FakeLink({"ok": True, "hits": [hit(), hit(id=8, kind="item")]})
    r = asyncio.run(get(data, "/api/library?q=x&kind=item&deeper=1", mind=running(data), link=link))
    assert [h["id"] for h in r.json()["hits"]] == [8]
    # the mind is down: in words, and nothing is called
    link = FakeLink({"ok": True, "hits": []})
    r = asyncio.run(get(data, "/api/library?q=x&deeper=1", link=link))
    v = r.json()
    assert r.status == 200 and v["hits"] == [] and link.calls == []
    assert v["deeper_why"] == "Deeper search is done by the mind, which isn't running."
    # the mind's state is unclear
    link = FakeLink({"ok": True, "hits": []})
    r = asyncio.run(get(data, "/api/library?q=x&deeper=1", mind=FakeMind(data, error="no list"), link=link))
    assert r.json()["deeper_why"] == "Deeper search is done by the mind, whose state is unknown."
    # busy or refused: the mind's words
    link = FakeLink({"ok": False, "error": "Another library search is running."})
    r = asyncio.run(get(data, "/api/library?q=x&deeper=1", mind=running(data), link=link))
    assert r.json()["deeper_why"] == "Another library search is running." and r.json()["hits"] == []
    # unreachable
    link = FakeLink(error="the control endpoint did not answer (TimeoutError)")
    r = asyncio.run(get(data, "/api/library?q=x&deeper=1", mind=running(data), link=link))
    assert r.json()["deeper_why"].startswith("The control endpoint did not answer")
    # a message that already ends in a period gets no second one
    link = FakeLink(error="the mind said so.")
    r = asyncio.run(get(data, "/api/library?q=x&deeper=1", mind=running(data), link=link))
    assert r.json()["deeper_why"] == "The mind said so."
    # an older mind without the op
    link = FakeLink({"ok": False, "error": "bad request: op is one of status, stop"})
    r = asyncio.run(get(data, "/api/library?q=x&deeper=1", mind=running(data), link=link))
    assert "restart" in r.json()["deeper_why"]
    # no link at all
    r = asyncio.run(get(data, "/api/library?q=x&deeper=1", mind=running(data)))
    assert r.status == 200 and r.json()["deeper_why"]


def test_deeper_works_with_control_off(data):
    link = FakeLink({"ok": True, "hits": [hit()]})
    controls = Controls(enabled=False, mind=running(data), voice=SimpleNamespace(act=lambda a: None))
    r = asyncio.run(get(data, "/api/library?q=x&deeper=1", mind=running(data), link=link,
                        controls=controls))
    assert [h["id"] for h in r.json()["hits"]] == [7]


def test_the_deeper_query_never_reaches_the_log(data, caplog):
    link = FakeLink({"ok": True, "hits": [hit()]})

    async def go():
        app = make_app(data, mind=running(data), link=link)
        await app.start()
        try:
            await http(app, "GET", f"/api/library?q={SECRET}&deeper=1")
            await http(app, "GET", f"/api/library?q={SECRET}")
            await http(app, "GET", f"/api/library?q={SECRET}&kind=nope")
        finally:
            await app.close()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())
    assert SECRET not in caplog.text

