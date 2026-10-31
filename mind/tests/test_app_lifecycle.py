"""The mind service's lifecycle (fix wave part F): replayed events at their own time, the
offline hooks around shutdown, the login reload, the stats line, the start banner and the
health loop.

Like test_app.py, the app runs against FakeBridge (real sockets on 127.0.0.1) and a FakeLLM,
with a FakeClock that yields instead of sleeping."""
import asyncio
import contextlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from conftest import HUMAN, run, seed_test_cast
from leeroy_mind.app import MindApp, startup_checks
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Chat, Hello, Login, Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_bridge import FakeBridge, load_fixture
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM

START = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)  # night "2026-10-29"
T_HELLO = int(START.timestamp() * 1000)  # the server's ms clock when it greets
DAY_MS = 86_400_000
LIVE = load_config(Path("missing.toml")).models.live
CAST = load_fixture(Path(__file__).parent / "fixtures" / "session_basic.jsonl")[1]
HUMAN_ROW = Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0, level=80, rank=0,
                   online=False, map=0, zone=0, spec_tab=-1)


def hello(human_guid: int = 0, **extra) -> dict:
    return {"type": "hello", "server": "mod-leeroy", "version": "0.1.0", "guild_id": 21,
            "human_guid": human_guid, **extra}


def chat(text: str, *, from_guid: int = HUMAN, from_name: str = "Testmage", **extra) -> dict:
    return {"type": "chat", "channel": "guild", "from_guid": from_guid, "from_name": from_name,
            "to_guid": 0, "text": text, "map": 0, "zone": 1519, **extra}


def presence(kind: str, **extra) -> dict:
    return {"type": kind, "guid": HUMAN, "human": True, **extra}


def boss(state: str, **extra) -> dict:  # Lord Marrowgar, a raid fight
    return {"type": "boss", "map": 631, "instance_id": 5, "boss_id": 0, "boss_name": "",
            "state": state, "members": [HUMAN, 101, 102], **extra}


def toml(port: int, **human) -> str:
    extra = "".join(f'{k} = "{v}"\n' for k, v in human.items())
    return f'[bridge]\nport = {port}\n\n[human]\nnickname = "Zan"\npronouns = "he"\n{extra}'


def reply(model: str, messages: list[dict]) -> str:
    if "Reply with only a JSON object matching this schema" in messages[-1]["content"]:
        return json.dumps({"memories": [], "overheard": None})  # an extraction: nothing kept
    return "evening, rough one?"


class App:
    def __init__(self, app: MindApp, bridge: FakeBridge, llm: FakeLLM, clock: FakeClock):
        self.app, self.bridge, self.llm, self.clock = app, bridge, llm, clock

    async def wait(self, pred, timeout: float = 3.0) -> None:
        async def poll():
            while not pred():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), timeout)

    async def advance(self, seconds: float, step: float = 0.5) -> None:
        await self.clock.advance(seconds, step)
        for _ in range(3):
            await asyncio.sleep(0.01)

    async def until(self, pred, timeout: float = 5.0, step: float = 0.5) -> None:
        """Advance the clock a step at a time (the reconnect backoff runs on it) until pred()."""
        end = asyncio.get_running_loop().time() + timeout
        while not pred():
            assert asyncio.get_running_loop().time() < end, "the condition never held"
            await self.clock.advance(step, step)
            await asyncio.sleep(0.005)

    def rows(self, sql: str, params=()) -> list:
        conn = self.app.store.conn
        out = conn.execute(sql, params).fetchall()
        conn.commit()
        return out

    def count(self, table: str, where: str = "", params=()) -> int:
        return self.rows(f"SELECT count(*) FROM {table} {where}", params)[0][0]

    def says(self) -> list[dict]:
        return [c for c in self.bridge.commands if c["type"] == "say"]


def seed_db(path: Path, clock: FakeClock) -> None:
    s = Store(open_db(path), clock=clock)
    try:
        seed_test_cast(s)
        s.upsert_cast([HUMAN_ROW], 1000.0)
    finally:
        s.close()


@contextlib.asynccontextmanager
async def mind(tmp_path: Path, clock: FakeClock, *, events, llm: FakeLLM | None = None,
               prepare=None, start: bool = True, bridge_up: bool = True,
               ollama_conf: Path | None = None):
    """The app over FakeBridge; with ollama_conf, `run`'s startup checks first (they read
    that mod-ollama-chat conf, never the server's)."""
    bridge = FakeBridge(events)
    port = await bridge.start()
    if not bridge_up:
        await bridge.go_down()  # the worldserver is not there: connections are refused
    (tmp_path / "mind.toml").write_text(toml(port), encoding="utf-8")
    seed_db(tmp_path / "mind.db", clock)
    llm = llm or FakeLLM(reply, models={LIVE})
    app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=llm, clock=clock,
                  rng_seed=1)
    if prepare is not None:
        prepare(app)
    if ollama_conf is not None:
        await startup_checks(app, ollama_conf=ollama_conf)
    task = asyncio.create_task(app.run()) if start else None
    try:
        yield App(app, bridge, llm, clock)
    finally:
        await app.stop()
        if task is not None:
            done, _ = await asyncio.wait({task}, timeout=5.0)
            assert task in done and not task.cancelled() and task.exception() is None
        await bridge.stop()


def fake_clock() -> FakeClock:
    return FakeClock(START, yields=64)


def reopen(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{(tmp_path / 'mind.db').as_uri()}?mode=ro", uri=True)


# -- item 1: a replayed event happened when the server says (correctness M2) ------------------

def test_a_raid_replayed_the_next_evening_stays_on_its_own_night(tmp_path):
    """A crash mid-raid, then a restart the next evening: the wipes the bridge replays count
    for their own night, the boss memory is that night's, and the rows keep their own time."""
    clock = fake_clock()

    async def main():
        then = T_HELLO - DAY_MS  # yesterday, 20:00
        events = [{**hello(0), "ts": T_HELLO}, {**CAST, "ts": T_HELLO},
                  boss("engaged", ts=then - 90_000), boss("wiped", ts=then)]
        async with mind(tmp_path, clock, events=events) as h:
            await h.wait(lambda: h.count("memory", "WHERE owner = 0") == 1)
            ts = [r[0] for r in h.rows("SELECT ts FROM game_event ORDER BY id")]
            assert ts == [(then - 90_000) / 1000, then / 1000]  # not the replay's time
            store = h.app.store
            assert store.get_meta("wipes:631:0:2026-10-28") == "1"
            assert store.get_meta("wipes:631:0:2026-10-29") is None  # tonight: no wipe yet
            assert h.app.event_rules.wipes_tonight(631, 0) == 0
            m = h.rows("SELECT key, t_event FROM memory WHERE owner = 0")[0]
            assert (m["key"], m["t_event"]) == ("boss:631:0:2026-10-28", then // 1000)
    run(main())


def test_his_replayed_lines_keep_the_time_he_typed_them(tmp_path):
    """His lines from hours apart, typed while the mind was down: each is stored at its own
    time (what extraction dates its memories by) and they are separate threads."""
    clock = fake_clock()

    async def main():
        three, one = T_HELLO - 3 * 3_600_000, T_HELLO - 3_600_000
        events = [{**hello(0), "ts": T_HELLO}, {**CAST, "ts": T_HELLO},
                  presence("login", ts=three - 60_000), chat("anyone up for heroics", ts=three),
                  chat("guess not, night all", ts=one), presence("logout", ts=one + 5_000)]
        async with mind(tmp_path, clock, events=events) as h:
            await h.wait(lambda: h.count("chat_line") == 2 and h.count("game_event") == 2)
            lines = h.rows("SELECT ts, thread_id FROM chat_line ORDER BY id")
            assert [r["ts"] for r in lines] == [three / 1000, one / 1000]
            assert lines[0]["thread_id"] != lines[1]["thread_id"]
            assert [r[0] for r in h.rows("SELECT ts FROM game_event ORDER BY id")] == [
                (three - 60_000) / 1000, (one + 5_000) / 1000]
            assert h.says() == []  # still only recorded (ruling 5)
    run(main())


def test_what_arrives_while_stopping_keeps_its_own_time_too(tmp_path):
    clock = fake_clock()

    async def main():
        async with mind(tmp_path, clock, events=[], start=False) as h:
            await h.app.handle(Hello(ts=T_HELLO, server="mod-leeroy", version="0.1.0",
                                     guild_id=21, human_guid=0))
            then = T_HELLO - 3_600_000
            h.app._record_only(Chat(ts=then, channel="guild", from_guid=HUMAN,
                                    from_name="Testmage", to_guid=0, text="anyone on", map=0,
                                    zone=0), "chat")
            h.app._record_only(Login(ts=then + 1000, guid=HUMAN, human=True), "login")
            assert h.rows("SELECT ts FROM chat_line")[0][0] == then / 1000
            assert h.rows("SELECT ts FROM game_event")[0][0] == then / 1000 + 1
    run(main())


def test_live_events_are_stamped_now(tmp_path):
    """Positive control: an event that was not held by the bridge keeps the mind's clock."""
    clock = fake_clock()

    async def main():
        async with mind(tmp_path, clock, events=[hello(HUMAN), CAST]) as h:
            await h.wait(lambda: h.app.session_id is not None)
            await h.advance(30)
            await h.bridge.push(chat("evening all"))
            await h.bridge.push(boss("engaged"))
            await h.wait(lambda: h.count("chat_line", "WHERE origin = 'human'") == 1
                         and h.count("game_event") == 1)
            assert h.rows("SELECT ts FROM chat_line WHERE origin = 'human'")[0][0] == \
                clock.now()
            assert h.rows("SELECT ts FROM game_event")[0][0] == clock.now()
    run(main())


# -- item 4: the offline hooks and shutdown (correctness M7, Task 17 re-review N1) -----------

def test_a_logout_as_the_mind_stops_starts_no_offline_hooks(tmp_path, caplog):
    """His logout, then Ctrl+C while the session is still ending (the engine waits up to the
    5 s ack timeout): the offline hooks (P1c's night job) never start in the middle of the
    shutdown, and nothing of theirs is left running when it is over."""
    clock = fake_clock()
    ran, entered, gate = [], asyncio.Event(), asyncio.Event()

    def prepare(app):
        async def hook():
            ran.append(app._stopping)
        app.register_offline_hook(hook)
        real = app.engine.end_session

        async def slow():
            entered.set()
            await gate.wait()
            await real()
        app.engine.end_session = slow

    async def main():
        events = [hello(0), CAST, presence("login")]
        async with mind(tmp_path, clock, events=events, prepare=prepare) as h:
            await h.wait(lambda: h.app.session_id is not None)
            await h.bridge.push(presence("logout"))
            await asyncio.wait_for(entered.wait(), 3.0)  # the session is ending
            stopping = asyncio.create_task(h.app.stop())
            await asyncio.sleep(0.05)
            gate.set()
            await asyncio.wait_for(stopping, 10.0)
            assert ran == [] and not h.app._offline_tasks
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    assert "offline_hooks_skipped: reason=stopping" in caplog.text
    with contextlib.closing(reopen(tmp_path)) as c:  # the session still ended
        assert c.execute("SELECT count(*) FROM session WHERE ended IS NULL").fetchone()[0] == 0


def test_offline_hooks_added_after_shutdown_began_are_cancelled_before_the_db_closes(
        tmp_path):
    clock = fake_clock()
    cancelled: list[bool] = []

    def prepare(app):
        real = app.engine.end_session

        async def end_session():
            await real()
            if app._stopping and not cancelled:  # shutdown's own step, after its first one
                async def late():
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.append(app._closed)
                        raise
                task = asyncio.get_running_loop().create_task(late())
                app._offline_tasks.add(task)
                task.add_done_callback(app._offline_tasks.discard)
        app.engine.end_session = end_session

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST], prepare=prepare) as h:
            await h.wait(lambda: h.app.world.online)
            await asyncio.wait_for(h.app.stop(), 10.0)
            assert cancelled == [False]  # cancelled while the store was still open
            assert not h.app._offline_tasks
    run(main())


def test_a_quick_relog_skips_the_offline_hooks(tmp_path, caplog):
    """The hooks wait (OFFLINE_WAIT_S at most) for the session's last writes. If he is back by
    then, a new session is open, and a night job must not start: they are skipped, logged."""
    clock = fake_clock()
    ran, gate = [], asyncio.Event()

    def prepare(app):
        async def hook():
            ran.append(app.session_id)
        app.register_offline_hook(hook)
        real = app.extractor.extract

        async def gated(t):  # his thread's extraction holds the hooks back
            await gate.wait()
            return await real(t)
        app.extractor.extract = gated

    async def main():
        events = [hello(0), CAST, presence("login"), chat("evening all")]
        async with mind(tmp_path, clock, events=events, prepare=prepare) as h:
            await h.wait(lambda: h.count("chat_line", "WHERE origin = 'human'") == 1)
            await h.advance(5)
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: h.app.store.open_session() is None)
            await h.bridge.push(presence("login"))
            await h.wait(lambda: h.app.session_id is not None)
            gate.set()
            await h.wait(lambda: "offline_hooks_skipped" in caplog.text)
            for _ in range(3):
                await asyncio.sleep(0.01)
            assert ran == []
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    assert "offline_hooks_skipped: reason=new_session" in caplog.text


def test_two_sessions_ending_close_together_run_the_hooks_once(tmp_path, caplog):
    """Part F review, Minor 2: logout, relog, logout within OFFLINE_WAIT_S. Both sessions'
    waits end together; the hooks run once, for the session that ended last."""
    clock = fake_clock()
    ran, gate = [], asyncio.Event()

    def prepare(app):
        async def hook():
            ran.append(app.session_id)
        app.register_offline_hook(hook)
        real = app.extractor.extract

        async def gated(t):  # the first session's thread holds both sessions' hooks back
            await gate.wait()
            return await real(t)
        app.extractor.extract = gated

    async def main():
        events = [hello(0), CAST, presence("login"), chat("evening all")]
        async with mind(tmp_path, clock, events=events, prepare=prepare) as h:
            await h.wait(lambda: h.count("chat_line", "WHERE origin = 'human'") == 1)
            await h.advance(5)
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: h.app.store.open_session() is None)
            await h.bridge.push(presence("login"))
            await h.wait(lambda: h.app.session_id is not None)
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: h.app.store.open_session() is None)
            assert len(h.app._offline_tasks) == 2 and ran == []  # both still waiting
            gate.set()
            await h.wait(lambda: not h.app._offline_tasks)
            assert ran == [None]  # once
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    skipped, = _fields(caplog, "offline_hooks_skipped")
    assert (skipped["reason"], skipped["session"], skipped["newer"]) == ("newer_session", 1, 2)


def test_offline_hook_runs_never_overlap(tmp_path):
    """A night job still running when the next quiet period begins: the next run waits for it
    to end instead of running beside it."""
    clock = fake_clock()
    seen, hold = [], asyncio.Event()

    def prepare(app):
        async def hook():
            seen.append("start")
            if len(seen) == 1:
                await hold.wait()  # the first run is a long job
            seen.append("end")
        app.register_offline_hook(hook)

    async def main():
        events = [hello(0), CAST, presence("login")]
        async with mind(tmp_path, clock, events=events, prepare=prepare) as h:
            await h.wait(lambda: h.app.session_id is not None)
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: seen == ["start"])  # the first quiet period's run
            await h.bridge.push(presence("login"))
            await h.wait(lambda: h.app.session_id is not None)
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: h.app.store.open_session() is None)
            for _ in range(20):
                await asyncio.sleep(0.01)
            assert seen == ["start"]  # the second quiet period waits for the first run
            hold.set()
            await h.wait(lambda: len(seen) == 4)
            assert seen == ["start", "end", "start", "end"]
    run(main())


# -- item 5: his login re-reads mind.toml, and the latch follows (Task 17 re-review N3) -------

def test_a_login_checks_the_live_model_the_reloaded_config_names(tmp_path, caplog):
    """A live model in mind.toml that is not pulled silences the guild at his login, not at the
    next health check up to a minute later (the greetings would go out meanwhile)."""
    clock = fake_clock()

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST]) as h:
            await h.wait(lambda: h.app.world.online and h.app.model_ready)
            port = h.app.cfg.bridge.port
            (tmp_path / "mind.toml").write_text(
                toml(port) + '\n[models]\nlive = "next-live:1b"\n', encoding="utf-8")
            await h.bridge.push(presence("login"))
            await h.wait(lambda: _count(caplog, "session_started") == 1)
            assert h.app.cfg.models.live == "next-live:1b" and not h.app.model_ready
            await h.advance(30)  # the greetings, well before the next health check
            assert h.says() == []
            assert h.count("utterance", "WHERE kind = 'greet' AND dropped = 'model_away'") >= 1
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    assert "ollama_unavailable" in caplog.text and "next-live:1b" in caplog.text


class Listing(FakeLLM):
    """Counts list_models (the latch's check) and, once armed, holds it until released, as a
    slow /api/tags would."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lists, self.armed = 0, False
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def list_models(self):
        self.lists += 1
        if self.armed:
            self.entered.set()
            await self.release.wait()
        return await super().list_models()


def test_a_relog_inside_the_model_check_still_skips_the_old_sessions_hooks(tmp_path, caplog):
    """Part F review, Important 1: his relog's model check waits on Ollama. The new session
    is open before it does, so the old session's offline hooks, whose waits end meanwhile,
    see him back and are skipped."""
    clock = fake_clock()
    ran, gate = [], asyncio.Event()
    llm = Listing(reply, models={LIVE, "next-live:1b"})

    def prepare(app):
        async def hook():
            ran.append(app.session_id)
        app.register_offline_hook(hook)
        real = app.extractor.extract

        async def gated(t):  # his thread's extraction holds the old session's hooks back
            await gate.wait()
            return await real(t)
        app.extractor.extract = gated

    async def main():
        events = [hello(0), CAST, presence("login"), chat("evening all")]
        async with mind(tmp_path, clock, events=events, llm=llm, prepare=prepare) as h:
            await h.wait(lambda: h.count("chat_line", "WHERE origin = 'human'") == 1)
            await h.advance(5)
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: h.app.store.open_session() is None)
            port = h.app.cfg.bridge.port  # his relog reloads another live model: a check
            (tmp_path / "mind.toml").write_text(
                toml(port) + '\n[models]\nlive = "next-live:1b"\n', encoding="utf-8")
            llm.armed = True
            await h.bridge.push(presence("login"))
            await asyncio.wait_for(llm.entered.wait(), 3.0)  # his login waits in the check
            gate.set()  # the old session's extraction ends meanwhile
            for _ in range(30):
                await asyncio.sleep(0.01)
            assert ran == [] and h.app.session_id is not None
            llm.release.set()
            await h.wait(lambda: _count(caplog, "session_started") == 2)
            for _ in range(5):
                await asyncio.sleep(0.01)
            assert ran == [] and h.app.model_ready
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    assert "offline_hooks_skipped: reason=new_session" in caplog.text


def test_a_login_checks_the_model_only_when_the_reload_changed_it(tmp_path, caplog):
    """Part F review, Minor 1: the check holds the dispatcher (Ollama may be slow), so a login
    that keeps the live model does not repeat the health loop's check."""
    clock = fake_clock()
    llm = Listing(reply, models={LIVE, "next-live:1b"})

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST], llm=llm) as h:
            await h.wait(lambda: h.app.world.online and h.app.model_ready)
            before = llm.lists
            await h.bridge.push(presence("login"))
            await h.wait(lambda: _count(caplog, "session_started") == 1)
            assert llm.lists == before  # the same model: the health loop's verdict holds
            await h.bridge.push(presence("logout"))
            await h.wait(lambda: h.app.store.open_session() is None)
            port = h.app.cfg.bridge.port
            (tmp_path / "mind.toml").write_text(
                toml(port) + '\n[models]\nlive = "next-live:1b"\n', encoding="utf-8")
            await h.bridge.push(presence("login"))
            await h.wait(lambda: _count(caplog, "session_started") == 2)
            assert llm.lists == before + 1 and h.app.model_ready  # changed: checked at once
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())


# -- item 6: the stats line, the code version and the start banner (liveops M5) --------------

def _fields(caplog, event: str) -> list[dict]:
    return [r.leeroy_fields for r in caplog.records if getattr(r, "leeroy_event", "") == event]


def test_a_stats_line_every_15_minutes_counts_talk_and_queues_never_text(tmp_path, caplog):
    """Replies to him are every kind that answers his lines (talk.select.plan_reply_turns:
    reply_human, and goodnight for his goodbye); his bot commands are counted apart from
    his talk (part F review, Minor 3)."""
    clock = fake_clock()

    def his(text: str, channel: str = "guild") -> Chat:
        return Chat(ts=0, channel=channel, from_guid=HUMAN, from_name="Testmage", to_guid=0,
                    text=text, map=0, zone=0)

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
            s, t = h.app.store, clock.now()
            for dt, guid, kind, text, dropped, llm in [
                    (1, 101, "reply_human", "hey you", None, 0.4),
                    (2, 101, "reply_human", "second line", None, 0.4),
                    (3, 102, "open", "so anyway", None, 1.2),
                    (4, 103, "open", "", "expired", 6.0),
                    (5, 104, "gz", "", "guards", None),
                    (8, 105, "goodnight", "night zan", None, 0.5),
                    (9, 102, "greet", "evening", None, 0.6)]:
                s.log_utterance(t + dt, guid, "guild", kind, text, [], dropped, None,
                                llm_s=llm)
            s.insert_chat_line(his("evening all"), session_id=None, origin="human", ts=t + 6)
            s.insert_chat_line(his("night all"), session_id=None, origin="human", ts=t + 7)
            s.insert_chat_line(his("follow", "party"), session_id=None, origin="human",
                               ts=t + 7.5)  # a bot command: stored, not talk
            await h.advance(899, step=5)
            assert _fields(caplog, "stats") == []
            await h.advance(2)
            assert len(_fields(caplog, "stats")) == 1
            await h.advance(900, step=5)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    first, second = _fields(caplog, "stats")  # it stops as the second ends: no empty third
    assert {k: first[k] for k in ("window_s", "said", "replies", "his_lines", "his_commands",
                                  "drops", "final")} == {
        "window_s": 900, "said": 5, "replies": 3, "his_lines": 2, "his_commands": 1,
        "drops": {"expired": 1, "guards": 1}, "final": False}
    assert (first["llm_p50_s"], first["llm_max_s"]) == (0.55, 6.0)
    assert set(first["queue"]) == {"llm", "extract", "memory", "bridge"}
    assert all(isinstance(v, int) for v in first["queue"].values())
    assert (second["said"], second["his_lines"], second["drops"], second["llm_p50_s"]) == (
        0, 0, {}, None)
    text = " ".join(r.getMessage() for r in caplog.records if "stats" in r.getMessage())
    assert not any(w in text for w in ("hey you", "evening all", "follow", "night zan"))


def test_the_window_still_open_when_the_mind_stops_gets_its_stats_line(tmp_path, caplog):
    """Dress rehearsal 2 (report 10): the stats loop dropped the window still open at
    shutdown, so the end of the night had no stats line. The stop logs it, marked final, after
    the pending turns are settled, and a mind that never ran logs none."""
    clock = fake_clock()

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
            s = h.app.store
            s.log_utterance(clock.now() + 1, 101, "guild", "reply_human", "hey you", [], None,
                            None, llm_s=0.4)
            await h.advance(902, step=5)
            s.log_utterance(clock.now() + 1, 102, "guild", "open", "so anyway", [], None, None,
                            llm_s=1.2)
            s.log_utterance(clock.now() + 2, 103, "guild", "open", "", [], "expired", None,
                            llm_s=6.0)
            await h.advance(300, step=5)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    full, last = _fields(caplog, "stats")
    assert (full["window_s"], full["said"], full["final"]) == (900, 1, False)
    assert (last["said"], last["drops"], last["final"]) == (1, {"expired": 1}, True)
    assert 300 <= last["window_s"] <= 310 and last["llm_max_s"] == 6.0
    started = [r for r in caplog.records if getattr(r, "leeroy_event", "") == "shutdown_started"]
    assert started and caplog.records.index(started[0]) < caplog.records.index(
        next(r for r in caplog.records if getattr(r, "leeroy_fields", {}).get("final")))
    text = " ".join(r.getMessage() for r in caplog.records if "stats" in r.getMessage())
    assert "stats:" in text and not any(w in text for w in ("hey you", "so anyway"))


def test_a_mind_stopped_before_it_ran_logs_no_stats(tmp_path, caplog):
    clock = fake_clock()

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST], start=False) as h:
            await h.app.stop()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    assert _fields(caplog, "stats") == []


def test_mind_started_carries_the_code_version(tmp_path, caplog):
    import importlib.metadata
    clock = fake_clock()

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    started, = _fields(caplog, "mind_started")
    assert started["version"].split("+")[0] == importlib.metadata.version("leeroy-mind")
    assert started["personas"] == 5 and started["data_dir"] == str(tmp_path)


def test_the_git_commit_is_read_from_a_checkout_and_a_worktree(tmp_path):
    from leeroy_mind.app import _git_commit
    main_repo, worktree = tmp_path / "main", tmp_path / "wt"
    git = main_repo / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text("a9f5ed2" + "0" * 33 + "\n", encoding="utf-8")
    assert _git_commit(main_repo) == "a9f5ed2"
    wt_git = git / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {wt_git.as_posix()}\n", encoding="utf-8")
    (wt_git / "HEAD").write_text("ref: refs/heads/fix-f\n", encoding="utf-8")
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    (git / "packed-refs").write_text("# pack-refs with: peeled fully-peeled sorted\n"
                                     "dc0aadf" + "1" * 33 + " refs/heads/fix-f\n",
                                     encoding="utf-8")
    assert _git_commit(worktree) == "dc0aadf"  # a packed ref in the common dir
    (wt_git / "HEAD").write_text("4d6bae9" + "2" * 33 + "\n", encoding="utf-8")
    assert _git_commit(worktree) == "4d6bae9"  # a detached head
    assert _git_commit(tmp_path / "nowhere") is None


def _banner_lines(out: list[str]) -> list[str]:
    return [line for line in "".join(out).splitlines() if line.startswith("mind running:")]


def test_the_start_banner_says_what_runs_once_the_bridge_has_greeted(tmp_path, capsys,
                                                                       caplog):
    clock = fake_clock()
    out: list[str] = []

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST]) as h:
            port = h.app.cfg.bridge.port
            await h.wait(lambda: out.append(capsys.readouterr().out) or _banner_lines(out))
            line, = _banner_lines(out)
            assert str(tmp_path) in line and "5 personas" in line and LIVE in line
            assert "(found)" in line and f"bridge 127.0.0.1:{port} connected" in line
            await h.bridge.drop_client()  # a reconnect is not another start
            await h.until(lambda: h.bridge.connections == 2 and h.app.bridge.greeted)
            await h.advance(15)
            out.append(capsys.readouterr().out)
            assert len(_banner_lines(out)) == 1
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    banner, = _fields(caplog, "banner")
    assert banner["connected"] is True and banner["personas"] == 5


def test_the_banner_says_so_when_the_bridge_is_not_there_after_10_s(tmp_path, capsys):
    clock = fake_clock()
    out: list[str] = []

    async def main():
        async with mind(tmp_path, clock, events=[], bridge_up=False,
                        llm=FakeLLM(reply)) as h:
            await h.advance(9)
            out.append(capsys.readouterr().out)
            assert _banner_lines(out) == []
            await h.advance(2)
            await h.wait(lambda: out.append(capsys.readouterr().out) or _banner_lines(out))
            line, = _banner_lines(out)
            assert "not connected yet" in line and "not found" in line  # no live model either
    run(main())


# -- item 7: the health loop reads mod-ollama-chat's conf and watches the drops (liveops M6) -

def _count(caplog, event: str) -> int:
    return len(_fields(caplog, event))


def test_the_health_loop_warns_once_when_mod_ollama_chat_changes_model(tmp_path, caplog):
    """The owner's `ollama reload` drill can move mod-ollama-chat to another model while the
    mind runs: two models then share the card and turns expire. Each change is warned about
    once, not at every check, and the match is logged when it comes back."""
    clock = fake_clock()
    conf = tmp_path / "mod_ollama_chat.conf"
    good = f'OllamaChat.Model = "{LIVE}"\nOllamaChat.NumCtx = 4096\n'
    conf.write_text(good, encoding="utf-8")

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST], ollama_conf=conf) as h:
            await h.wait(lambda: h.app.world.online)
            assert _count(caplog, "ollama_chat_mismatch") == 0
            conf.write_text(good.replace(LIVE, "qwen3:14b"), encoding="utf-8")
            await h.advance(61)
            assert _count(caplog, "ollama_chat_mismatch") == 1
            await h.advance(120)
            assert _count(caplog, "ollama_chat_mismatch") == 1  # once per change
            conf.write_text(good.replace("4096", "8192"), encoding="utf-8")
            await h.advance(60)
            assert _count(caplog, "ollama_chat_mismatch") == 2  # another change
            assert _count(caplog, "ollama_chat_matches") == 0
            conf.write_text(good, encoding="utf-8")
            await h.advance(60)
            assert _count(caplog, "ollama_chat_matches") == 1
            await h.advance(60)
            assert _count(caplog, "ollama_chat_mismatch") == 2
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(main())
    details = [f["detail"] for f in _fields(caplog, "ollama_chat_mismatch")]
    assert "qwen3:14b" in details[0] and "8192" in details[1]


def _drop(app: MindApp, at: float, reason: str) -> None:
    app.store.log_utterance(at, 101, "guild", "open", "", [], reason, None)


def test_the_health_loop_warns_when_model_drops_pile_up(tmp_path, caplog):
    """drops_high: 5 turns dropped as expired or llm_error within a minute mean the model is
    too slow or failing though it is listed; at most one warning every 10 minutes."""
    clock = fake_clock()

    async def main():
        async with mind(tmp_path, clock, events=[hello(0), CAST]) as h:
            await h.wait(lambda: h.app.world.online)
            t = clock.now()
            for i, reason in enumerate(["expired", "llm_error", "guards", "expired",
                                        "llm_error", "expired"]):
                _drop(h.app, t + 10 + i, reason)
            await h.advance(61)  # the check at 60 s
            warned = _fields(caplog, "drops_high")
            assert len(warned) == 1
            assert {k: warned[0][k] for k in ("drops", "expired", "llm_error")} == {
                "drops": 5, "expired": 3, "llm_error": 2}
            for i in range(5):
                _drop(h.app, t + 70 + i, "llm_error")
            await h.advance(60)
            assert _count(caplog, "drops_high") == 1  # not again within 10 minutes
            await h.advance(540)
            for i in range(5):
                _drop(h.app, clock.now() + i, "expired")
            await h.advance(60)
            assert _count(caplog, "drops_high") == 2
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(main())


def test_drops_spread_over_more_than_a_minute_or_of_other_kinds_do_not_warn(tmp_path, caplog):
    clock = fake_clock()

    async def main():
        async with mind(tmp_path, clock, events=[], start=False) as h:
            t = clock.now() - 100
            for dt in (0, 1, 2, 63, 64):  # 3, then 2 a minute later: never 5 within 60 s
                _drop(h.app, t + dt, "expired")
            for dt in (65, 66, 67, 68):
                _drop(h.app, t + dt, "guards")  # the loop guards' drops: not the model's
            h.app._check_drops(clock.now())
            assert _count(caplog, "drops_high") == 0
            for dt in (69, 70, 71):  # now 63, 64, 69, 70 and 71: 5 within a minute
                _drop(h.app, t + dt, "llm_error")
            h.app._check_drops(clock.now())
            assert _count(caplog, "drops_high") == 1
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(main())
