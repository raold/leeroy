"""The mind service (Task 17): bridge events in, memory and talk out, logs, shutdown, the CLI.

The app runs against FakeBridge (real sockets on 127.0.0.1) and a FakeLLM. The clock is a
FakeClock that yields instead of sleeping, so fake minutes pass in milliseconds; the sockets
get real time from wait(), until() and settle()."""
import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import HUMAN, run, seed_test_cast
from leeroy_mind.app import (GUILD_ID, AlreadyRunning, MindApp, acquire_instance_lock,
                             check_ollama_chat, instance_lock_held, startup_checks)
from leeroy_mind.bridge import BridgeClient
from leeroy_mind.config import load_config
from leeroy_mind.library.chunks import ChunkDraft, SourceResult
from leeroy_mind.library.core import Library
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.logs import log, setup_logging
from leeroy_mind.memory.events import EventMemoryRules
from leeroy_mind.memory.threads import ClosedThread
from leeroy_mind.memory.writer import Embedder, MemoryCandidate, MemoryWriter
from leeroy_mind.personas.io import PersonaCache, save_persona
from leeroy_mind.protocol import Cast, Chat, Hello, Member, register_event
from leeroy_mind.storage.db import EmbeddingMismatch, open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.engine import TalkEngine
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.talk.world import WorldView
from leeroy_mind.testing.fake_bridge import FakeBridge, load_fixture
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, RACES

FIXTURE = Path(__file__).parent / "fixtures" / "session_basic.jsonl"
START = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
NICK, REAL = "Zan", "Quillon"  # invented: the owner's names live only in his local mind.toml
ORION = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
SCHEMA_MARK = "Reply with only a JSON object matching this schema"  # chat_json's suffix (C7)
LINE = re.compile(r"^\[L(\d+)\] ([^:]+): ", re.MULTILINE)
HUMAN_ROW = Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0, level=80, rank=0,
                   online=False, map=0, zone=0, spec_tab=-1)


@dataclasses.dataclass(frozen=True, slots=True)
class TestJob:  # an extension's event type (C21: registry tests use test_* names)
    __test__ = False
    ts: int
    guid: int
    kind: str


register_event("test_app_job", TestJob)


@pytest.fixture
def clock():
    return FakeClock(START, yields=64)


def hello(human_guid: int = 0, guild_id: int = GUILD_ID) -> dict:
    return {"type": "hello", "server": "mod-leeroy", "version": "0.1.0", "guild_id": guild_id,
            "human_guid": human_guid}


CAST = load_fixture(FIXTURE)[1]  # 101-105, online, zone 1519


def chat(text: str, *, channel: str = "guild", from_guid: int = HUMAN,
         from_name: str = "Testmage", to_guid: int = 0, **extra) -> dict:
    return {"type": "chat", "channel": channel, "from_guid": from_guid, "from_name": from_name,
            "to_guid": to_guid, "text": text, "map": 0, "zone": 1519, **extra}


def presence(kind: str, guid: int = HUMAN, human: bool = True, **extra) -> dict:
    return {"type": kind, "guid": guid, "human": human, **extra}


def toml(port: int, *, chattiness: int = 4) -> str:
    return (f"[talk]\nchattiness = {chattiness}\n\n[bridge]\nport = {port}\n\n"
            f'[human]\nnickname = "{NICK}"\nreal_name = "{REAL}"\npronouns = "he"\n')


def extraction_json(messages: list[dict]) -> str:
    """A valid extraction for the first line a member said, citing that line."""
    for line_id, name in LINE.findall(messages[-1]["content"]):
        if name != NICK:
            return json.dumps({"memories": [{
                "speaker": name, "text": f"I said evening to {NICK} after his rough day.",
                "what": "event", "disagrees": False, "importance": 4, "valence": 0.2,
                "about": [NICK], "evidence": [f"L{line_id}"]}], "overheard": None})
    return json.dumps({"memories": [], "overheard": None})


def talk_and_extract(model: str, messages: list[dict]) -> str:
    if SCHEMA_MARK in messages[-1]["content"]:
        return extraction_json(messages)
    return "evening, rough one?"


def seed_db(path: Path, clock) -> None:
    s = Store(open_db(path), clock=clock)
    try:
        seed_test_cast(s)
        s.upsert_cast([HUMAN_ROW], 1000.0)
    finally:
        s.close()


class Harness:
    def __init__(self, app: MindApp, bridge: FakeBridge, llm: FakeLLM, clock: FakeClock,
                 task: asyncio.Task):
        self.app, self.bridge, self.llm, self.clock, self.task = app, bridge, llm, clock, task

    async def wait(self, pred, timeout: float = 3.0) -> None:
        """Real time only: the clock stands still while the sockets catch up."""
        async def poll():
            while not pred():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), timeout)

    async def until(self, pred, timeout: float = 5.0, step: float = 0.5) -> None:
        """Advance the clock a step at a time (reconnect backoff runs on it) until pred()."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not pred():
            assert loop.time() < end, "the condition never held"
            await self.clock.advance(step, step)
            await asyncio.sleep(0.005)

    async def settle(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0.01)

    async def advance(self, seconds: float, step: float = 0.5) -> None:
        await self.clock.advance(seconds, step)
        await self.settle()

    def says(self) -> list[dict]:
        return [c for c in self.bridge.commands if c["type"] == "say"]

    def rows(self, sql: str, params=()) -> list:
        conn = self.app.store.conn
        out = conn.execute(sql, params).fetchall()
        conn.commit()
        return out

    def count(self, table: str, where: str = "", params=()) -> int:
        return self.rows(f"SELECT count(*) FROM {table} {where}", params)[0][0]

    def memories(self) -> list[tuple[int, str, list[str]]]:
        return [(r["owner"], r["text"], json.loads(r["evidence"]))
                for r in self.rows("SELECT owner, text, evidence FROM memory ORDER BY id")]


@contextlib.asynccontextmanager
async def running(tmp_path: Path, clock, *, events, script=talk_and_extract, llm=None,
                  chattiness: int = 4, seed: int = 1, prepare=None, bridge_kw=None):
    bridge = FakeBridge(events, **(bridge_kw or {}))
    port = await bridge.start()
    (tmp_path / "mind.toml").write_text(toml(port, chattiness=chattiness), encoding="utf-8")
    seed_db(tmp_path / "mind.db", clock)
    llm = llm or FakeLLM(script, models={ORION})  # the live model is there: not silent
    app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=llm, clock=clock,
                  rng_seed=seed)
    if prepare is not None:
        prepare(app)
    task = asyncio.create_task(app.run())
    try:
        yield Harness(app, bridge, llm, clock, task)
    finally:
        await app.stop()
        done, _ = await asyncio.wait({task}, timeout=5.0)
        await bridge.stop()
        crashed = task.exception() if task in done and not task.cancelled() else None
    assert task in done and crashed is None, crashed  # run() ended cleanly


def reopen(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{(tmp_path / 'mind.db').as_uri()}?mode=ro", uri=True)


# -- the brief's end-to-end session ------------------------------------------------------------

def test_e2e_session_with_fake_bridge(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=load_fixture(FIXTURE)) as h:
            await h.wait(lambda: h.count("chat_line") >= 1)
            await h.advance(120)
            says = h.says()
            # 2-3 login greetings + 2-3 replies to "evening ...", minus guard and repeat drops
            assert 2 <= len(says) <= 6 and all(c["channel"] == "guild" for c in says)
            assert h.count("chat_line") == 1 + len(says)  # the echoes are never stored
            assert h.count("chat_line", "WHERE origin = 'mind' AND thread_id IS NOT NULL") \
                == len(says)  # the mind's own lines reach the thread (on_line_sent)
            assert h.app.store.open_session() is not None  # ruling 20: before stop()
            await h.advance(100)  # the thread falls silent for 90 s or more -> extracted
            assert any("chat:" in e for _, _, ev in h.memories() for e in ev)
            assert all(REAL not in json.dumps(c.get("messages", ""))
                       for c in h.llm.calls)  # nobody here is close: his real name never sent
            await h.app.stop()
        wal = tmp_path / "mind.db-wal"
        assert not wal.exists() or wal.stat().st_size == 0
        with contextlib.closing(reopen(tmp_path)) as c:
            assert c.execute("SELECT count(*) FROM session WHERE ended IS NULL").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM session").fetchone()[0] == 1
    run(main())


def test_cast_game_chat_is_logged_not_threaded(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
            await h.bridge.push(chat("ok following", channel="party", from_guid=102,
                                     from_name="Belra"))
            await h.wait(lambda: h.count("chat_line") == 1)
            await h.advance(60)
            row = h.rows("SELECT origin, thread_id FROM chat_line")[0]
            assert (row["origin"], row["thread_id"]) == ("game", None)
            assert h.says() == []
            # positive control: the human's own line is threaded
            await h.bridge.push(chat("anyone around?", channel="party"))
            await h.wait(lambda: h.count("chat_line") == 2)
            row = h.rows("SELECT origin, thread_id FROM chat_line WHERE id = 2")[0]
            assert row["origin"] == "human" and row["thread_id"] is not None
    run(main())


def test_the_stored_cast_follows_each_cast_snapshot(tmp_path, clock, caplog):
    """Fix wave G6: the owner kicked a member, and the bridge's next cast left him out, but
    the stored roster (generation, seeding and doctor read it) kept him. Each cast is the
    whole cast now: who is missing from it leaves the roster, persona and relationships kept.
    His own row stays, before the bridge has named him too (the roster's guild master), and an
    empty cast (a bridge that hasn't loaded the guild) changes nothing."""
    without_ebbo = {"type": "cast", "members": [m for m in CAST["members"] if m["guid"] != 105]}

    async def main():
        async with running(tmp_path, clock, events=[hello(0), CAST]) as h:
            store = h.app.store
            await h.wait(lambda: h.app.world.online)
            assert set(store.cast()) == {HUMAN, 101, 102, 103, 104, 105}
            store.put_relationship(dataclasses.replace(store.get_relationship(105, HUMAN),
                                                       affinity=30.0, interactions=4))
            await h.bridge.push(without_ebbo)
            await h.wait(lambda: 105 not in store.cast())
            assert set(store.cast()) == {HUMAN, 101, 102, 103, 104}
            assert store.get_persona_json(105) is not None
            assert store.get_relationship(105, HUMAN).affinity == 30.0
            assert store.get_meta("human_guid") is None  # not named yet: his row stayed anyway
            await h.bridge.push({"type": "cast", "members": []})
            await h.wait(lambda: not h.app.world.members)
            assert set(store.cast()) == {HUMAN, 101, 102, 103, 104}
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    assert "cast_left: guids=[105]" in caplog.text


def test_human_bot_commands_are_stored_not_threaded(tmp_path, clock):
    async def main():
        group = {"type": "group", "leader_guid": HUMAN, "raid": False,
                 "members": [HUMAN, 101, 102]}
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST, group]) as h:
            await h.wait(lambda: h.app.world.group is not None)
            await h.bridge.push(chat("co +wait for attack", channel="party"))
            await h.bridge.push(chat("follow", channel="party"))
            await h.wait(lambda: h.count("chat_line") == 2)
            await h.advance(30)
            rows = h.rows("SELECT origin, thread_id FROM chat_line")
            assert [(r["origin"], r["thread_id"]) for r in rows] == [("human", None)] * 2
            assert h.says() == []
            await h.bridge.push(chat("wait what happened there", channel="party"))
            await h.wait(lambda: h.count("chat_line") == 3)  # positive control: talk is
            assert h.rows("SELECT thread_id FROM chat_line WHERE id = 3")[0][0] is not None
    run(main())


def test_restart_mid_session_resyncs(tmp_path, clock):
    async def main():
        hellos, offline = [], []

        def prepare(app):
            async def on_hello(ev):
                hellos.append(ev.human_guid)

            async def on_offline():
                offline.append(app.clock.now())
            app.register_event_handler("hello", on_hello)
            app.register_offline_hook(on_offline)

        async with running(tmp_path, clock, events=[hello(HUMAN), CAST], prepare=prepare) as h:
            store = h.app.store
            await h.wait(lambda: hellos == [HUMAN])
            first = store.open_session()
            assert first is not None  # he was online at hello: a session, but no greeting
            await h.bridge.drop_client()  # worldserver restart, he is still online
            await h.until(lambda: len(hellos) == 2)
            assert store.open_session() == first and h.count("session") == 1
            h.bridge.set_greeting(hello=hello(0))  # he logged off while the bridge was down
            await h.bridge.drop_client()
            await h.until(lambda: len(hellos) == 3)
            assert store.open_session() is None and offline
            assert h.count("session", "WHERE ended IS NOT NULL") == 1 and len(offline) == 1
            h.bridge.set_greeting(hello=hello(HUMAN))
            await h.bridge.drop_client()
            await h.until(lambda: len(hellos) == 4)
            assert h.count("session", "WHERE ended IS NULL") == 1 and h.count("session") == 2
            await h.advance(60)
            assert h.says() == []  # a resync never greets
            await answered_in(h, store.open_session())  # but it can talk
    run(main())


async def answered_in(h: "Harness", session_id: int) -> None:
    """Tests lens M7, the positive control after a resync's silence: his next line is
    answered, in the session the resync left open."""
    sent = "WHERE kind = 'reply_human' AND dropped IS NULL"
    before = h.count("utterance", sent)
    await h.bridge.push(chat("still there?"))
    await h.advance(40)
    rows = h.rows(f"SELECT session_id FROM utterance {sent} ORDER BY id")
    assert len(rows) > before and {r[0] for r in rows[before:]} == {session_id}


def test_runs_without_ollama(tmp_path, clock):
    async def main():
        llm = FakeLLM(talk_and_extract)
        llm.fail_next(10**6)  # every chat and embed call raises LLMError
        async with running(tmp_path, clock, events=load_fixture(FIXTURE), llm=llm) as h:
            await h.wait(lambda: h.count("chat_line") >= 1)
            await h.advance(300, step=1.0)
            assert h.count("chat_line") == 1 and h.count("game_event") >= 1
            assert h.says() == [] and not h.task.done()
            assert h.count("utterance", "WHERE dropped = 'llm_error'") >= 2  # it tried
            assert h.count("chat_thread", "WHERE status = 'failed'") == 1  # P1c retries it
    run(main())


def test_config_reload_on_login(tmp_path, clock):
    async def main():
        events = [hello(0), CAST, presence("login")]
        async with running(tmp_path, clock, events=events, chattiness=10) as h:
            await h.wait(lambda: h.app.store.open_session() is not None)
            first = h.app.store.open_session()
            await h.advance(300, step=1.0)
            social = "WHERE session_id = ? AND kind IN ('open', 'bit', 'flirt')"
            assert h.count("utterance", social, (first,)) >= 1  # positive control
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: h.app.store.open_session() is None)
            port = h.app.cfg.bridge.port
            (tmp_path / "mind.toml").write_text(toml(port, chattiness=0), encoding="utf-8")
            await h.bridge.push(presence("login"))
            await h.wait(lambda: h.app.store.open_session() not in (None, first))
            second = h.app.store.open_session()
            assert h.app.cfg.talk.chattiness == 0
            await h.advance(600, step=1.0)
            assert h.count("utterance", social, (second,)) == 0
            assert h.count("utterance", "WHERE session_id = ? AND kind = 'greet'", (second,))
    run(main())


def test_logs_are_json_lines(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=load_fixture(FIXTURE)) as h:
            await h.wait(lambda: h.count("chat_line") >= 1)
            await h.advance(60)
            assert h.says()
            log("custom_event", answer=42, who="everyone")

    teardown = setup_logging(tmp_path / "logs")
    try:
        run(main())
    finally:
        teardown()
    records = [json.loads(line) for line in
               (tmp_path / "logs" / "mind.log").read_text(encoding="utf-8").splitlines()]
    assert records and all({"ts", "level", "event"} <= r.keys() for r in records)
    sent = [r for r in records if r["event"] == "say_sent"]
    assert sent and isinstance(sent[0]["guid"], int) and sent[0]["channel"] == "guild"
    assert {"event": "custom_event", "answer": 42, "who": "everyone"}.items() <= next(
        r for r in records if r["event"] == "custom_event").items()
    assert records[-1]["event"] == "shutdown_complete"


def test_log_formats_event_lines_free_text_and_errors(tmp_path, capsys):
    teardown = setup_logging(tmp_path / "logs")
    talk = logging.getLogger("leeroy_mind.talk")
    try:
        talk.info("say_dropped: guid=%d kind=%s channel=%s reason=%s", 101, "open", "guild",
                  "guards")
        talk.warning("talk_llm_failed: guid=%d kind=%s: %s", 102, "greet", "timed out")
        logging.getLogger("leeroy_mind").info("extraction of thread %d failed: %s", 7, "x")
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("leeroy_mind").exception("event_failed: type=%s", "chat")
        log("quiet_one")
    finally:
        teardown()
    lines = (tmp_path / "logs" / "mind.log").read_text(encoding="utf-8").splitlines()
    dropped, failed, free, crashed, quiet = (json.loads(x) for x in lines)
    assert dropped["event"] == "say_dropped" and dropped["level"] == "INFO"
    assert (dropped["guid"], dropped["kind"], dropped["reason"]) == (101, "open", "guards")
    assert (failed["event"], failed["kind"], failed["detail"]) == (
        "talk_llm_failed", "greet", "timed out")
    assert free["event"] == "log" and free["msg"] == "extraction of thread 7 failed: x"
    assert crashed["event"] == "event_failed" and "ValueError: boom" in crashed["exc"]
    assert quiet["event"] == "quiet_one"
    err = capsys.readouterr().err  # WARNING and above reach the console
    assert "talk_llm_failed" in err and "event_failed" in err
    assert "say_dropped" not in err and "quiet_one" not in err
    assert not logging.getLogger("leeroy_mind").handlers  # the teardown removed them


def test_check_ollama_chat_warns(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    conf = tmp_path / "mod_ollama_chat.conf"

    def warnings(text: str) -> list[str]:
        conf.write_text(text, encoding="utf-8")
        return check_ollama_chat(conf, cfg)

    good = f'OllamaChat.Enable = 1\nOllamaChat.Model = "{ORION}"\nOllamaChat.NumCtx = 4096\n'
    assert warnings(good) == []
    assert warnings(good.replace(f'"{ORION}"', ORION)) == []  # unquoted values too (C17)
    assert warnings(f"# OllamaChat.Model = other\n{good}") == []  # comments are skipped
    assert len(warnings(good.replace(ORION, "qwen3:14b"))) == 1
    assert len(warnings(good.replace("4096", "8192"))) == 1
    two = warnings(good.replace(ORION, "qwen3:14b").replace("4096", '"8192"'))
    assert len(two) == 2 and any("qwen3:14b" in w for w in two) and any("8192" in w for w in two)
    assert len(warnings("OllamaChat.Enable = 1\n")) == 2  # unset: the module's own defaults
    conf.unlink()
    assert len(check_ollama_chat(conf, cfg)) == 1  # a missing file gives one warning


def test_registered_handlers_see_every_event_type(tmp_path, clock):
    async def main():
        seen: list[tuple[str, object]] = []

        def prepare(app):
            store = app.store

            def stored(sql, params=()):
                n = store.conn.execute(sql, params).fetchone()[0]
                store.conn.commit()
                return n

            async def on(ev):
                name = type(ev).__name__
                if name == "Hello":
                    seen.append(("hello", store.get_meta("guild_id")))
                elif name == "Login":
                    seen.append(("login", store.open_session()))
                elif name == "Logout":
                    seen.append(("logout", store.open_session()))
                elif name == "Chat" and ev.via_bridge:
                    seen.append(("echo", None))  # the ack may be recorded before or after it
                elif name == "Chat":
                    seen.append(("chat", stored("SELECT count(*) FROM chat_line WHERE text = ?",
                                                (ev.text,))))
                else:
                    seen.append((name, stored("SELECT count(*) FROM game_event WHERE type = ?",
                                              ("afk" if name == "AfkEvent" else "test_app_job",))))
            for t in ("hello", "login", "logout", "chat", "afk", "test_app_job"):
                app.register_event_handler(t, on)

        events = [hello(0), CAST, presence("login"), chat("evening all"),
                  {"type": "afk", "guid": 103, "on": True},
                  {"type": "test_app_job", "guid": 0, "kind": "form_raid"}]
        async with running(tmp_path, clock, events=events, prepare=prepare) as h:
            await h.wait(lambda: len(seen) >= 5)
            await h.advance(60)
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: any(k == "logout" for k, _ in seen))
            kinds = dict(seen)  # each handler ran after the app's own row
            assert kinds["hello"] == str(GUILD_ID)
            assert kinds["login"] is not None and kinds["logout"] is None
            assert kinds["chat"] == 1 and kinds["AfkEvent"] == 1 and kinds["TestJob"] == 1
            assert ("echo", None) in seen  # echoes reach the handlers too
            assert h.count("chat_line") == 1 + len(h.says())  # and are never stored
            assert h.count("game_event", "WHERE type = 'test_app_job'") == 1
    run(main())


def test_connection_hook_on_drop_and_reconnect(tmp_path, clock):
    async def main():
        ups: list[bool] = []

        def prepare(app):
            async def hook(up):
                ups.append(up)
            app.register_connection_hook(hook)

        async with running(tmp_path, clock, events=[hello(0), CAST], prepare=prepare) as h:
            await h.wait(lambda: ups == [True])
            await h.bridge.drop_client()
            await h.until(lambda: ups == [True, False, True])
    run(main())


def test_background_tasks_start_and_stop(tmp_path, clock):
    async def main():
        log_: list[str] = []

        def prepare(app):
            async def loop():
                log_.append(f"started running={app.running}")
                try:
                    await asyncio.Event().wait()
                finally:
                    log_.append(f"cancelled session_open={app.store.open_session() is not None}")
            app.register_background(loop)

        events = [hello(0), CAST, presence("login")]
        async with running(tmp_path, clock, events=events, prepare=prepare) as h:
            await h.wait(lambda: h.app.store.open_session() is not None and log_)
            assert log_ == ["started running=True"]
            await h.app.stop()
            # stop() cancels the extensions first, while the session is still open
            assert log_ == ["started running=True", "cancelled session_open=True"]
            await asyncio.wait({h.task}, timeout=2.0)
            assert h.task.done() and not h.app.running
    run(main())


def test_app_exposes_extension_attributes(tmp_path, clock):
    async def main():
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
        try:
            for name, kind in [("store", Store), ("scheduler", LLMScheduler),
                               ("embedder", Embedder), ("writer", MemoryWriter),
                               ("event_rules", EventMemoryRules), ("personas", PersonaCache),
                               ("world", WorldView), ("raid", RaidState),
                               ("engine", TalkEngine), ("bridge", BridgeClient)]:
                assert isinstance(getattr(app, name), kind), name
            assert app.cfg.talk.chattiness == 4 and app.data_dir == tmp_path
            assert app.clock is clock and isinstance(app.llm, FakeLLM)
            assert app.extractor is not None and app.arbiter is None
            for hook in ("register_event_handler", "register_connection_hook",
                         "register_offline_hook", "register_background", "handle", "origin_of"):
                assert callable(getattr(app, hook)), hook
        finally:
            await app.stop()
    run(main())


def test_the_extractor_reads_the_engines_words_for_its_lines(tmp_path, clock):
    """Fix wave G3 (rehearsal 3, check 8): the copy rule's words before the typing come from
    the talk engine."""
    async def main():
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
        try:
            app.engine._words[7] = "the words before the typing"
            assert app.extractor._said(7) == "the words before the typing"
            assert app.extractor._said(8) is None
        finally:
            await app.stop()
    run(main())


def test_second_instance_refused(tmp_path):
    assert not instance_lock_held(tmp_path)
    with acquire_instance_lock(tmp_path):
        assert instance_lock_held(tmp_path)
        with pytest.raises(AlreadyRunning):
            acquire_instance_lock(tmp_path)
        assert instance_lock_held(tmp_path)
    assert not instance_lock_held(tmp_path)
    with acquire_instance_lock(tmp_path):  # free again once released
        pass


# -- hello, the human and sessions (carry from Task 16; pre-flight rulings 5 and 6) --------------

def test_hello_keeps_the_last_human_guid_and_login_greets(tmp_path, clock, caplog):
    async def main():
        events = [hello(0, guild_id=1), CAST]
        async with running(tmp_path, clock, events=events) as h:
            app = h.app
            await h.wait(lambda: app.world.online)
            assert "guild_id_unexpected" in caplog.text  # the guild is 21
            assert app.store.get_meta("guild_id") == "1"
            assert app.store.open_session() is None and app.store.get_meta("human_guid") is None
            assert app.engine.human.guid == HUMAN  # the roster's guild master until named
            await h.bridge.push(presence("login"))
            await h.wait(lambda: app.store.open_session() is not None)
            assert app.store.get_meta("human_guid") == str(HUMAN)
            assert app.engine.human.guid == HUMAN and app.engine.human.is_guild_master
            await h.advance(60)
            greet = h.count("utterance", "WHERE kind = 'greet' AND dropped IS NULL")
            assert 2 <= greet <= 3 and len(h.says()) == greet  # his login is greeted
            await h.bridge.push(presence("login"))  # a repeated login is idempotent
            await h.advance(60)
            assert h.count("session") == 1 and len(h.says()) == greet
            await h.bridge.push(hello(0))  # he logged off: 0 never replaces his guid
            await h.wait(lambda: app.store.open_session() is None)
            assert app.store.get_meta("human_guid") == str(HUMAN)
            assert app.engine.human.guid == HUMAN
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(main())


RAID_GROUP = {"type": "group", "leader_guid": HUMAN, "raid": True,
              "members": [HUMAN, 101, 102, 103, 104, 105]}


def boss(state: str) -> dict:
    return {"type": "boss", "map": 631, "instance_id": 5, "boss_id": 0, "boss_name": "",
            "state": state, "members": RAID_GROUP["members"]}


def achievement(guid: int, achievement_id: int = 2136) -> dict:
    return {"type": "achievement", "guid": guid, "achievement_id": achievement_id,
            "name": "Glory of the Hero"}


def spoken(h: "Harness") -> list[dict]:
    """Every command the mind sent but the bridge client's own pings."""
    return [c for c in h.bridge.commands if c["type"] != "ping"]


def test_nothing_is_sent_while_the_model_is_away(tmp_path, clock):
    """The silent latch (controller ruling): down at start, the guild says nothing at all, LLM
    line or template, emote or say_as, and everything is still recorded."""
    async def main():
        llm = FakeLLM(talk_and_extract)  # Ollama answers, but the live model is not pulled
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST, RAID_GROUP],
                           llm=llm) as h:
            await h.wait(lambda: h.app.world.group is not None)
            assert not h.app.model_ready
            for ev in (boss("engaged"), boss("killed"), achievement(102),
                       chat("gg all, nice kill")):
                await h.bridge.push(ev)
            await h.wait(lambda: h.count("chat_line") == 1)
            await h.advance(45)  # the kill line and gz, the emotes, the gz, the replies
            said = asyncio.create_task(h.app.engine.say_as(101, "guild", "o/",
                                                           kind=TurnKind.CALLOUT))
            await h.advance(15)  # its typing delay runs on the clock
            assert said.done() and said.result() is False  # P1d's template path too
            assert spoken(h) == []
            assert h.count("utterance", "WHERE dropped = 'model_away'") >= 3  # at the gate
            assert h.count("utterance", "WHERE dropped IS NULL") == 0
            kinds = {r["type"] for r in h.rows("SELECT type FROM game_event")}
            assert {"group", "boss", "achievement"} <= kinds  # still recorded
            assert any("killed" in text for _, text, _ in h.memories())
    run(main())


def test_speech_resumes_once_a_check_finds_the_model(tmp_path, clock, caplog):
    async def main():
        llm = FakeLLM(talk_and_extract)  # not pulled yet
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST], llm=llm) as h:
            await h.wait(lambda: h.app.world.online)
            await h.bridge.push(achievement(102))
            await h.advance(30)
            assert spoken(h) == [] and not h.app.model_ready
            llm.set_models({ORION})  # pulled
            await h.advance(40)  # past the next check, 60 s after the first
            assert h.app.model_ready
            await h.bridge.push(achievement(103, 2137))
            await h.advance(30)
            assert spoken(h) and {c["type"] for c in spoken(h)} == {"say"}
        assert caplog.text.count("ollama_unavailable") == 1  # once per change, not per check
        assert caplog.text.count("ollama_available") == 1
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())


def test_a_model_that_goes_away_silences_the_guild_within_one_check(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST]) as h:
            await h.wait(lambda: h.app.world.online and h.app.model_ready)
            await h.bridge.push(achievement(102))
            await h.advance(30)
            said = len(spoken(h))
            assert said >= 1  # positive control: it speaks while the model is there
            h.llm.set_models(set())  # unloaded, or removed
            await h.advance(40)  # one check
            assert not h.app.model_ready
            await h.bridge.push(achievement(103, 2137))
            await h.advance(30)
            assert len(spoken(h)) == said
            assert h.count("utterance", "WHERE dropped = 'model_away'") >= 1
    run(main())


def crashed(tmp_path: Path, *, quiet_for: timedelta) -> tuple[int, int, float]:
    """mind.db as a crashed run left it: a session never ended, its last line `quiet_for` before
    START, and that line's thread still 'open'. Returns (session, thread, the line's ts)."""
    seed_db(tmp_path / "mind.db", FakeClock(START))
    then = FakeClock(START - quiet_for)
    s = Store(open_db(tmp_path / "mind.db"), clock=then)
    try:
        sid = s.start_session(then.now() - 600)
        line = s.insert_chat_line(Chat(ts=0, channel="guild", from_guid=HUMAN,
                                       from_name="Testmage", to_guid=0, text="brb", map=0,
                                       zone=0), session_id=sid, origin="human")
        tid = s.open_thread("guild", then.now())
        s.set_thread([line], tid)
    finally:
        s.close()  # no end_session: the process died
    return sid, tid, then.now()


def test_a_crashed_session_is_resumed_while_he_is_still_on(tmp_path, clock):
    async def main():
        sid, tid, _ = crashed(tmp_path, quiet_for=timedelta(minutes=5))
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST]) as h:
            await h.wait(lambda: h.app.session_id is not None)
            assert h.app.session_id == sid == h.app.store.open_session()
            assert h.count("session") == 1  # the same session, not a second one
            await h.advance(60)
            assert h.says() == []  # resumed, not greeted
            # M4: the thread the crash left open went to P1c's pile when the app was built
            assert h.app.store.thread_status(tid) == "pending"
            await answered_in(h, sid)
    run(main())


def test_a_crashed_session_ends_once_when_he_is_gone(tmp_path, clock):
    async def main():
        offline: list[float] = []

        def prepare(app):
            async def hook():
                offline.append(app.clock.now())
            app.register_offline_hook(hook)

        sid, _, last = crashed(tmp_path, quiet_for=timedelta(minutes=5))
        async with running(tmp_path, clock, events=[hello(0), CAST], prepare=prepare) as h:
            await h.wait(lambda: h.app.store.open_session() is None and offline)
            await h.settle()
            rows = h.rows("SELECT id, ended FROM session")
            assert [(r["id"], r["ended"]) for r in rows] == [(sid, last)]  # where it went quiet
            assert len(offline) == 1
    run(main())


def test_a_stale_crashed_session_is_closed_and_a_new_one_begun(tmp_path, clock):
    async def main():
        sid, _, last = crashed(tmp_path, quiet_for=timedelta(hours=2))
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST]) as h:
            await h.wait(lambda: h.app.session_id not in (None, sid))
            rows = h.rows("SELECT id, ended FROM session ORDER BY id")
            assert [r["ended"] for r in rows] == [last, None]  # yesterday's, then tonight's
            await h.advance(60)
            assert h.says() == []  # a resync never greets
            await answered_in(h, h.app.session_id)
    run(main())


def test_offline_hooks_run_after_the_sessions_last_writes(tmp_path, clock):
    """M2: when an offline hook (P1c's night job) starts, the session's threads are extracted
    and its activity summaries written: nothing is left for it to pick up twice."""
    async def main():
        seen: list[tuple] = []

        gate = asyncio.Event()
        gate.set()

        def prepare(app):
            async def hook():
                activity = app.store.conn.execute(
                    "SELECT count(*) FROM memory WHERE text LIKE 'Tonight:%'").fetchone()[0]
                app.store.conn.commit()
                seen.append((app.store.threads_pending(), activity))
            app.register_offline_hook(hook)
            real = app.extractor.extract

            async def gated(t):  # held before the model, so the activity summaries still go
                await gate.wait()
                return await real(t)
            app.extractor.extract = gated

        dalaran = {"type": "zone", "guid": 101, "zone_id": 4395, "zone_name": "Dalaran"}
        events = [hello(0), CAST, presence("login"), chat("evening all"), dalaran]
        async with running(tmp_path, clock, events=events, prepare=prepare) as h:
            await h.wait(lambda: h.count("game_event", "WHERE type = 'zone'") == 1)
            await h.advance(20)
            gate.clear()  # tests lens M3: the session's extraction cannot finish yet
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: h.app.store.open_session() is None)
            await h.wait(lambda: h.count("memory", "WHERE text LIKE 'Tonight:%'") == 1)
            for _ in range(5):
                await h.settle()
            assert seen == []  # the summaries are in; the hook still waits for the extraction
            gate.set()
            await h.wait(lambda: seen)
            assert seen == [([], 1)]
    run(main())


def test_a_new_human_guid_drops_the_old_chat_name(tmp_path, clock):
    """M3: the chat name belongs to the character it was seen on."""
    async def main():
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST, chat("evening")]) as h:
            await h.wait(lambda: h.app.store.get_meta("human_name") == "Testmage")
            h.app.store.upsert_cast([dataclasses.replace(HUMAN_ROW, guid=2, name="Othermage")],
                                    1000.0)
            await h.bridge.push(presence("login", guid=2))
            await h.wait(lambda: h.app.human_guid == 2)
            assert h.app.store.get_meta("human_name") == ""
            assert h.app.engine.human.character_name == "Othermage"  # his roster row
    run(main())


def test_the_human_name_comes_from_his_chat(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=load_fixture(FIXTURE)) as h:
            await h.wait(lambda: h.count("chat_line") >= 1)
            assert h.app.store.get_meta("human_name") == "Testmage"
            assert h.app.engine.human.character_name == "Testmage"
    run(main())


def test_backlog_events_are_recorded_but_never_answered(tmp_path, clock):
    async def main():
        old = {"ts": 1_000}  # long before the hello: replayed on connect
        events = [hello(0), CAST, presence("login", **old),
                  chat("evening all, anyone up for heroics", **old),
                  {"type": "achievement", "guid": 102, "achievement_id": 2136,
                   "name": "Glory of the Hero", **old}]
        async with running(tmp_path, clock, events=events) as h:
            await h.wait(lambda: h.count("chat_line") == 1 and h.count("game_event") == 2)
            await h.advance(60)
            assert h.says() == [] and h.count("utterance") == 0  # never answered
            assert h.app.store.open_session() is None  # the hello says he is offline
            row = h.rows("SELECT origin, thread_id FROM chat_line")[0]
            assert row["origin"] == "human" and row["thread_id"] is not None  # threaded
            assert any("Glory of the Hero" in text for _, text, _ in h.memories())
            await h.bridge.push(presence("login"))  # positive control: live events are
            await h.advance(60)
            assert h.says()
    run(main())


T_HELLO = int(START.timestamp() * 1000)  # the server's ms clock when it greets


def _line_at(ts: int) -> Chat:
    return Chat(ts=ts, channel="guild", from_guid=HUMAN, from_name="Testmage", to_guid=0,
                text="hi", map=0, zone=0)


def test_is_backlog_is_more_than_5_s_older_than_the_hello(tmp_path, clock):
    """Tests lens I1: realistic server timestamps (ms) one either side of the threshold."""
    async def main():
        seed_db(tmp_path / "mind.db", clock)
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
        try:
            assert not app.is_backlog(_line_at(T_HELLO - 600_000))  # no hello yet
            await app.handle(Hello(ts=T_HELLO, server="mod-leeroy", version="0.1.0",
                                   guild_id=GUILD_ID, human_guid=0))
            assert app.is_backlog(_line_at(T_HELLO - 5_001))
            assert app.is_backlog(_line_at(T_HELLO - 600_000))
            for ts in (T_HELLO - 5_000, T_HELLO - 4_999, T_HELLO, T_HELLO + 10):
                assert not app.is_backlog(_line_at(ts)), ts
            assert not app.is_backlog(Hello(ts=T_HELLO - 60_000, server="mod-leeroy",
                                            version="0.1.0", guild_id=GUILD_ID, human_guid=0))
            assert not app.is_backlog(Cast(ts=T_HELLO - 60_000, members=()))
        finally:
            await app.stop()
    run(main())


def test_backlog_is_what_the_bridge_held_more_than_5_s(tmp_path, clock):
    """Tests lens I1: a restart replays the last minutes. His login 3 minutes and his line 1
    minute before the hello are only recorded; the line from 2 s before it is answered."""
    async def main():
        heard: list[str] = []

        def prepare(app):
            real = app.engine.on_human_chat

            async def spy(row):
                heard.append(row.text)
                await real(row)
            app.engine.on_human_chat = spy

        events = [{**hello(HUMAN), "ts": T_HELLO}, {**CAST, "ts": T_HELLO},
                  presence("login", ts=T_HELLO - 180_000),
                  chat("anyone up for toc", ts=T_HELLO - 60_000),
                  chat("evening all", ts=T_HELLO - 2_000)]
        async with running(tmp_path, clock, events=events, prepare=prepare) as h:
            await h.wait(lambda: h.count("chat_line") >= 2)
            await h.advance(60)
            assert heard == ["evening all"]  # the minute-old line never reaches the engine
            assert h.count("utterance", "WHERE kind = 'greet'") == 0  # nor the replayed login
            assert h.count("utterance", "WHERE kind = 'reply_human' AND dropped IS NULL") >= 1
            assert h.says()
    run(main())


def test_a_worldserver_restart_keeps_the_mind_running(tmp_path, clock, caplog):
    """Tests lens I2: while the worldserver restarts its port refuses connections; the mind
    keeps retrying, reconnects to the same port and answers him again."""
    async def main():
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST]) as h:
            await h.wait(lambda: h.app.world.online and h.app.session_id is not None)
            sid = h.app.session_id
            await h.bridge.go_down()
            await h.until(lambda: "bridge_connect_failed" in caplog.text, timeout=10.0)
            assert not h.task.done() and not h.app.bridge.connected  # refused, still running
            await h.bridge.come_up(hello=hello(HUMAN))
            await h.until(lambda: h.bridge.connections == 2 and h.app.bridge.connected,
                          timeout=10.0)
            assert not h.task.done() and h.count("session") == 1 and h.app.session_id == sid
            replies = h.count("utterance", "WHERE kind = 'reply_human' AND dropped IS NULL")
            await h.bridge.push(chat("still there?"))
            await h.advance(40)
            assert h.count("utterance",
                           "WHERE kind = 'reply_human' AND dropped IS NULL") > replies
            assert not h.task.done()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())


def test_an_echo_before_its_ack_is_still_recorded_once(tmp_path, clock):
    """Tests lens M11: mod-leeroy may write a say's echo before its ack."""
    async def main():
        async with running(tmp_path, clock, events=load_fixture(FIXTURE),
                           bridge_kw={"echo_first": True}) as h:
            await h.wait(lambda: h.count("chat_line") >= 1)
            await h.advance(120)
            says = h.says()
            assert says and h.count("chat_line") == 1 + len(says)
            assert h.count("utterance", "WHERE dropped IS NULL") == len(says)
    run(main())


def test_whispers_without_a_cast_member_are_ignored(tmp_path, clock):
    async def main():
        handled: list[str] = []

        def prepare(app):
            async def on_chat(ev):
                if not ev.via_bridge:
                    handled.append(ev.text)
            app.register_event_handler("chat", on_chat)

        async with running(tmp_path, clock, events=[hello(HUMAN), CAST],
                           prepare=prepare) as h:
            await h.wait(lambda: h.app.world.online)
            await h.bridge.push(chat("u selling that ore?", channel="whisper", to_guid=999))
            await h.bridge.push(chat("ty", channel="whisper", from_guid=999,
                                     from_name="Stranger", to_guid=HUMAN))
            await h.bridge.push(chat("you around?", channel="whisper", to_guid=101))
            await h.wait(lambda: h.count("chat_line") >= 1)
            await h.settle()
            rows = h.rows("SELECT text, audience FROM chat_line")
            assert [r["text"] for r in rows] == ["you around?"]  # C27
            assert json.loads(rows[0]["audience"]) == [101]
            assert handled == ["you around?"]  # M1: ignored outright, handlers included
    run(main())


def test_echoed_emotes_are_not_stored_and_errors_are_only_logged(tmp_path, clock, caplog):
    async def main():
        async with running(tmp_path, clock, events=[hello(0), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
            await h.bridge.push({"type": "text_emote", "guid": 101, "emote": "cheer",
                                 "target_guid": 0, "via_bridge": True})
            await h.bridge.push({"type": "error", "message": "bad field"})
            await h.bridge.push({"type": "text_emote", "guid": HUMAN, "emote": "hug",
                                 "target_guid": 102})
            await h.wait(lambda: h.count("game_event") == 1)
            await h.settle()
            assert [r["type"] for r in h.rows("SELECT type FROM game_event")] == ["text_emote"]
            assert "bridge_error" in caplog.text and h.says() == []
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(main())


def test_every_chat_line_stores_who_could_see_it(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=load_fixture(FIXTURE)) as h:
            await h.wait(lambda: h.count("chat_line") >= 1)
            await h.advance(60)
            rows = h.rows("SELECT origin, audience FROM chat_line ORDER BY id")
            assert {r["origin"] for r in rows} == {"human", "mind"}
            assert all(json.loads(r["audience"]) == [101, 102, 103, 104, 105] for r in rows)
    run(main())


def _persona(guid: int):
    from conftest import make_persona
    return make_persona(guid, f"M{guid}")


def test_bystanders_are_the_recent_online_audience(tmp_path, clock):
    async def main():
        seed_db(tmp_path / "mind.db", clock)
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
        try:
            extra = range(201, 213)
            for g in extra:  # twelve more members with personas, to reach the cap
                save_persona(app.store, _persona(g), source="seed", ts=1000.0,
                             race=RACES[2], class_name=CLASSES[1])
            members = [Member(guid=g, name=f"M{g}", race=2, class_=1, gender=0, level=80, rank=3,
                              online=g != 105, map=0, zone=10, spec_tab=-1)
                       for g in (101, 102, 103, 104, 105, *extra)]
            await app.handle(Hello(ts=0, server="mod-leeroy", version="0.1.0",
                                   guild_id=GUILD_ID, human_guid=HUMAN))
            await app.handle(Cast(ts=0, members=tuple(members)))

            async def say(guid, text, channel="guild"):
                await app.handle(Chat(ts=0, channel=channel, from_guid=guid,
                                      from_name=f"M{guid}", to_guid=0, text=text, map=0, zone=10))
            await say(103, "back from the bank", channel="party")  # 20 minutes ago
            await clock.advance(900)
            await say(102, "brb kettle", channel="say")  # 5 minutes ago, another channel
            await say(105, "night all")  # offline now
            await clock.advance(300)
            await say(HUMAN, "evening all")
            await say(101, "evening boss")
            ids = [r[0] for r in app.store.conn.execute(
                "SELECT id FROM chat_line WHERE channel = 'guild' AND from_guid IN (1, 101)")]
            app.store.conn.commit()
            thread = ClosedThread(1, "guild", tuple(ids), "silence")
            assert app.bystanders(thread, {HUMAN, 101}) == [102]
            for g in extra:
                await say(g, f"hi from {g}")
            got = app.bystanders(thread, {HUMAN, 101})
            assert len(got) == 10 and not {101, 103, 105} & set(got)
        finally:
            await app.stop()
    run(main())


def test_event_memories_are_written_off_the_dispatcher(tmp_path, clock):
    async def main():
        gate = asyncio.Event()
        llm = FakeLLM(talk_and_extract, gate=gate)  # the embedder waits until the gate opens
        events = [hello(0), CAST,
                  {"type": "loot_won", "guid": 103, "item_id": 50734, "name": "Arcane Bracers",
                   "method": "roll"},
                  chat("grats on the bracers", from_guid=104, from_name="Dunna")]
        async with running(tmp_path, clock, events=events, llm=llm) as h:
            await h.wait(lambda: h.count("chat_line") == 1)  # the next event is handled
            assert h.memories() == []  # while the loot memory still waits on the embedder
            gate.set()
            await h.wait(lambda: h.memories())
            owner, text, evidence = h.memories()[0]
            assert owner == 103 and "Arcane Bracers" in text and evidence[0].startswith("event:")
    run(main())


def test_writer_screens_the_owners_real_name(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
            r = await h.app.writer.write(MemoryCandidate(
                owner=101, kind="conversation", text=f"I owe {REAL} a pint.", t_event=1,
                actors=(101, HUMAN), importance=5, valence=0.3, source="extract",
                evidence=("chat:1",)))
            assert r.action == "inserted"
            assert h.memories()[-1][1] == f"I owe {NICK} a pint."
    run(main())


# -- shutdown and Ctrl+C -----------------------------------------------------------------------

def test_stop_extracts_the_open_threads(tmp_path, clock):
    async def main():
        events = [hello(HUMAN), CAST, chat("anyone up?")]
        async with running(tmp_path, clock, events=events) as h:
            await h.wait(lambda: h.count("chat_thread") == 1)
            await h.app.stop()
        with contextlib.closing(reopen(tmp_path)) as c:
            assert c.execute("SELECT status FROM chat_thread").fetchone()[0] == "extracted"
    run(main())


def test_stop_leaves_threads_it_cannot_extract_pending(tmp_path, clock):
    async def main():
        llm = FakeLLM(talk_and_extract, gate=asyncio.Event())  # the model never answers
        # two threads: one cut off mid-extraction, one still queued behind it
        events = [hello(HUMAN), CAST, chat("anyone up?"), chat("u there?", channel="whisper",
                                                               to_guid=101)]
        async with running(tmp_path, clock, events=events, llm=llm) as h:
            await h.wait(lambda: h.count("chat_thread") == 2)
            h.app.extract_budget_s = 0.2
            await h.app.stop()
            await asyncio.wait({h.task}, timeout=2.0)
            assert h.task.done()
        with contextlib.closing(reopen(tmp_path)) as c:
            assert [r[0] for r in c.execute("SELECT status FROM chat_thread")] == ["pending"] * 2
    run(main())


def test_events_during_shutdown_are_only_recorded(tmp_path, clock):
    async def main():
        seen: list[str] = []

        def prepare(app):
            async def on_chat(ev):
                seen.append(ev.text)
            app.register_event_handler("chat", on_chat)

        gate = asyncio.Event()
        llm = FakeLLM(talk_and_extract, gate=gate)  # stop() waits for the extraction
        events = [hello(HUMAN), CAST, chat("anyone up?")]
        async with running(tmp_path, clock, events=events, llm=llm, prepare=prepare) as h:
            await h.wait(lambda: h.count("chat_thread") == 1)
            h.app.extract_budget_s = 60.0  # tests lens M8: the gate ends the wait, not a timer
            stopping = asyncio.create_task(h.app.stop())
            await h.wait(lambda: h.app._stopping)
            await h.bridge.push(chat("still there?"))  # the bridge is still up
            await h.wait(lambda: h.count("chat_line") == 2)
            assert h.rows("SELECT thread_id FROM chat_line WHERE id = 2")[0][0] is None
            assert seen == ["anyone up?"]  # no handler, no thread, no talk while stopping
            gate.set()
            await asyncio.wait_for(stopping, 10.0)
    run(main())


def test_cancelling_run_stops_the_app(tmp_path, clock, caplog):
    async def main():
        events = [hello(0), CAST, presence("login")]
        async with running(tmp_path, clock, events=events) as h:
            await h.wait(lambda: h.app.store.open_session() is not None)
            h.task.cancel()  # what asyncio.run does with Ctrl+C
            with pytest.raises(asyncio.CancelledError):
                await h.task
            assert not h.app.running
        with contextlib.closing(reopen(tmp_path)) as c:
            assert c.execute("SELECT count(*) FROM session WHERE ended IS NULL").fetchone()[0] == 0
        assert "shutdown_complete" in caplog.text
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())


# -- startup checks and the CLI ----------------------------------------------------------------

def test_startup_checks_log_what_is_missing(tmp_path, clock, caplog):
    async def main():
        cfg = load_config(tmp_path / "mind.toml")
        conf = tmp_path / "mod_ollama_chat.conf"
        conf.write_text('OllamaChat.Model = "qwen3:14b"\nOllamaChat.NumCtx = 4096\n',
                        encoding="utf-8")
        app = MindApp(cfg, tmp_path, llm=FakeLLM(models={cfg.models.live}), clock=clock)
        try:
            assert not app.model_ready  # silent until a check has found the live model
            await startup_checks(app, ollama_conf=conf)
            assert app.model_ready and "ollama_available" in caplog.text
            assert "ollama_model_missing" in caplog.text and cfg.models.embedding in caplog.text
            assert "ollama_chat_mismatch" in caplog.text and "ollama_unavailable" not in caplog.text
            caplog.clear()

            class Down(FakeLLM):
                async def list_models(self):
                    raise LLMError("connection refused")
            app.llm = Down()
            await startup_checks(app, ollama_conf=conf)
            assert not app.model_ready
            assert "ollama_unavailable" in caplog.text and "connection refused" in caplog.text
            app.llm = FakeLLM()  # Ollama answers, without the live model
            caplog.clear()
            await app.check_model()
            assert not app.model_ready and caplog.text == ""  # no change: nothing logged
            app.store.set_meta("embed_model", "another-embedder")
            with pytest.raises(EmbeddingMismatch):
                await startup_checks(app, ollama_conf=conf)
        finally:
            await app.stop()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())


def test_run_refuses_a_second_instance(tmp_path, capsys, monkeypatch):
    import leeroy_mind.__main__ as cli
    served = []

    async def fake_serve(root, **kw):  # never the real service, whatever the lock does
        served.append(root)
        return 0
    monkeypatch.setattr(cli, "serve", fake_serve)
    _ready_dir(tmp_path)
    with acquire_instance_lock(tmp_path):
        assert cli.main(["run", "--data-dir", str(tmp_path)]) == 1
    assert "mind service is running" in capsys.readouterr().out and served == []
    assert cli.main(["run", "--data-dir", str(tmp_path)]) == 0 and served == [tmp_path]


def test_serve_snapshots_checks_and_stops(tmp_path, caplog):
    from leeroy_mind.__main__ import serve

    async def main():
        bridge = FakeBridge([hello(0), CAST])
        port = await bridge.start()
        (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
        seed_db(tmp_path / "mind.db", FakeClock(START))
        conf = tmp_path / "mod_ollama_chat.conf"
        conf.write_text(f'OllamaChat.Model = "{ORION}"\nOllamaChat.NumCtx = 4096\n',
                        encoding="utf-8")
        apps = []
        task = asyncio.create_task(serve(tmp_path, make_llm=lambda cfg: FakeLLM(),
                                         ollama_conf=conf, ready=apps.append))
        try:
            async def connected():
                while not (apps and apps[0].world.online):
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(connected(), 5.0)
            await apps[0].stop()
            assert await asyncio.wait_for(task, 5.0) == 0
        finally:
            await bridge.stop()
        assert len(list((tmp_path / "snapshots").glob("mind-start-*.db"))) == 1
        assert "ollama_unavailable" in caplog.text  # FakeLLM has no models
        assert "ollama_chat_mismatch" not in caplog.text
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(main())


def _doctor_setup(tmp_path, port: int, *, model: str = ORION) -> Path:
    (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
    seed_db(tmp_path / "mind.db", FakeClock(START))
    conf = tmp_path / "mod_ollama_chat.conf"
    conf.write_text(f'OllamaChat.Model = "{model}"\nOllamaChat.NumCtx = 4096\n',
                    encoding="utf-8")
    return conf


def test_doctor_reports_every_check(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    cfg = load_config(tmp_path / "missing.toml")
    models = {cfg.models.live, cfg.models.embedding, cfg.models.reflection}
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        conf = _doctor_setup(tmp_path, listener.getsockname()[1])
        make_library(tmp_path / "library.db", FakeClock(START))
        llm = FakeLLM(lambda model, messages: "hey all", models=models)
        argv = ["doctor", "--data-dir", str(tmp_path), "--ollama-conf", str(conf)]
        assert main(argv, make_llm=lambda c: llm) == 0
        out = capsys.readouterr().out.splitlines()
        assert len(out) == 14 and all(line.startswith("ok") for line in out), out
        assert any("live_fallback" in line and "not pulled" in line for line in out)
        assert any("personas 5" in line for line in out)
        assert any("fit every woman's attitude" in line for line in out)
        assert "ok   library: 2 chunks, embedded 0, model none" in out
        chat_call = next(c for c in llm.calls if "messages" in c)
        assert chat_call["think"] is False and chat_call["options"]["num_ctx"] == 4096
    finally:
        listener.close()


def test_doctor_fails_what_is_broken(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]  # closed once the block ends: nothing listens there
    conf = _doctor_setup(tmp_path, port, model="qwen3:14b")
    cfg = load_config(tmp_path / "mind.toml")
    llm = FakeLLM(lambda model, messages: "", models={cfg.models.live})  # it thought anyway
    argv = ["doctor", "--data-dir", str(tmp_path), "--ollama-conf", str(conf)]
    assert main(argv, make_llm=lambda c: llm) == 1
    fails = [line for line in capsys.readouterr().out.splitlines() if line.startswith("FAIL")]
    text = "\n".join(fails)
    assert "think" in text and cfg.models.embedding in text and "qwen3:14b" in text
    assert f"127.0.0.1:{port}" in text and len(fails) == 4
    with acquire_instance_lock(tmp_path):  # the service holds the bridge: no connection
        main(argv, make_llm=lambda c: llm)
    assert "skipped" in capsys.readouterr().out


@pytest.mark.parametrize("what", ["missing", "garbage", "embedded"])
def test_doctor_reports_the_library_and_only_notes_its_absence(tmp_path, capsys, what):
    from leeroy_mind.__main__ import main
    cfg = load_config(tmp_path / "missing.toml")
    models = {cfg.models.live, cfg.models.embedding, cfg.models.reflection}
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        conf = _doctor_setup(tmp_path, listener.getsockname()[1])
        path = tmp_path / "library.db"
        if what == "garbage":
            path.write_bytes(b"not a database at all " * 64)
        elif what == "embedded":
            make_library(path, FakeClock(START))
            c = sqlite3.connect(path)
            c.execute("UPDATE chunks SET embedding = x'00' WHERE id = 1")
            c.execute("INSERT INTO meta(key, value) VALUES ('embed_model', 'another-embedder')")
            c.commit()
            c.close()
        before = path.read_bytes() if path.exists() else None
        llm = FakeLLM(lambda model, messages: "hey all", models=models)
        argv = ["doctor", "--data-dir", str(tmp_path), "--ollama-conf", str(conf)]
        code = main(argv, make_llm=lambda c: llm)
        out = capsys.readouterr().out.splitlines()
        line = next(x for x in out if "library" in x)
        if what == "missing":
            assert code == 0 and line.startswith("note library: no library.db at")
            assert "talk runs without facts" in line
        elif what == "garbage":
            assert code == 1 and line.startswith("FAIL library.db cannot be read")
        else:
            assert code == 0 and line == ("ok   library: 2 chunks, embedded 1, model "
                                          f"another-embedder (the mind embeds with "
                                          f"{cfg.models.embedding}: text search only)")
        assert (path.read_bytes() if path.exists() else None) == before  # read-only
    finally:
        listener.close()


def test_db_snapshot_and_migrate(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    seed_db(tmp_path / "mind.db", FakeClock(START))
    assert main(["db", "--data-dir", str(tmp_path), "snapshot", "--label", "manual"]) == 0
    assert len(list((tmp_path / "snapshots").glob("mind-manual-*.db"))) == 1
    assert main(["db", "--data-dir", str(tmp_path), "migrate"]) == 0
    assert "schema v" in capsys.readouterr().out
    with acquire_instance_lock(tmp_path):
        assert main(["db", "--data-dir", str(tmp_path), "migrate"]) == 1
    assert "mind service is running" in capsys.readouterr().out


# -- the data dir (liveops I1, M1, M8) ---------------------------------------------------------

def _ready_dir(path: Path, port: int = 9) -> Path:
    """A data dir `run` accepts: mind.toml and a mind.db with the test cast's personas."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "mind.toml").write_text(toml(port), encoding="utf-8")
    seed_db(path / "mind.db", FakeClock(START))
    return path


@pytest.mark.parametrize("missing", ["everything", "mind.toml", "mind.db", "personas"])
def test_run_and_doctor_refuse_a_data_dir_that_is_not_a_mind(tmp_path, capsys, monkeypatch,
                                                              missing):
    """A worktree's own mind-data (or a typo) must never become a new, empty mind: the
    backlog would drain into it and the guild would be silent."""
    import leeroy_mind.__main__ as cli
    served, llms = [], []

    async def no_service(root, **kw):
        served.append(root)
        return 0
    monkeypatch.setattr(cli, "serve", no_service)
    root = tmp_path / "mind-data"
    if missing != "everything":
        _ready_dir(root)
        if missing == "personas":
            conn = sqlite3.connect(root / "mind.db")
            conn.execute("DELETE FROM persona_core")
            conn.commit()
            conn.close()
        else:
            (root / missing).unlink()
    def files():  # a read-only look at a WAL database may leave its -shm and -wal beside it
        return (sorted(p.name for p in root.iterdir() if not p.name.endswith(("-shm", "-wal")))
                if root.exists() else None)
    before = files()
    for argv in (["run", "--data-dir", str(root)], ["doctor", "--data-dir", str(root)]):
        assert cli.main(argv, make_llm=lambda cfg: llms.append(cfg) or FakeLLM()) == 1, argv
        out = capsys.readouterr()
        assert str(root) in out.out + out.err, argv  # the resolved path, so it can be fixed
        assert "--data-dir" in out.out + out.err
    assert served == [] and llms == []  # neither started: no service, no Ollama call
    assert files() == before  # nothing created: no mind.db, no lock, no logs, no snapshot


def test_run_names_the_default_data_dir_it_refuses(tmp_path, capsys, monkeypatch):
    import leeroy_mind.__main__ as cli
    monkeypatch.setattr(cli, "serve", lambda root, **kw: pytest.fail("served"))
    empty = tmp_path / "default-mind-data"
    monkeypatch.setenv("LEEROY_MIND_DATA", str(empty))
    assert cli.main(["run"]) == 1
    out = capsys.readouterr()
    assert str(empty) in out.err and not empty.exists()


def test_run_prints_a_start_line_with_the_data_dir(tmp_path, capsys, monkeypatch):
    import leeroy_mind.__main__ as cli
    served = []

    async def no_service(root, **kw):
        served.append(root)
        return 0
    monkeypatch.setattr(cli, "serve", no_service)
    root = _ready_dir(tmp_path / "mind-data")
    assert cli.main(["run", "--data-dir", str(root)]) == 0 and served == [root]
    out = capsys.readouterr().out
    assert str(root) in out and "5 personas" in out


def test_db_takes_the_data_dir_after_the_action_too(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    root = tmp_path / "mind-data"
    root.mkdir()
    seed_db(root / "mind.db", FakeClock(START))
    assert main(["db", "migrate", "--data-dir", str(root)]) == 0
    assert main(["db", "snapshot", "--label", "x", "--data-dir", str(root)]) == 0
    assert len(list((root / "snapshots").glob("mind-x-*.db"))) == 1
    assert main(["db", "--data-dir", str(root), "migrate"]) == 0  # before it, as before
    capsys.readouterr()


def test_db_migrate_never_creates_a_mind_db(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    root = tmp_path / "typo"
    assert main(["db", "migrate", "--data-dir", str(root)]) == 1
    assert str(root) in capsys.readouterr().err and not root.exists()


def test_the_cli_help_explains_the_data_dir(capsys):
    from leeroy_mind.__main__ import main
    for cmd in ("run", "doctor", "db", "init"):
        with pytest.raises(SystemExit):
            main([cmd, "--help"])
        out = capsys.readouterr().out
        assert "--data-dir" in out and "LEEROY_MIND_DATA" in out, cmd


def test_mind_started_says_where_it_runs_from(tmp_path, clock, caplog):
    """Liveops I1/M5: the log tells which data dir and which code a run uses."""
    async def main():
        async with running(tmp_path, clock, events=[hello(0), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    started = next(r.getMessage() for r in caplog.records
                   if r.getMessage().startswith("mind_started"))
    import leeroy_mind
    assert str(tmp_path) in started and "personas=5" in started
    assert str(Path(leeroy_mind.__file__).resolve().parent) in started


def test_a_first_start_without_meta_takes_the_guild_master_as_the_human(tmp_path, clock):
    """Liveops M3: the live mind.db starts with an empty meta. Until a replayed login named
    him, his replayed guild lines were stored as game chat and his whispers with strangers
    (C27) were kept. The roster's guild master is the human until the bridge says otherwise."""
    async def main():
        old = {"ts": 1_000}
        events = [hello(0), CAST, chat("evening all", **old),
                  chat("u selling that ore?", channel="whisper", to_guid=999, **old)]
        async with running(tmp_path, clock, events=events) as h:
            assert h.app.human_guid == HUMAN and h.app.store.get_meta("human_guid") is None
            await h.wait(lambda: h.count("chat_line") >= 1)
            await h.settle()
            rows = h.rows("SELECT origin, text FROM chat_line")
            assert [(r["origin"], r["text"]) for r in rows] == [("human", "evening all")]
    run(main())


def test_replayed_lines_are_threaded_at_their_own_time(tmp_path, clock):
    """Correctness M2: his lines typed while the mind was down, minutes apart, are separate
    conversations, not one thread stamped at replay time."""
    async def main():
        events = [{**hello(HUMAN), "ts": T_HELLO}, {**CAST, "ts": T_HELLO},
                  chat("first thing tonight", ts=T_HELLO - 1_200_000),
                  chat("much later", ts=T_HELLO - 600_000)]
        async with running(tmp_path, clock, events=events) as h:
            await h.wait(lambda: h.count("chat_line") == 2)
            await h.settle()
            threads = [r[0] for r in h.rows("SELECT thread_id FROM chat_line ORDER BY id")]
            assert None not in threads and threads[0] != threads[1]
            await h.advance(2)  # the next tick finds both silent: they go to extraction
            assert h.count("chat_thread", "WHERE status = 'open'") == 0
    run(main())


@pytest.mark.parametrize("argv", [
    ["personas", "import"], ["personas", "import", "--dry-run"], ["personas", "card", "101"],
    ["personas", "mix"], ["personas", "export"], ["personas", "rebalance-stances"],
    ["roster", "--file", "roster.json"]])
def test_writer_commands_wait_for_the_service_to_stop(tmp_path, capsys, argv):
    """Privacy minor 5, spec minor 8: the running mind is the only writer of mind.db, so the
    personas and roster commands refuse while it holds mind.lock, as relink-human does."""
    from leeroy_mind.__main__ import main
    root = _ready_dir(tmp_path / "mind-data")
    (tmp_path / "roster.json").write_text("[]", encoding="utf-8")
    argv = [a.replace("roster.json", str(tmp_path / "roster.json")) for a in argv]
    before = (root / "mind.db").stat().st_mtime_ns
    with acquire_instance_lock(root):
        assert main([argv[0], "--data-dir", str(root), *argv[1:]]) == 1
    assert "mind service is running" in capsys.readouterr().out
    assert (root / "mind.db").stat().st_mtime_ns == before
    assert not (root / "personas").exists() or not list((root / "personas").iterdir())
    main([argv[0], "--data-dir", str(root), *argv[1:]])  # free again: it runs
    assert "mind service is running" not in capsys.readouterr().out


def test_a_loop_that_dies_stops_the_app_and_fails_run(tmp_path, clock, caplog):
    """Tests lens M4: a loop of the app's own that ends by itself is a bug."""
    async def main():
        bridge = FakeBridge([hello(HUMAN), CAST])
        port = await bridge.start()
        (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
        seed_db(tmp_path / "mind.db", clock)
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path,
                      llm=FakeLLM(talk_and_extract, models={ORION}), clock=clock)
        ready = asyncio.Event()

        async def dies(queue):
            await ready.wait()
            raise ValueError("the extraction worker broke")
        app.extractor.run = dies
        task = asyncio.create_task(app.run())
        try:
            async def session_open():
                while app.session_id is None:
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(session_open(), 3.0)
            ready.set()
            with pytest.raises(RuntimeError, match="extraction loop stopped"):
                await asyncio.wait_for(task, 10.0)
            assert not app.running
        finally:
            await bridge.stop()
        with contextlib.closing(reopen(tmp_path)) as c:
            assert c.execute("SELECT count(*) FROM session WHERE ended IS NULL").fetchone()[0] == 0
    with caplog.at_level(logging.ERROR, logger="leeroy_mind"):
        run(main())
    assert "loop_stopped" in caplog.text


def test_a_hung_model_list_counts_as_the_model_away(tmp_path, clock, caplog, monkeypatch):
    """Tests lens M9: LIST_MODELS_S bounds a list_models that never answers."""
    import leeroy_mind.app as app_mod
    monkeypatch.setattr(app_mod, "LIST_MODELS_S", 0.1)

    class Hung(FakeLLM):
        async def list_models(self):
            await asyncio.Event().wait()

    async def main():
        seed_db(tmp_path / "mind.db", clock)
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=Hung(models={ORION}),
                      clock=clock)
        try:
            assert await asyncio.wait_for(app.check_model(), 3.0) is False
            assert not app.model_ready and app.models_seen is None
        finally:
            await app.stop()
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(main())
    assert "ollama_unavailable" in caplog.text


def test_a_task_that_ignores_its_cancel_holds_stop_only_so_long(tmp_path, clock, caplog,
                                                                 monkeypatch):
    """Tests lens M9: CANCEL_WAIT_S bounds each wait for a cancelled task."""
    import leeroy_mind.app as app_mod
    monkeypatch.setattr(app_mod, "CANCEL_WAIT_S", 0.1)
    release = asyncio.Event()

    def prepare(app):
        async def stubborn():
            while not release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    pass  # refuses to stop
        app.register_background(stubborn)

    async def main():
        async with running(tmp_path, clock, events=[hello(0), CAST], prepare=prepare) as h:
            await h.wait(lambda: h.app.world.online)
            try:
                await asyncio.wait_for(h.app.stop(), 3.0)
            finally:
                release.set()  # the task ends on its own, so nothing is left behind
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(main())
    assert "task_ignored_cancel" in caplog.text


def test_personas_import_screens_with_the_humans_identity(tmp_path, monkeypatch):
    """Part C: import_personas screens the real name and romance aimed at a member when the
    CLI gives it the human's identity."""
    import leeroy_mind.personas.io as io_mod
    from leeroy_mind.__main__ import main
    root = _ready_dir(tmp_path / "mind-data")
    seen = []

    def fake(store, in_dir, *, ts, dry_run=False, human=None):
        seen.append(human)
        return io_mod.ImportReport()
    monkeypatch.setattr(io_mod, "import_personas", fake)
    assert main(["personas", "--data-dir", str(root), "import", "--dry-run"]) == 0
    assert [(h.guid, h.nickname, h.real_name) for h in seen] == [(HUMAN, NICK, REAL)]


def test_personas_export_reports_an_unknown_guid(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    root = _ready_dir(tmp_path / "mind-data")
    assert main(["personas", "--data-dir", str(root), "export", "--guid", "999"]) == 1
    assert "no persona for guid 999" in capsys.readouterr().err
    assert not (root / "personas" / "999.json").exists()
    assert main(["personas", "--data-dir", str(root), "export", "--guid", "101"]) == 0
    assert (root / "personas" / "101.json").exists()


def test_a_card_prints_on_a_cp1252_console(tmp_path, monkeypatch):
    """A card may hold what an old Windows console cannot show; printing it must not crash."""
    import io
    import sys
    from leeroy_mind.__main__ import main
    root = _ready_dir(tmp_path / "mind-data")
    c = sqlite3.connect(root / "mind.db")
    c.execute("UPDATE persona_core SET card = card || ' ☕' WHERE guid = 101")
    c.commit()
    c.close()
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", out)
    assert main(["personas", "--data-dir", str(root), "card", "101"]) == 0
    out.flush()
    assert b"\\u2615" in raw.getvalue() and b"tokens]" in raw.getvalue()


# -- the library (fix wave part E) -------------------------------------------------------------

class _Source:
    def __init__(self, name, chunks):
        self.name, self.chunks = name, chunks

    def load(self, library):
        return SourceResult(self.name, list(self.chunks))


def make_library(path: Path, clock) -> Path:
    """A small library.db of invented chunks, as the library CLI builds one."""
    lib = Library(path, clock=clock)
    try:
        lib.build([
            _Source("world", [ChunkDraft(
                "quest:9001", "quest", "The Lancer's Trial", "Quest The Lancer's Trial (level "
                "80, Tournament, daily). Starts with Marshal Brightlance. Objective: Joust three "
                "champions.", section="Tournament")]),
            _Source("wiki", [ChunkDraft(
                "wiki:77:0", "zone", "Lancer Grounds", "The Lancer Grounds are east of the Frost "
                "Peak in Icecrown, where the Lancer's Trial dailies start.", license="CC BY-SA "
                "4.0", url="https://warcraft.wiki.gg/wiki/Lancer_Grounds")])])
    finally:
        lib.close()
    return path


def _library_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith(("library_unavailable", "library_opened"))]


def test_the_app_opens_the_library_read_only_and_closes_it(tmp_path, clock, caplog):
    async def main():
        make_library(tmp_path / "library.db", clock)
        cfg = load_config(tmp_path / "mind.toml")
        app = MindApp(cfg, tmp_path, llm=FakeLLM(), clock=clock)
        try:
            lib = app.library
            assert isinstance(lib, Library) and lib.readonly and lib.llm is app.scheduler
            assert lib.embed_model == cfg.models.embedding
            assert app.engine.facts is not None and app.engine.facts.library is lib
            assert [c.title for c in lib.search_text("Lancer Grounds", k=1)] == ["Lancer Grounds"]
            with pytest.raises(sqlite3.OperationalError):
                lib.set_meta("x", "y")
        finally:
            await app.stop()
        with pytest.raises(sqlite3.ProgrammingError):
            lib.conn.execute("SELECT 1")  # closed with the app
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    logs = _library_logs(caplog)
    assert len(logs) == 1 and logs[0].startswith("library_opened")


@pytest.mark.parametrize("what", ["missing", "garbage"])
def test_the_app_runs_without_a_library_it_cannot_open(tmp_path, clock, caplog, what):
    path = tmp_path / "library.db"
    if what == "garbage":
        path.write_bytes(b"not a database at all " * 64)
    before = path.read_bytes() if path.exists() else None

    async def main():
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
        try:
            assert app.library is None and app.engine.facts is None
        finally:
            await app.stop()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    logs = _library_logs(caplog)
    assert len(logs) == 1 and logs[0].startswith("library_unavailable")  # once
    assert next(r for r in caplog.records if r.getMessage() == logs[0]).levelno == logging.WARNING
    assert (path.read_bytes() if path.exists() else None) == before  # never created or changed


def test_his_game_question_is_answered_with_the_librarys_facts(tmp_path, clock):
    async def main():
        make_library(tmp_path / "library.db", clock)
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
            await h.bridge.push(chat("anyone know where the lancer's trial dailies start?"))
            await h.wait(lambda: h.count("chat_line") >= 1)
            await h.advance(40)
            talk = [c["messages"][-1]["content"] for c in h.llm.calls if "messages" in c
                    and SCHEMA_MARK not in c["messages"][-1]["content"]]
            # the first answer has the facts; a later one is told it's answered (fix wave G1)
            assert talk and "Facts you know" in talk[0]
            assert "Starts with Marshal Brightlance." in talk[0]
            assert all("Facts you know" in p or "already answered that" in p for p in talk)
            assert not any("http" in p for p in talk)
            assert h.says()
    run(main())
