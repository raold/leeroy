"""The sandbox mind on a scratch copy: invented cast, FakeLLM, ephemeral ports, a fast fake clock."""
import asyncio
import hashlib
import json
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN, TEST_CAST, seed_test_cast
from engine_rig import save_member
from leeroy_mind.clock import SystemClock
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Member
from leeroy_mind.sandbox import prepare
from leeroy_mind.sandbox.api import Api
from leeroy_mind.sandbox.cli import Client, _logging, sandbox_main, sandbox_talk_main
from leeroy_mind.sandbox.runtime import Sandbox, SandboxLLM
from leeroy_mind.storage.db import checkpoint, open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.beat import MARK
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM

LIVE_TOML = """\
[talk]
chattiness = 4
[human]
nickname = "Zan"
real_name = "Quillon"
pronouns = "he"
[voice]
enabled = true
autostart = true
[selfies]
enabled = true
booth = true
[realm]
enabled = true
[signals]
enabled = true
[guards]
enabled = true
[claims]
enabled = true
[server]
enabled = true
[bakeoff]
capture = false
"""
ANSWER = "long day at work, you?"
BEAT = json.dumps({"read": "he asks about my day", "lands": "nice", "want": "share",
                   "use": None})


def script(model, messages):
    last = messages[-1].get("content", "")
    return BEAT if MARK in last else ANSWER


def make_live(tmp_path: Path) -> tuple[Path, Store]:
    """A live-looking data dir, its mind.db held open as a running mind holds it."""
    live = tmp_path / "live"
    live.mkdir()
    (live / "mind.toml").write_bytes(LIVE_TOML.encode())
    store = Store(open_db(live / "mind.db"), clock=SystemClock())
    seed_test_cast(store)
    for guid in TEST_CAST:
        save_member(store, guid, region="US", guild_role="officer" if guid == 102 else "raider")
    store.set_meta("human_guid", str(HUMAN))
    store.set_meta("human_name", "Testmage")
    store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0,
                              level=80, rank=0, online=False, map=0, zone=1519, spec_tab=-1)],
                      1000.0)
    for name in ("personas", "prompts", "realm"):
        (live / name).mkdir()
    (live / "personas" / "101.json").write_bytes(b"{}")
    (live / "prompts" / "note.txt").write_bytes(b"an overlay")
    store.conn.commit()
    checkpoint(store.conn)  # what it wrote is in mind.db itself, not only in its WAL
    return live, store


def digest(root: Path) -> dict:
    """Every file under root but SQLite's -shm (a reader's marks live there) and -wal."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and not p.name.endswith(("-shm", "-wal"))}


def prepared(tmp_path, live) -> prepare.Layout:
    layout = prepare.check_out(tmp_path / "box", live)
    prepare.make_root(layout)
    prepare.take_snapshot(live, layout)
    prepare.copy_library(live, layout)
    return layout


async def fast(clock):
    while True:
        await clock.advance(1, 0.5)


def run(tmp_path, layout, body, *, online=False, llm=None, real_clock=False, quick=False):
    """Start a sandbox and its API on the fast clock (the system's with real_clock), run
    `body(sandbox, api, client)`, stop. `quick`: the sandbox's --fast."""
    async def go():
        clock = None if real_clock else FakeClock(
            datetime(2026, 11, 5, 20, tzinfo=ZoneInfo("America/New_York")))
        ticker = asyncio.create_task(fast(clock)) if clock is not None else None
        cfg = load_config(tmp_path / "defaults.toml")  # absent: the default models
        model = llm or FakeLLM(script, models={cfg.models.live, cfg.models.embedding})
        sandbox = Sandbox(layout, make_llm=lambda cfg: model, clock=clock, online=online, seed=1,
                          ollama_conf=tmp_path / "no-ollama-chat.conf", fast=quick)
        api = Api(sandbox)
        teardown = _logging(layout.data / "logs", "INFO")
        try:
            await sandbox.start()
            await api.start()
            client = Client(api.url)

            async def call(method, path, payload=None):
                return await asyncio.to_thread(client.call, method, path, payload, timeout=60)
            return await body(sandbox, api, call)
        finally:
            await api.close()
            await sandbox.close()
            if ticker is not None:
                ticker.cancel()
            teardown()
    return asyncio.run(go())


def test_a_dm_is_answered_with_its_prompt_and_nothing_leaves_the_sandbox(tmp_path):
    live, store = make_live(tmp_path)
    try:
        before = digest(live)
        layout = prepared(tmp_path, live)

        async def body(sandbox, api, call):
            assert sandbox.app.data_dir == layout.data
            assert sandbox.bridge.port not in (7878,) and api.port != 7881
            res = await call("POST", "/say", {"channel": "dm", "to": 102,
                                              "text": "hey, how was your day?", "wait": 120})
            assert res["ok"] and res["settled"]
            assert res["his"]["text"] == "hey, how was your day?" and res["his"]["message"]
            [answer] = res["answers"]
            assert answer["guid"] == 102 and answer["name"] == "Belra"
            assert answer["text"] and answer["latency_s"] >= 0
            assert answer["discord_message"] and answer["channel"] == "dm:102"
            assert answer["attempts"] >= 1 and answer["sent_attempt"] >= 0
            prompt = answer["prompt"]
            assert prompt["tokens"] > 0 and prompt["reply"]
            assert any("hey, how was your day?" in m["content"] for m in prompt["messages"])
            assert answer["beat"]["asked"] and answer["beat"]["want"] == "share"
            assert answer["guard"]["verdict"]
            status = await call("GET", "/status")
            assert status["model"]["ready"] and not status["human"]["online"]
            assert "NightScheduler.run_forever" in status["quiet"]
            members = (await call("GET", "/members?online=1"))["members"]
            assert {m["guid"] for m in members} == set(TEST_CAST)
            assert all(m["summary"] and m["persona"] for m in members)
            logged = await call("GET", "/log?limit=5000")
            assert any(e.get("event") == "sandbox_say" for e in logged["lines"])
            more = await call("GET", f"/log?since={logged['next']}")
            assert more["next"] >= logged["next"]
            return sandbox.bridge.commands
        commands = run(tmp_path, layout, body)
        assert not [c for c in commands if c["type"] == "say"]  # a DM never reaches the game
        assert digest(live) == before
    finally:
        store.close()
    assert {p.name for p in tmp_path.iterdir()} <= {"live", "box"}
    assert not (layout.info).exists() and not (layout.data / "control.json").exists()


def test_login_whisper_logout(tmp_path):
    live, store = make_live(tmp_path)
    store.close()
    layout = prepared(tmp_path, live)

    async def body(sandbox, api, call):
        refused = None
        try:
            await call("POST", "/say", {"channel": "whisper", "to": 101, "text": "you there?"})
        except RuntimeError as e:
            refused = str(e)
        assert refused and "/login" in refused
        assert (await call("POST", "/login", {}))["online"]
        assert sandbox.app.world.human_online
        res = await call("POST", "/say", {"channel": "whisper", "to": 101, "text": "you there?"})
        [answer] = res["answers"]
        assert answer["guid"] == 101 and answer["channel"] == "whisper"
        said = [c for c in sandbox.bridge.commands if c["type"] == "say"]
        assert said and said[-1]["to_guid"] == HUMAN
        assert (await call("POST", "/group", {"members": [101, 103]}))["group"] == [HUMAN, 101, 103]
        party = await call("POST", "/say", {"channel": "party", "text": "ready?"})
        assert party["answers"] and {a["guid"] for a in party["answers"]} <= {101, 103}
        assert not (await call("POST", "/logout", {}))["online"]
        assert not sandbox.app.world.human_online
    run(tmp_path, layout, body)


def test_reset_restores_the_copy(tmp_path):
    live, store = make_live(tmp_path)
    store.close()
    layout = prepared(tmp_path, live)

    def messages():
        import sqlite3
        from contextlib import closing
        uri = f"{(layout.data / 'mind.db').resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            return conn.execute("SELECT count(*) FROM discord_message").fetchone()[0]

    async def body(sandbox, api, call):
        assert messages() == 0
        await call("POST", "/say", {"channel": "dm", "to": 103, "text": "morning"})
        assert messages() >= 2
        first = sandbox.app
        status = await call("POST", "/reset", {})
        assert status["resets"] == 1 and sandbox.app is not first and messages() == 0
        again = await call("POST", "/say", {"channel": "dm", "to": 103, "text": "morning"})
        assert again["answers"] and messages() >= 2
    run(tmp_path, layout, body)


def test_quiet_mind(tmp_path):
    live, store = make_live(tmp_path)
    store.close()
    layout = prepared(tmp_path, live)

    async def body(sandbox, api, call):
        app = sandbox.app
        assert app.night_scheduler.due() is None
        assert app.night_scheduler.due(gate=False) is None
        assert not app.selfies.cfg.enabled and not app.voice.cfg.enabled
        assert app.arbiter.admin.__class__.__name__ == "NullAdmin"
        assert not app.guild.armory.available
        names = {getattr(f, "__qualname__", "") for f in app._background}
        assert "NightScheduler.run_forever" not in names
        assert names <= {"ControlEndpoint.serve", "Capture.run", "ServerLease.run",
                         "attach_server.<locals>.play_tick"}
        assert isinstance(app.llm, SandboxLLM)
        with pytest.raises(Exception):
            await app.llm.chat(app.cfg.models.reflection, [], options={})
        assert app.llm.refused["chat"] == 1
        assert app.engine.bakeoff_capture
        assert app.engine.wait_scale == 1.0  # her delay and typing as in the live mind
    run(tmp_path, layout, body)


def test_the_sandbox_toml_turns_off_what_acts_on_its_own(tmp_path):
    live_like = """\
[talk]
chattiness = 4
whispers_per_hour = 4
[human]
nickname = "Zan"
real_name = "Quillon"
pronouns = "he"
[server]
enabled = true
[culture]
enabled = true
calendar = true
loot = true
bots = true
emoji = true
memes = true
[guards]
enabled = true
[photos]
enabled = true
send = true
requests = true
[selfies]
enabled = true
booth = true
snaps = true
[voice]
enabled = true
autostart = true
[voice_notes]
enabled = true
[training]
enabled = true
lab = true
[realm]
enabled = true
[world]
enabled = true
[banter]
enabled = true
[outreach]
enabled = true
[playstyle]
enabled = true
[raidplay]
respond = true
[personas]
auto_newcomers = true
[guild]
regear_to = 245
[bakeoff]
capture = false
[control]
ops_actions = true
"""
    for text in (live_like, "[human]\nnickname = \"Zan\"\n"):
        path = tmp_path / "mind.toml"
        prepare.write_toml(path, prepare.with_bridge(prepare.quiet_toml(text), 40404))
        assert tomllib.loads(path.read_text("utf-8")) == prepare.with_bridge(
            prepare.quiet_toml(text), 40404)
        cfg = load_config(path)
        assert cfg.talk.whispers_per_hour == 0 and cfg.talk.openers_per_hour == 0
        assert cfg.bridge.port == 40404 and cfg.bridge.host == "127.0.0.1"
        assert cfg.ollama.max_inflight == 2
        from leeroy_mind.bakeoff.config import load as bakeoff
        from leeroy_mind.culture.config import load_culture_config
        from leeroy_mind.endpoint import load_control_config
        from leeroy_mind.guild.config import load_guild_config
        from leeroy_mind.photos.config import load_photos_config
        from leeroy_mind.selfies.config import load_selfie_config
        from leeroy_mind.server.config import load_server_config
        from leeroy_mind.voice.config import load_voice_config
        assert bakeoff(path).capture
        assert not load_selfie_config(path).enabled
        assert not load_voice_config(path).enabled
        photos = load_photos_config(path)
        assert not photos.send and not photos.requests
        server = load_server_config(path)
        assert server.conversations_per_hour == 0 and server.dms_per_day == 0
        assert server.enabled == ("[server]\nenabled = true" in text)
        culture = load_culture_config(path)
        assert not (culture.calendar or culture.loot or culture.bots or culture.memes)
        guild = load_guild_config(path)
        assert guild.offline_raids_per_day == 0 and guild.regear_to == 0
        control = load_control_config(path)
        assert control.enabled and not control.ops_actions and control.port == 0


@pytest.mark.parametrize("where", ["inside", "same", "around", "git", "file", "busy"])
def test_live_looking_places_are_refused(tmp_path, where):
    live, store = make_live(tmp_path)
    store.close()
    out = {"inside": live / "scratch" / "box", "same": live, "around": tmp_path,
           "git": tmp_path / "repo" / "box", "file": tmp_path / "a-file",
           "busy": tmp_path / "busy"}[where]
    if where == "git":
        (tmp_path / "repo" / ".git").mkdir(parents=True)
    if where == "file":
        out.write_bytes(b"x")
    if where == "busy":
        out.mkdir()
        (out / "notes.txt").write_bytes(b"mine")
    before = digest(tmp_path)
    with pytest.raises(prepare.Refused):
        prepare.check_out(out, live)
    assert sandbox_main(["--data-dir", str(live), "--out", str(out)]) == 1
    assert digest(tmp_path) == before


def test_the_repository_and_links_are_refused(tmp_path):
    live, store = make_live(tmp_path)
    store.close()
    with pytest.raises(prepare.Refused):
        prepare.check_out(Path(__file__).parent / "box", live)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {type(exc).__name__}")
    with pytest.raises(prepare.Refused):
        prepare.check_out(link / "box", live)


def test_api_refuses_browsers_and_wrong_hosts(tmp_path):
    live, store = make_live(tmp_path)
    store.close()
    layout = prepared(tmp_path, live)

    async def raw(port, text):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(text.encode())
        await writer.drain()
        data = await reader.read()
        writer.close()
        return int(data.split(b" ", 2)[1])

    async def body(sandbox, api, call):
        p = api.port
        assert await raw(p, f"GET /status HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n\r\n") == 200
        assert await raw(p, "GET /status HTTP/1.1\r\nHost: evil.example\r\n\r\n") == 403
        assert await raw(p, f"GET /status HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n"
                            "Origin: http://evil.example\r\n\r\n") == 403
        assert await raw(p, f"POST /logout HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n"
                            "Content-Type: text/plain\r\nContent-Length: 2\r\n\r\n{}") == 415
        assert await raw(p, f"GET /nothing HTTP/1.1\r\nHost: localhost:{p}\r\n\r\n") == 404
        assert await raw(p, f"POST /say HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n"
                            "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}") == 400
    run(tmp_path, layout, body)


def test_sandbox_talk_runs_a_script(tmp_path):
    live, store = make_live(tmp_path)
    store.close()
    layout = prepared(tmp_path, live)
    talk = tmp_path / "talk.txt"
    talk.write_bytes("# a comment\nhey you\n\nwhat are you up to tonight?\n".encode())
    saved = tmp_path / "out.json"
    printed = []

    async def body(sandbox, api, call):
        code = await asyncio.to_thread(sandbox_talk_main, [
            "--url", api.url, "--guid", "104", "--channel", "dm", "--script", str(talk),
            "--json", str(saved), "--prompts"], out=printed.append)
        assert code == 0
    run(tmp_path, layout, body)
    turns = json.loads(saved.read_bytes())["turns"]
    assert [t["text"] for t in turns] == ["hey you", "what are you up to tonight?"]
    assert all(t["result"]["answers"] for t in turns)
    assert printed[0].startswith("[1] you -> Dunna (dm): hey you")
    assert any("prompt" in line and "tok" in line for line in printed)
    said = turns[0]["result"]["answers"][0]["text"]  # her typing on the model's words
    assert any(line.strip() == said for line in printed)
    assert any(line.strip().startswith("|   ") for line in printed)  # --prompts
    saved.unlink()
    talk.unlink()


def test_fast_answers_in_model_time(tmp_path):
    """--fast: on the system's clock, an answer comes as soon as the (fake, instant) model has
    made it: no reading delay and no typing time, and a reset keeps it."""
    live, store = make_live(tmp_path)
    store.close()
    layout = prepared(tmp_path, live)

    async def body(sandbox, api, call):
        assert sandbox.app.engine.wait_scale == 0.0
        assert (await call("GET", "/status"))["fast"]
        res = await call("POST", "/say", {"channel": "dm", "to": 105, "text": "evening"})
        [answer] = res["answers"]
        assert answer["latency_s"] < 3 and res["settled"]
        await call("POST", "/login", {})
        whisper = await call("POST", "/say", {"channel": "whisper", "to": 101, "text": "hi"})
        assert whisper["answers"][0]["latency_s"] < 3
        await call("POST", "/reset", {})
        assert sandbox.app.engine.wait_scale == 0.0
    run(tmp_path, layout, body, real_clock=True, quick=True)


def test_wait_scale_skips_the_delay():
    """Pipeline._delay waits turn.delay times wait_scale on the clock: 0 returns at once."""
    from types import SimpleNamespace
    from leeroy_mind.talk.pipeline import Pipeline

    async def go():
        clock = FakeClock(datetime(2026, 11, 5, 20, tzinfo=ZoneInfo("America/New_York")))
        turn, meta = SimpleNamespace(delay=30.0), SimpleNamespace(wake=None)
        await asyncio.wait_for(Pipeline._delay(SimpleNamespace(clock=clock, wait_scale=0.0),
                                               turn, meta), 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(Pipeline._delay(SimpleNamespace(clock=clock, wait_scale=1.0),
                                                   turn, meta), 0.2)
    asyncio.run(go())
