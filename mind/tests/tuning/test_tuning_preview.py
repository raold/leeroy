"""T7: the preview. Numbers for the dials that change how often; a line before and after, with
one seed, for the wording dials, only while the live model is already resident; nothing is
stored, logged or spent, and no model is loaded, unloaded or swapped. FakeLLM only."""
import logging
from datetime import datetime, timezone

import pytest

from conftest import HUMAN, make_persona, run, seed_test_cast
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member
from dbtemplate import open_migrated
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.tuning.lines import BUILTIN_LINES
from leeroy_mind.tuning.preview import LEASE, REFUSE

CLOCK = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
REAL = "Quillon"  # invented, as in the engine tests
TOML = f'[human]\nnickname = "Zan"\nreal_name = "{REAL}"\npronouns = "he"\n'


class Admin:
    def __init__(self, loaded):
        self.loaded = list(loaded)
        self.calls = []

    async def loaded_models(self):
        return list(self.loaded)

    async def unload(self, model):
        self.calls.append(("unload", model))

    async def preload(self, model, keep_alive, *, options=None):
        self.calls.append(("preload", model, keep_alive))


def _app(root, *, loaded=True, lines=None):
    (root / "mind.toml").write_text(TOML, encoding="utf-8")
    s = Store(open_migrated(root / "mind.db"), clock=FakeClock(CLOCK))
    try:
        seed_test_cast(s)
        s.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=80,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
        s.set_meta("human_guid", str(HUMAN))
        woman = make_persona(102, "Belra", player_gender="female", stance="lust",
                             habits=[{"substance": "alcohol", "when": "daily", "odds": 0.4,
                                      "max_state": "drunk"}])
        save_persona(s, woman, source="seed", ts=1.0, race="Night Elf", class_name="Druid")
        # his inner circle: her prompt could offer his real name, a preview's never does
        s.put_relationship(Relationship(a=102, b=HUMAN, affinity=80, familiarity=95,
                                        interactions=40))
    finally:
        s.close()
    count = [0]

    def script(model, messages):
        count[0] += 1
        return (lines or "ok so line {n} then").format(n=count[0])
    cfg = load_config(root / "mind.toml")
    app = MindApp(cfg, root, llm=FakeLLM(script, models={cfg.models.live}), clock=FakeClock(CLOCK))
    app.arbiter.admin = Admin([cfg.models.live, cfg.models.embedding] if loaded
                              else [cfg.models.embedding])
    return app


def _args(scope, target, dial, value):
    return {"action": "preview", "scope": scope, "target": target, "dial": dial, "value": value}


def _written(app):
    return {t: app.store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("tuning_change", "tuning_value", "utterance", "chat_line", "memory")}


def test_frequency_dials_show_numbers_and_ask_no_model(tmp_path):
    async def go():
        app = _app(tmp_path)
        try:
            before = _written(app)
            r = await app.tuning.handle(_args("member", "102", "forward", 10))
            assert r["ok"] and r["kind"] == "numbers" and r["reach"] == 1
            rows = {x["what"]: x for x in r["subjects"][0]["numbers"]}
            flirt = rows["chance a social turn becomes a flirt"]
            assert flirt["after"] == pytest.approx(min(90.0, 2 * flirt["before"]))
            r = await app.tuning.handle(_args("member", "102", "party", 10))
            assert r["subjects"][0]["numbers"][0]["before"] == 40
            assert r["subjects"][0]["numbers"][0]["after"] == 80
            r = await app.tuning.handle(_args("member", "101", "reliability", 2))
            late = {x["what"]: x for x in r["subjects"][0]["numbers"]}["late for a summon"]
            assert (late["before"], late["after"]) == (False, True)
            r = await app.tuning.handle(_args("group", "everyone", "talkative", 5))
            assert r["reach"] == 5 and len(r["subjects"]) == 3
            r = await app.tuning.handle(_args("world", "", "chattiness", 8))
            gap = r["world"][0]
            assert gap["before"] == pytest.approx(2 * gap["after"])
            assert app.llm.calls == [] and _written(app) == before
        finally:
            app.store.close()
    run(go())


def test_the_preview_counts_photos(tmp_path):
    async def go():
        app = _app(tmp_path)
        try:
            r = await app.tuning.handle(_args("world", "", "selfies", 10))
            week, day = r["world"]
            assert (week["before"], week["after"]) == (3, 40)
            assert (day["before"], day["after"]) == (1, 14)
            assert app.llm.calls == []
        finally:
            app.store.close()
    run(go())


def test_a_wording_dial_shows_a_line_before_and_after_with_one_seed(tmp_path, caplog):
    async def go():
        app = _app(tmp_path)
        try:
            state, before = app.engine.rng.getstate(), _written(app)
            r = await app.tuning.handle(_args("member", "102", "humour", 9))
            assert r["ok"] and r["kind"] == "lines", r
            (one,) = r["subjects"]
            assert one["guid"] == 102 and one["before"]["text"] and one["after"]["text"]
            chats = [c for c in app.llm.calls if "messages" in c]
            assert len(chats) == 2
            seeds = {c["options"]["seed"] for c in chats}
            assert len(seeds) == 1
            assert chats[0]["options"]["num_ctx"] == chats[1]["options"]["num_ctx"] == 4096
            systems = [c["messages"][0]["content"] for c in chats]
            assert BUILTIN_LINES[("humour", "high")] not in systems[0]
            assert BUILTIN_LINES[("humour", "high")] in systems[1]
            assert all(REAL not in m["content"] for c in chats for m in c["messages"])
            assert app.engine.rng.getstate() == state and _written(app) == before
            assert app.arbiter.holders == frozenset() and app.arbiter.admin.calls == []
        finally:
            app.store.close()
    with caplog.at_level(logging.DEBUG):
        run(go())
    assert "line 1 then" not in caplog.text and "line 2 then" not in caplog.text


def test_a_line_preview_holds_a_lease_while_it_runs_so_no_night_starts(tmp_path):
    seen = []

    async def go():
        app = _app(tmp_path)
        try:
            real = app.engine.preview_line

            async def watch(*a, **kw):
                seen.append(app.arbiter.holders)
                return await real(*a, **kw)
            app.engine.preview_line = watch
            assert (await app.tuning.handle(_args("member", "101", "lore", 1)))["ok"]
            assert app.arbiter.holders == frozenset()
        finally:
            app.store.close()
    run(go())
    assert seen and all(h == {LEASE} for h in seen)


def test_without_the_live_model_resident_a_line_preview_is_refused_in_plain_words(tmp_path):
    async def go():
        app = _app(tmp_path, loaded=False)
        try:
            r = await app.tuning.handle(_args("member", "101", "argumentative", 9))
            assert r == {"ok": False, "error": REFUSE["unloaded"]}
            app.arbiter.admin.loaded = [app.cfg.models.live, app.cfg.models.reflection]
            r = await app.tuning.handle(_args("member", "101", "argumentative", 9))
            assert r == {"ok": False, "error": REFUSE["night"]}
            assert app.llm.calls == [] and app.arbiter.admin.calls == []
        finally:
            app.store.close()
    run(go())


def test_a_night_run_under_way_refuses_a_line_preview(tmp_path):
    async def go():
        app = _app(tmp_path)
        try:
            app.night_scheduler = type("S", (), {"running": "2026-10-29", "due": lambda s: None})()
            r = await app.tuning.handle(_args("member", "101", "theorycraft", 9))
            assert r == {"ok": False, "error": REFUSE["night"]}
        finally:
            app.store.close()
    run(go())


def test_a_preview_refuses_what_a_change_would(tmp_path):
    async def go():
        app = _app(tmp_path)
        try:
            r = await app.tuning.handle(_args("member", "101", "forward", 9))
            assert not r["ok"] and "Men stay friends" in r["error"]
            r = await app.tuning.handle(_args("member", str(HUMAN), "humour", 9))
            assert r == {"ok": False, "error": "your own character is never tuned"}
        finally:
            app.store.close()
    run(go())


def test_the_world_romance_dial_previews_her_whisper_and_its_band(tmp_path):
    async def go():
        app = _app(tmp_path)
        try:
            r = await app.tuning.handle(_args("world", "", "romance", 9))
            assert r["ok"] and r["kind"] == "lines"
            assert r["world"][0]["before"] == "warm" and r["world"][0]["after"] == "explicit"
            assert [s["guid"] for s in r["subjects"]] == [102]
            chats = [c for c in app.llm.calls if "messages" in c]
            assert len(chats) == 2 and chats[0]["messages"] != chats[1]["messages"]
            assert app.cfg.romance.dial == 3  # nothing was set
        finally:
            app.store.close()
    run(go())
