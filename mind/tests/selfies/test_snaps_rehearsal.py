"""P4 Task 13: the headless rehearsal of the night side of situational photos. A real MindApp on a
scratch data dir with its real control endpoint, a FakeBridge, a FakeLLM and the fake worker in
identity mode (no torch, no model, no GPU), each port free and loopback. It touches no live data,
bridge, Ollama, voice service or console. Photos are made and stocked here; sending them belongs
to the guild Discord's photo phase. Every person here is invented."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from conftest import HUMAN, run, seed_test_cast
from leeroy_mind import endpoint
from leeroy_mind.app import GUILD_ID, MindApp
from leeroy_mind.clock import night_of
from leeroy_mind.config import load_config
from leeroy_mind.nightly.store import run_get
from leeroy_mind.protocol import Member
from leeroy_mind.selfies import cli, files, snapstage, stage
from leeroy_mind.selfies.job import pid_alive
from leeroy_mind.selfies.scene import scene_from, scene_json
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.snaps import looks_hash
from leeroy_mind.selfies.store import current
from leeroy_mind.selfies.store import pick as pick_photo
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_bridge import FakeBridge, load_fixture
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from nightly.conftest import FakeAdmin, generic
from nightly.test_night_enrich import APPEARANCE
from nightly.test_night_scenes import SCENE_REPLY, raid

from .fakes import CLOCK, fake_command, planned_snap, ready, renderable_cast, tagged_jpeg, until
from .test_selfies_rehearsal import closed

MEMBERS = (101, 102, 103)
SCHEMA = "matching this schema:"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "session_basic.jsonl"


def answer(model: str, messages: list[dict]) -> str:
    """A scene for the scene pass, a quiet generic reply for any other schema."""
    text = messages[-1]["content"]
    if SCHEMA not in text:
        return "evening"
    shape, _ = json.JSONDecoder().raw_decode(text.rsplit(SCHEMA, 1)[-1].lstrip())
    return json.dumps(SCENE_REPLY if shape["title"] == "scene" else generic(text))


def snap_rows(data: Path) -> list[tuple]:
    s = Store(open_db(data / "mind.db"), clock=FakeClock(CLOCK))
    try:
        return [tuple(r) for r in s.conn.execute(
            "SELECT id, kind, guid, status, ended FROM selfie_snap ORDER BY id")]
    finally:
        s.close()


def test_situational_photos_are_made_at_night_and_a_login_ends_the_worker(
        tmp_path, monkeypatch, capsys):
    data = tmp_path / "mind-data"
    data.mkdir()
    env = {"FAKE_SELFIE_IDENTITY": "1"}  # the fake worker's behaviour for the night under way
    for mod in (stage, snapstage):
        monkeypatch.setattr(mod, "worker_command",
                            lambda cfg, identity=False: fake_command(tmp_path)[0])
        monkeypatch.setattr(mod, "worker_env", lambda cfg: {**fake_command(tmp_path)[1], **env})
    pids, log = tmp_path / "pids.json", tmp_path / "requests.log"
    hello = {"type": "hello", "server": "mod-leeroy", "version": "0.1.0", "guild_id": GUILD_ID,
             "human_guid": HUMAN}
    out = {}

    async def go():
        clock = FakeClock(CLOCK, yields=64)
        bridge = FakeBridge([hello, load_fixture(FIXTURE)[1]])
        bridge_port = await bridge.start()
        out["ports"] = [bridge_port]
        (data / "mind.toml").write_text(
            f"[bridge]\nport = {bridge_port}\n[talk]\nchattiness = 0\n"
            "[selfies]\nenabled = true\nsnaps = true\n", encoding="utf-8")
        store = Store(open_db(data / "mind.db"), clock=clock)
        try:
            seed_test_cast(store)
            renderable_cast(store, *MEMBERS)
            store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0,
                                      level=80, rank=0, online=False, map=0, zone=0,
                                      spec_tab=-1)], 1000.0)
            for guid in MEMBERS:  # a chosen profile photo, with the history file the pick keeps
                ready(store, data, guid, 1, now=1.0)
                pick = pick_photo(store, guid, 1, 0, source="console", now=2.0)
                hist = files.history(data, guid, pick)
                hist.parent.mkdir(parents=True, exist_ok=True)
                hist.write_bytes(tagged_jpeg(f"history/{guid}"))
            # a raid he led an hour before the night, with one kill
            raid(SimpleNamespace(store=store), ended=CLOCK.timestamp() - 3600, members=MEMBERS)
        finally:
            store.close()
        cfg = load_config(data / "mind.toml")
        llm = FakeLLM(answer, models={cfg.models.live, cfg.models.reflection,
                                      cfg.models.embedding})
        app = MindApp(cfg, data, llm=llm, clock=clock)
        admin = FakeAdmin([cfg.models.embedding])
        app.arbiter.admin = admin
        seen = {}
        preload = admin.preload

        async def checked_preload(model, keep_alive, *, options=None):
            if model == cfg.models.live and pids.exists():
                got = json.loads(pids.read_text())
                seen["alive"] = (pid_alive(got["pid"]), pid_alive(got["child"]))
            await preload(model, keep_alive, options=options)

        admin.preload = checked_preload
        task = asyncio.create_task(app.run())
        try:
            await until(lambda: app.world.human_online, 10)
            await bridge.push({"type": "logout", "guid": HUMAN, "human": True})
            await until(lambda: app.nightly.presence.online is False, 10)

            async def advance_until(done, step=30.0):
                while not done():
                    await clock.advance(step, step)
                    await asyncio.sleep(0.02)

            def night_done():
                run_ = run_get(app.store, night_of(clock.local()))
                return run_ is not None and run_.status == "done"

            # 1: the first night plans and renders three situations and one guild photo
            await asyncio.wait_for(advance_until(night_done), 180)
            rows = snap_rows(data)
            assert [(r[1], r[3]) for r in rows if r[1] == "situation"] == [("situation", "ready")] * 3
            assert sorted(r[2] for r in rows if r[1] == "situation") == list(MEMBERS)
            assert [r[3] for r in rows if r[1] == "guild"] == ["ready"]
            for sid, kind, guid, *_ in rows:
                if kind == "guild":
                    assert files.guild_photo(data, sid).is_file()
                    assert files.guild_photo(data, sid, thumb=True).is_file()
                    assert not files.guild_tiles(data, sid).exists()
                else:
                    assert files.snap(data, guid, sid).is_file()
                    assert files.snap(data, guid, sid, thumb=True).is_file()
            out["ready"] = [r[0] for r in rows]

            # 2: selfies status shows the snap counts
            assert cli.selfies_main(["--data-dir", str(data), "status"]) == 0
            shown = capsys.readouterr().out
            status = json.loads(shown[shown.index("\n{") + 1:])
            assert status["snaps"]["ready"] == 4 and status["snaps"]["planned"] == 0
            assert {"scenes", "snaps"} <= set(status["stages"])

            # 3 and 4: the second night. 102 has a planned photo whose render will hang; 103 has
            # one planned for a profile photo he then replaces, which must expire unrendered
            live = Store(open_db(data / "mind.db"), clock=clock)
            try:
                stored_scene = scene_json(scene_from(compose_choices(SCENE_REPLY)))
                good = planned_snap(live, 102, ref="rehearsal:good", scene=stored_scene,
                                    pick=current(live, 102).pick, looks=looks_hash(APPEARANCE))
                stale = planned_snap(live, 103, ref="rehearsal:stale", scene=stored_scene,
                                     pick=current(live, 103).pick, looks=looks_hash(APPEARANCE))
                ready(live, data, 103, 2, now=3.0)
                pick_photo(live, 103, 2, 0, source="console", now=4.0)
            finally:
                live.close()
            env.update(FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
                       FAKE_SELFIE_PIDS=str(pids), FAKE_SELFIE_LOG=str(log))
            first = night_of(clock.local())  # the next night is a new label: play through the day
            await bridge.push({"type": "login", "guid": HUMAN, "human": True})
            await until(lambda: app.world.human_online, 10)
            await asyncio.wait_for(advance_until(
                lambda: night_of(clock.local()) != first, step=600.0), 20)
            await bridge.push({"type": "logout", "guid": HUMAN, "human": True})
            await until(lambda: app.nightly.presence.online is False, 10)
            await asyncio.wait_for(advance_until(
                lambda: log.exists() and f"s{good}:0 " in log.read_text()), 120)
            assert app.nightly.running is not None
            assert all(pid_alive(p) for p in json.loads(pids.read_text()).values())
            await bridge.push({"type": "login", "guid": HUMAN, "human": True})
            await until(lambda: "alive" in seen, 20)
            assert seen["alive"] == (False, False)  # dead before the live model's preload
            await app.nightly.handback()
            got = {r[0]: r for r in snap_rows(data)}
            assert got[good][3] == "planned"  # waits for a later night
            assert not files.snap(data, 102, good).exists()
            assert not files.snap(data, 102, good, thumb=True).exists()
            assert got[stale][3] == "expired" and got[stale][4] == "photo_changed"
            assert not files.snap(data, 103, stale).exists()
            assert f"s{stale}:" not in log.read_text()
            for sid in out["ready"]:  # R9: nothing the first night made is deleted
                assert got[sid][3] in ("ready", "expired")
                if got[sid][1] == "situation":
                    assert files.snap(data, got[sid][2], sid).is_file()
            out["ports"] += [json.loads((data / endpoint.KEY_FILE).read_text())["port"]]
        finally:
            await app.stop()
            await asyncio.wait({task}, timeout=10)
            await bridge.stop()

    run(go())
    assert not (data / endpoint.KEY_FILE).exists()
    assert len(out["ports"]) == 2 and len(set(out["ports"])) == 2
    assert all(closed(p) for p in out["ports"])
