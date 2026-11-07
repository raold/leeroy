"""The light minds' speech: rival officers' rationed, screened lines from the capitals, the
gankers' emotes and the officers' places, over the real Realm service, Store, DirectLedger,
queue and body ledger with the small RealmApp surface. The model is a FakeLLM behind the real
LLMScheduler; the armory is a stub, so nothing reads worldserver.conf or MySQL."""
import asyncio
import dataclasses
import json
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import uuid

import pytest

from conftest import run, seed_test_cast
from leeroy_mind.config import HumanCfg
from leeroy_mind.llm.client import ChatResult
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.llm.scheduler import LLMScheduler, Priority
from leeroy_mind.protocol import Ack, Hello, Logout
from leeroy_mind.realm import protocol, speech as speech_module, store as realm_store
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.config import LIMITS
from leeroy_mind.realm.direct import DirectLedger
from leeroy_mind.realm.places import DIST, Place, Places
from leeroy_mind.realm.ports import LivePorts
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.realm.speech import Speech
from realm.fakes import RealmApp, enabled

GUILD_NAME = "Testcast"  # His guild's own name (guild 21): never said by a rival.
REAL_NAME = "Quillon Testname"  # His (synthetic) real name: never in a rival's prompt.
TESTGUILD, OTHERGUILD, GANKERS = 50, 60, 70
VEX, ROOK, MARA, GRIM = 201, 202, 211, 301
ORGRIMMAR, UNDERCITY, DURATOR = 1637, 1497, 14


class FakeLLM:
    """Answers in turn; the last answer repeats."""

    def __init__(self, *texts):
        self.texts = list(texts) or ["lf1m heroic voa, pst"]
        self.calls = []

    async def chat(self, model, messages, *, options, think=None, format=None, keep_alive=None,
                   timeout=None):
        self.calls.append(dict(model=model, messages=messages, options=options, think=think,
                               format=format, timeout=timeout))
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return ChatResult(text, "", 4, 0.1)


class RecordingScheduler(LLMScheduler):
    def __init__(self, llm, clock):
        super().__init__(llm, clock=clock)
        self.submitted = []

    async def submit(self, priority, job, *, deadline=None):
        self.submitted.append((priority, deadline))
        return await super().submit(priority, job, deadline=deadline)


class FakeArmory:
    def __init__(self):
        self.reads = []

    def guild_name(self, guild_id):
        self.reads.append(guild_id)
        return GUILD_NAME


def seed_rivals(store):
    with store.transaction():
        for guild, role, faction, name in ((TESTGUILD, "progression", "horde", "Testguild"),
                                           (OTHERGUILD, "drama", "horde", "Otherguild"),
                                           (GANKERS, "gankers", "alliance", "Gankguild")):
            store.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (?,?,?,?,1.0)",
                               (guild, role, faction, name))
        for guid, guild, role, name in ((VEX, TESTGUILD, "officer", "Vex"), (ROOK, TESTGUILD, "officer", "Rook"),
                                        (MARA, OTHERGUILD, "officer", "Mara"), (GRIM, GANKERS, "ganker", "Grim")):
            store.conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added) VALUES (?,?,?,?,1.0)",
                               (guid, guild, name, role))


def make_app(tmp_path, store, clock, monkeypatch, *, llm=None, blocklist=True, name=GUILD_NAME):
    app = RealmApp(tmp_path, store, clock)
    app.personas = seed_test_cast(store)
    seed_rivals(store)
    llm = llm or FakeLLM()
    app.scheduler = RecordingScheduler(llm, clock)
    app.cfg = dataclasses.replace(app.cfg, models=dataclasses.replace(app.cfg.models, live="synthetic-live-model"),
                                  human=HumanCfg("Boss", REAL_NAME, ""))
    app.model_ready = True
    app.engine = NS(speak=AsyncMock(return_value=[]), say_as=AsyncMock(return_value=True),
                    live_admission=AsyncMock())
    phase = {"now": "none"}
    app.raid = NS(phase=lambda now: phase["now"])
    app.world.human_zone = ORGRIMMAR
    if blocklist:
        (tmp_path / "realm").mkdir(exist_ok=True)
        (tmp_path / "realm" / "blocklist.txt").write_text("blockedword\n", encoding="utf-8")
    if name:
        store.set_meta("realm:guild_name", json.dumps({"guild": 21, "name": name, "at": 1.0}))
    enabled(tmp_path, True)
    realm = attach_realm(app)
    armory = FakeArmory()
    monkeypatch.setattr(realm.ports, "armory", lambda: armory)
    speech = realm.part("speech")
    speech._places = Places.load(DIST)
    speech.moment_chance = 0.0  # Moments come from the tests.
    return NS(app=app, realm=realm, speech=speech, llm=llm, phase=phase, armory=armory, store=store,
              clock=clock)


@pytest.fixture
def sapp(tmp_path, store, clock, monkeypatch):
    return make_app(tmp_path, store, clock, monkeypatch)


async def start(s, *, online=True):
    await s.app.connection(True)
    await s.app.event(Hello(0, "test", "1", 21, 1 if online else 0))


async def settle(realm):
    for _ in range(50):
        pending = [t for t in (*realm._children, *realm.queue._tasks, *realm.ports._tasks,
                               *realm.ports._settlements) if not t.done()]
        if not pending:
            await asyncio.sleep(0)
            if not any(not t.done() for t in (*realm._children, *realm.queue._tasks)):
                return
            continue
        await asyncio.gather(*pending, return_exceptions=True)


async def step(s, n=1):
    for _ in range(n):
        await s.realm.tick()
        await settle(s.realm)


async def speak(s):
    result = s.speech.maybe_speak(s.clock.now())
    await settle(s.realm)
    return result


async def pump(s):
    """Run the action queue once: its paced dispatch, then the sends' settlement."""
    s.realm.queue.tick()
    await settle(s.realm)


async def arrive(s, move):
    await s.app.event(protocol.RealmJob(int(s.clock.now() * 1000), move.id, "realm_move", "done", "",
                                        move.guid, 0))


async def later(s, seconds):
    await s.clock.advance(seconds, step=seconds)


async def his_line(s, text="anyone selling frostweave", channel="trade"):
    await s.app.event(protocol.ChannelChat(int(s.clock.now() * 1000), channel, 1, "Him", ORGRIMMAR,
                                           text, False))


def says(s, channel=None):
    return [c for c in s.app.sent if type(c) is protocol.RealmSay
            and (channel is None or c.channel == channel)]


def moves(s):
    return [c for c in s.app.sent if type(c) is protocol.RealmMove]


def lines(store):
    return [dict(r) for r in store.conn.execute(
        "SELECT id,guid,guild,channel,text,kind,state,reason,command_id FROM realm_line ORDER BY id")]


def place(store, guid, name):
    realm_store.set_actor_place(store.conn, guid, name, 1.0)


def retire(store, guild):
    with store.transaction():
        store.conn.execute("UPDATE realm_guild SET status='retired' WHERE id=?", (guild,))


# -- failing closed ------------------------------------------------------------------------------

def test_attaches_as_a_part_on_its_events(sapp):
    assert type(sapp.speech) is Speech and sapp.realm.part("speech") is sapp.speech
    for name in ("channel_chat", "realm_kill", "pvp_kill", "duel_end", "realm_job", "hello", "logout"):
        assert sapp.realm.on_event in sapp.app.handlers[name]
    run(sapp.realm.close())


@pytest.mark.parametrize("missing", ["blocklist", "guild_name"])
def test_screen_fails_closed_and_speech_sends_nothing(tmp_path, store, clock, monkeypatch, missing):
    s = make_app(tmp_path, store, clock, monkeypatch, blocklist=missing != "blocklist",
                 name="" if missing == "guild_name" else GUILD_NAME)
    s.armory.guild_name = lambda guild_id: None  # The refresh finds nothing either.
    place(store, VEX, "uc_bank")

    async def go():
        await start(s)
        await step(s, 3)
        assert s.speech.maybe_speak(clock.now()) is None
        await s.app.event(protocol.PvpKill(1000, GRIM, 1, 0, 1637))
        await step(s)
        await pump(s)
        sent = [c for c in s.app.sent if type(c) is not protocol.RealmActors]
        assert sent == [] and s.llm.calls == [] and lines(store) == []
        await s.realm.close()
    run(go())


def test_guild_name_refreshed_once_per_hello(tmp_path, store, clock, monkeypatch):
    s = make_app(tmp_path, store, clock, monkeypatch, blocklist=False, name="")

    async def go():
        await start(s)
        await step(s, 2)
        assert s.armory.reads == []  # No blocklist: the screen cannot be ready, nothing is read.
        (tmp_path / "realm").mkdir()
        (tmp_path / "realm" / "blocklist.txt").write_text("blockedword\n", encoding="utf-8")
        await step(s, 3)
        assert s.armory.reads == [21] and s.realm.ports.guild_name() == GUILD_NAME
        await s.app.event(Hello(0, "test", "1", 21, 1))  # The same connection: no second read.
        await step(s, 2)
        assert s.armory.reads == [21]
        await s.app.connection(False)
        await start(s)  # A new connection.
        await step(s, 2)
        assert s.armory.reads == [21, 21]
        await s.realm.close()
    run(go())


# -- the rules for a rival line --------------------------------------------------------------------

@pytest.mark.parametrize("phase", ["combat", "wipe_silence", "wipe_debrief"])
def test_no_rival_line_in_his_raid_combat(sapp, phase):
    place(sapp.store, VEX, "uc_bank")
    sapp.phase["now"] = phase

    async def go():
        await start(sapp)
        assert await speak(sapp) is None
        sapp.speech.answer(protocol.ChannelChat(1000, "trade", 1, "Him", ORGRIMMAR, "Testguild are bad", False))
        await settle(sapp.realm)
        assert sapp.llm.calls == [] and says(sapp) == []
        sapp.phase["now"] = "trash"
        assert await speak(sapp) == "trade"
        assert len(says(sapp, "trade")) == 1
        await sapp.realm.close()
    run(go())


def test_no_model_call_while_he_is_offline(sapp):
    place(sapp.store, VEX, "uc_bank")

    async def go():
        await start(sapp, online=False)
        assert await speak(sapp) is None
        assert sapp.llm.calls == [] and sapp.app.scheduler.submitted == [] and says(sapp) == []
        await sapp.realm.close()
    run(go())


def test_trade_needs_a_capital(sapp):
    place(sapp.store, VEX, "uc_bank")
    retire(sapp.store, OTHERGUILD)
    sapp.app.world.human_zone = DURATOR

    async def go():
        await start(sapp)
        await speak(sapp)
        assert says(sapp, "trade") == []
        await later(sapp, 600)
        sapp.app.world.human_zone = ORGRIMMAR
        assert await speak(sapp) == "trade"
        [line] = says(sapp, "trade")
        assert line.guid == VEX and line.to_guid == 0
        await sapp.realm.close()
    run(go())


def test_officer_speaks_from_another_capital(sapp):
    place(sapp.store, VEX, "uc_bank")  # Undercity: the City channel is one per faction.

    async def go():
        await start(sapp)
        assert await speak(sapp) == "trade"
        await pump(sapp)
        assert [c.guid for c in says(sapp, "trade")] == [VEX] and moves(sapp) == []
        [row] = lines(sapp.store)
        assert (row["guid"], row["guild"], row["channel"], row["kind"], row["state"]) == (
            VEX, TESTGUILD, "trade", "rival", "sent")
        assert row["command_id"] == says(sapp)[0].id
        await sapp.realm.close()
    run(go())


def test_officer_moved_to_a_capital_first(sapp):
    async def go():
        await start(sapp)
        retire(sapp.store, OTHERGUILD)
        assert await speak(sapp) == "moving"
        await pump(sapp)
        [move] = moves(sapp)
        assert (move.guid, move.place) == (VEX, "uc_bank") and says(sapp) == []
        assert sapp.llm.calls == []
        assert await speak(sapp) is None  # The move is still on its way: no second one.
        await pump(sapp)
        assert len(moves(sapp)) == 1
        await arrive(sapp, move)
        assert realm_store.actor_places(sapp.store.conn)[VEX][0] == "uc_bank"
        assert await speak(sapp) == "trade"  # On a later tick.
        assert [c.guid for c in says(sapp, "trade")] == [VEX]
        await sapp.realm.close()
    run(go())


def test_capital_chosen_is_not_his_own(sapp):
    sapp.app.world.human_zone = UNDERCITY
    retire(sapp.store, OTHERGUILD)

    async def go():
        await start(sapp)
        assert await speak(sapp) == "moving"
        await pump(sapp)
        assert [m.place for m in moves(sapp)] == ["tb_bank"]
        await sapp.realm.close()
    run(go())


def test_general_needs_the_officer_in_his_zone(sapp):
    sapp.speech._places = Places({
        **Places.load(DIST),
        "durotar_post": Place("durotar_post", 1, DURATOR, 300.0, -4700.0, 11.0, 0.0, "city")})
    sapp.app.world.human_zone = DURATOR
    retire(sapp.store, OTHERGUILD)
    place(sapp.store, VEX, "uc_bank")  # In a capital, but not in his zone.

    async def go():
        await start(sapp)
        assert await speak(sapp) == "moving"
        await pump(sapp)
        [move] = moves(sapp)
        assert move.place == "durotar_post" and says(sapp) == []
        await arrive(sapp, move)
        assert await speak(sapp) == "general"
        [line] = says(sapp)
        assert (line.channel, line.guid) == ("general", move.guid)
        await sapp.realm.close()
    run(go())


def test_no_say_or_yell_rival_path(sapp):
    place(sapp.store, VEX, "uc_bank")
    place(sapp.store, MARA, "tb_bank")

    async def go():
        await start(sapp)
        for zone in (ORGRIMMAR, DURATOR, 4395, 3711, ORGRIMMAR):
            sapp.app.world.human_zone = zone
            await speak(sapp)
            await his_line(sapp)
            await later(sapp, 200)
        assert says(sapp) and all(c.channel in {"trade", "general", "whisper"} for c in says(sapp))
        assert {r["channel"] for r in lines(sapp.store)} <= {"trade", "general", "whisper"}
        await sapp.realm.close()
    run(go())


def test_whisper_to_him_at_most_once_a_day(sapp):
    sapp.app.world.human_zone = 3711  # Sholazar: no Trade, no General path.

    async def go():
        await start(sapp)
        assert await speak(sapp) == "whisper"
        [line] = says(sapp)
        assert (line.channel, line.to_guid) == ("whisper", 1)
        for _ in range(3):
            await later(sapp, 3600)
            assert await speak(sapp) is None
        await later(sapp, 86400)
        assert await speak(sapp) == "whisper"
        assert len(says(sapp, "whisper")) == 2
        await sapp.realm.close()
    run(go())


# -- rationing -----------------------------------------------------------------------------------

@pytest.mark.parametrize("gaps,expected", [((60, 60), 1), ((200, 200), 2)])
def test_rationing_spacing(sapp, gaps, expected):
    retire(sapp.store, OTHERGUILD)
    place(sapp.store, VEX, "uc_bank")

    async def go():
        await start(sapp)
        await speak(sapp)
        for gap in gaps:
            await his_line(sapp)  # Never the three-in-a-row rule here.
            await later(sapp, gap)
            await speak(sapp)
        assert len(says(sapp, "trade")) == expected  # The per-guild cap stops the third.
        await sapp.realm.close()
    run(go())


def test_rationing_realm_cap(sapp, monkeypatch):
    monkeypatch.setattr(speech_module, "LIMITS", dataclasses.replace(LIMITS, rival_guild_lines_per_hour=10))
    place(sapp.store, VEX, "uc_bank")
    place(sapp.store, MARA, "tb_bank")

    async def go():
        await start(sapp)
        for _ in range(8):
            await speak(sapp)
            await his_line(sapp)
            await later(sapp, 200)
        sent = says(sapp, "trade")
        assert len(sent) == 6 and {c.guid for c in sent} == {VEX, MARA}
        await later(sapp, 3600)
        assert await speak(sapp) == "trade"
        await sapp.realm.close()
    run(go())


def test_never_three_in_a_row(sapp):
    place(sapp.store, VEX, "uc_bank")
    place(sapp.store, MARA, "tb_bank")

    async def go():
        await start(sapp)
        for _ in range(3):
            await speak(sapp)
            await later(sapp, 200)
        assert len(says(sapp, "trade")) == 2  # No member or human line between: no third.
        await sapp.app.event(protocol.ChannelChat(int(sapp.clock.now() * 1000), "trade", 101, "Arkon",
                                                  ORGRIMMAR, "wts mammoth", True))  # A member's line.
        assert await speak(sapp) == "trade"
        assert len(says(sapp, "trade")) == 3
        await sapp.realm.close()
    run(go())


def test_rationing_counts_sent_only(sapp):
    retire(sapp.store, OTHERGUILD)
    place(sapp.store, VEX, "uc_bank")
    now = sapp.clock.now()
    with sapp.store.transaction():
        for state, reason, command in (("refused", "native_refused", uuid.uuid4().hex),
                                       ("unknown", "transport_unknown", uuid.uuid4().hex),
                                       ("dropped", "flirt", None), ("refused", "unsent", uuid.uuid4().hex)):
            sapp.store.conn.execute(
                "INSERT INTO realm_line(ts,guid,guild,channel,text,command_id,kind,state,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (now - 100, VEX, TESTGUILD, "trade", "old line", command,
                                                 "rival", state, reason))

    async def go():
        await start(sapp)
        assert await speak(sapp) == "trade"  # Nothing was sent in the last 180 s.
        await his_line(sapp)
        await later(sapp, 200)
        assert await speak(sapp) == "trade"
        await his_line(sapp)
        await later(sapp, 200)
        assert await speak(sapp) is None  # Two sent: the guild's hourly cap.
        assert len(says(sapp, "trade")) == 2
        await sapp.realm.close()
    run(go())


# -- answers, gz and the model call ----------------------------------------------------------------

def test_answers_his_trade_line_once(sapp):
    place(sapp.store, VEX, "uc_bank")
    place(sapp.store, MARA, "tb_bank")

    async def go():
        await start(sapp)
        ev = protocol.ChannelChat(5000, "trade", 1, "Him", ORGRIMMAR, "lol <Testguild> wiped on patchwerk", False)
        await sapp.app.event(ev)
        await settle(sapp.realm)
        await sapp.app.event(ev)  # The same line again.
        await sapp.app.event(protocol.ChannelChat(6000, "trade", 1, "Him", ORGRIMMAR, "wts frostweave", False))
        await settle(sapp.realm)
        [answer] = says(sapp, "trade")
        assert answer.guid == VEX
        assert [r["kind"] for r in lines(sapp.store)] == ["answer"]
        prompt = sapp.llm.calls[0]["messages"][-1]["content"]
        assert "wiped on patchwerk" in prompt
        await sapp.realm.close()
    run(go())


def test_gz_after_a_server_kill(sapp):
    place(sapp.store, VEX, "uc_bank")
    place(sapp.store, MARA, "tb_bank")
    with sapp.store.transaction():
        fact = sapp.store.conn.execute(
            "INSERT INTO realm_fact(kind,text,source,ref,public,ts) VALUES ('kill',?, 'event','event:1',1,?)",
            ("Testcast killed Patchwerk (25).", sapp.clock.now())).lastrowid
        for guild in (TESTGUILD, OTHERGUILD):
            sapp.store.conn.execute("INSERT INTO realm_knows(fact,guild,since,how) VALUES (?,?,?,'server')",
                                    (fact, guild, sapp.clock.now()))
    row = NS(id=fact, text="Testcast killed Patchwerk (25).")

    async def go():
        await start(sapp)
        assert sapp.speech.gz(row) is True
        await settle(sapp.realm)
        assert sapp.speech.gz(row) is False  # Once per fact.
        [line] = says(sapp, "trade")
        assert line.guid == VEX and [r["kind"] for r in lines(sapp.store)] == ["gz"]
        assert "Patchwerk" in sapp.llm.calls[0]["messages"][-1]["content"]
        await sapp.realm.close()
    run(go())


def test_realm_kill_event_brings_its_gz(sapp, monkeypatch):
    place(sapp.store, VEX, "uc_bank")
    seen = []
    monkeypatch.setattr(sapp.speech, "_kill_fact", lambda ev: NS(id=9, text="a kill"))
    monkeypatch.setattr(sapp.speech, "gz", lambda fact: seen.append(fact.id) or True)

    async def go():
        await start(sapp)
        kill = protocol.RealmKill(
            ts=2000, kill_id="0" * 32 + ":1", native_epoch=1, guild_id=21, human_guid=1, killed_at_ms=1000,
            verified_at_ms=1500, fight_started_at_ms=500, fight_ms=500, attempts=1, map=533, instance_id=7,
            boss_id=15931, difficulty=1, size=25, heroic=False, human_present=True, cast_members=(101,))
        await sapp.app.event(kill)
        assert seen == [9]
        await sapp.realm.close()
    run(go())


def test_compose_uses_background_and_deadline(sapp):
    place(sapp.store, VEX, "uc_bank")
    retire(sapp.store, OTHERGUILD)

    async def go():
        await start(sapp)
        now = sapp.clock.now()
        await speak(sapp)
        assert sapp.app.scheduler.submitted == [(Priority.BACKGROUND, now + LIMITS.rival_line_deadline_s)]
        [call] = sapp.llm.calls
        assert call["model"] == "synthetic-live-model" and call["think"] is False
        assert call["options"] == dict(LIVE.options) and call["options"]["num_ctx"] == 4096
        system, user = call["messages"]
        assert system["role"] == "system" and "Wrath of the Lich King" in system["content"]
        assert "Vex" in user["content"] and "Testguild" in user["content"]
        assert GUILD_NAME not in user["content"] and GUILD_NAME not in system["content"]
        assert "Boss" in user["content"]  # HumanIdentity.describe(include_real_name=False).
        assert "Quillon" not in user["content"] and "Quillon" not in system["content"]
        await sapp.realm.close()
    run(go())


def test_prompt_carries_cards_and_known_facts(sapp, monkeypatch):
    place(sapp.store, VEX, "uc_bank")
    retire(sapp.store, OTHERGUILD)
    cards = sapp.app.data_dir / "realm" / "cards"
    cards.mkdir(parents=True)
    (cards / f"{VEX}.json").write_text(json.dumps({"temperament": "smug", "habits": ["brags", "rolls need"]}),
                                       encoding="utf-8")
    (cards / f"guild-{TESTGUILD}.txt").write_text("A hardcore progression guild. " * 100, encoding="utf-8")
    known = [NS(text="Testguild downed Sartharion.")]
    monkeypatch.setattr(sapp.realm.ledger, "known", lambda guild: known if guild == TESTGUILD else [])

    async def go():
        await start(sapp)
        await speak(sapp)
        user = sapp.llm.calls[0]["messages"][-1]["content"]
        assert "smug" in user and "brags" in user and "Sartharion" in user
        assert user.count("hardcore progression") <= 150 * 4 // len("A hardcore progression guild. ") + 1
        await sapp.realm.close()
    run(go())


def test_dropped_line_retried_once(tmp_path, store, clock, monkeypatch, caplog):
    s = make_app(tmp_path, store, clock, monkeypatch, llm=FakeLLM("hey cutie ;)"))
    place(store, VEX, "uc_bank")
    retire(store, OTHERGUILD)
    caplog.set_level(logging.INFO, logger="leeroy_mind.realm.speech")

    async def go():
        await start(s)
        await speak(s)
        assert len(s.llm.calls) == 2 and says(s) == []
        rows = lines(store)
        assert [(r["state"], r["reason"], r["command_id"]) for r in rows] == [("dropped", "flirt", None)] * 2
        assert [r.getMessage() for r in caplog.records if "realm_line_dropped" in r.getMessage()] == [
            f"realm_line_dropped id={rows[0]['id']} reason=flirt", f"realm_line_dropped id={rows[1]['id']} reason=flirt"]
        await s.realm.close()
    run(go())


def test_screen_guild_name_drops_and_retry_can_pass(tmp_path, store, clock, monkeypatch):
    s = make_app(tmp_path, store, clock, monkeypatch, llm=FakeLLM("Testcast are clowns", "lf2m ulduar 25"))
    place(store, VEX, "uc_bank")
    retire(store, OTHERGUILD)

    async def go():
        await start(s)
        await speak(s)
        assert [(r["state"], r["reason"]) for r in lines(store)] == [("dropped", "guild_name"), ("sent", None)]
        assert [c.text for c in says(s)] == ["lf2m ulduar 25"]
        await s.realm.close()
    run(go())


# -- refusals and restarts -----------------------------------------------------------------------

def refusing(s, error, *, kind=protocol.RealmSay, times=1):
    left = {"n": times}

    async def send(cmd, *, preserve_id=False):
        s.app.sent.append(cmd)
        if type(cmd) is kind and left["n"] > 0:
            left["n"] -= 1
            return Ack(0, cmd.id, False, error)
        return Ack(0, cmd.id, True, "")
    s.app.send = send


def test_busy_officer_skipped(sapp):
    place(sapp.store, VEX, "uc_bank")
    place(sapp.store, ROOK, "uc_bank")
    retire(sapp.store, OTHERGUILD)
    refusing(sapp, "busy:training_owner")

    async def go():
        await start(sapp)
        await speak(sapp)
        assert [(r["guid"], r["state"], r["reason"]) for r in lines(sapp.store)] == [
            (VEX, "refused", "busy_training_owner")]
        assert await speak(sapp) == "trade"  # A refused line is not a sent one: no gap needed.
        assert [c.guid for c in says(sapp)] == [VEX, ROOK]
        assert lines(sapp.store)[-1]["state"] == "sent"
        await sapp.realm.close()
    run(go())


def test_not_in_city_invalidates_the_place_record(sapp):
    place(sapp.store, VEX, "uc_bank")
    retire(sapp.store, OTHERGUILD)
    refusing(sapp, "not_in_city")

    async def go():
        await start(sapp)
        await speak(sapp)
        assert VEX not in realm_store.actor_places(sapp.store.conn)
        assert await speak(sapp) == "moving"
        await pump(sapp)
        assert [m.guid for m in moves(sapp)] == [ROOK]  # Vex rests a while; Rook goes.
        await sapp.realm.close()
    run(go())


def test_line_unknown_after_restart_never_resent(tmp_path, store, clock, monkeypatch):
    earlier = "earlier-process"
    ledger = DirectLedger(store, process_id=earlier, clock=clock)
    context = ActionContext(True, True, True, 1, earlier, 1, 21, 1)
    texts = {"dispatched": "wtb tank for naxx", "prepared": "lf healer os3d"}
    ids = {}
    for state, text in texts.items():
        cmd = protocol.RealmSay(uuid.uuid4().hex, VEX, "trade", text)
        with store.transaction():
            ids[state] = store.conn.execute(
                "INSERT INTO realm_line(ts,guid,guild,channel,text,command_id,kind,state) VALUES (?,?,?,?,?,?,?,?)",
                (clock.now() - 30, VEX, TESTGUILD, "trade", text, cmd.id, "rival", "prepared")).lastrowid
            ledger.prepare(cmd, owner="speech", ref=f"line:{ids[state]}", context=context, expects_job=False)
        if state == "dispatched":
            ledger.before_send(cmd, context=context)
            with store.transaction():
                store.conn.execute("UPDATE realm_line SET state='dispatched' WHERE id=?", (ids[state],))
    s = make_app(tmp_path, store, clock, monkeypatch)  # The restart: a new process attaches.
    place(store, VEX, "uc_bank")
    retire(store, OTHERGUILD)

    async def go():
        await start(s)
        await step(s, 2)
        rows = {r["id"]: r for r in lines(store)}
        assert (rows[ids["dispatched"]]["state"], rows[ids["dispatched"]]["reason"]) == ("unknown", "recovered_dispatched")
        assert (rows[ids["prepared"]]["state"], rows[ids["prepared"]]["reason"]) == ("refused", "recovered_prepared")
        assert await speak(s) == "trade"  # A fresh line, never the old ones again.
        assert [c.text for c in says(s)] == ["lf1m heroic voa, pst"]
        assert len(lines(store)) == 3
        await s.realm.close()
    run(go())


# -- the officers' places --------------------------------------------------------------------------

class RaidNight:
    def __init__(self, state):
        self.state, self.session, self._closing = NS(name=state), None, []


def test_officer_move_refused_only_for_held_bodies(sapp):
    """His raid holds exactly his roster and his group: an officer in his group is refused and
    another one moved; with an unreadable group the hold falls back to everyone, refusing both."""
    sapp.app.guild = NS(raid_night=RaidNight("WAITING_CALL"), offline=None)
    sapp.app.world.group = NS(members=(VEX,), leader_guid=1, raid=True)
    retire(sapp.store, OTHERGUILD)

    async def go():
        await start(sapp, online=False)
        for _ in range(3):
            await step(sapp)
            await pump(sapp)
        assert [m.guid for m in moves(sapp)] == [ROOK]  # Vex was never on the wire: held.
        refused = [r for r in sapp.realm.action_store.records() if r.result_reason == "held"]
        assert [json.loads(r.payload)["guid"] for r in refused] == [VEX]
        await arrive(sapp, moves(sapp)[0])
        assert set(realm_store.actor_places(sapp.store.conn)) == {ROOK}
        await sapp.realm.close()
    run(go())


def test_hold_everyone_fallback_refuses_both(sapp):
    sapp.app.guild = NS(raid_night=RaidNight("WAITING_CALL"), offline=None)
    sapp.app.world.group = NS(members=[VEX], leader_guid=1, raid=True)  # Unreadable: a list.
    retire(sapp.store, OTHERGUILD)

    async def go():
        await start(sapp, online=False)
        for _ in range(4):
            await step(sapp)
            await pump(sapp)
        assert moves(sapp) == []
        refused = [json.loads(r.payload)["guid"] for r in sapp.realm.action_store.records()
                   if r.result_reason == "held"]
        assert sorted(refused) == [VEX, ROOK]
        await sapp.realm.close()
    run(go())


def test_officers_positioned_at_hello_while_offline_and_after_the_raid(sapp):
    night = RaidNight("CALLED")  # His raid holds his roster and his group, never the rivals.
    sapp.app.guild = NS(raid_night=night, offline=None)
    sapp.app.world.group = NS(members=(103,), leader_guid=1, raid=True)

    async def go():
        await start(sapp)  # He is online: the hello pass still places both guilds.
        await step(sapp)
        await pump(sapp)
        first = moves(sapp)
        assert sorted((m.guid, m.place) for m in first) == [(VEX, "uc_bank"), (MARA, "uc_bank")]
        for move in first:
            await arrive(sapp, move)
        with sapp.store.conn:
            sapp.store.conn.execute("UPDATE realm_actor SET place='' WHERE guid=?", (MARA,))
        await later(sapp, 5)  # The queue paces body commands, two a second.
        await step(sapp)
        await pump(sapp)
        assert len(moves(sapp)) == 2  # Online, mid-raid: no pass until the raid ends.
        night.state = NS(name="ENDED")
        await step(sapp)
        await pump(sapp)
        assert [m.guid for m in moves(sapp)[2:]] == [MARA]
        await arrive(sapp, moves(sapp)[-1])
        with sapp.store.conn:
            sapp.store.conn.execute("UPDATE realm_actor SET place='' WHERE guid=?", (VEX,))
        await sapp.app.event(Logout(0, 1, True))
        sapp.app.world.human_online = False
        await later(sapp, 5)
        await step(sapp)
        await pump(sapp)
        assert [m.guid for m in moves(sapp)[3:]] == [VEX]  # Offline: placed again.
        await arrive(sapp, moves(sapp)[-1])
        await later(sapp, 5)
        await step(sapp)
        await pump(sapp)
        assert len(moves(sapp)) == 4
        await sapp.realm.close()
    run(go())


def test_any_realm_move_done_records_the_place(sapp):
    async def go():
        await start(sapp)
        move = protocol.RealmMove(uuid.uuid4().hex, MARA, "dal_bank")
        sapp.realm.queue.submit(move, owner="realm_day")
        await pump(sapp)
        await arrive(sapp, move)
        assert realm_store.actor_places(sapp.store.conn)[MARA][0] == "dal_bank"
        await sapp.realm.close()
    run(go())


# -- the gankers ---------------------------------------------------------------------------------

def test_gankers_never_call_model(sapp):
    async def go():
        await start(sapp)
        await sapp.app.event(protocol.PvpKill(1000, GRIM, 1, 571, 4197))
        await settle(sapp.realm)
        await sapp.app.event(protocol.DuelEnd(2000, 1, GRIM, "won", 1, 1637, True))
        await settle(sapp.realm)
        await sapp.app.event(protocol.PvpKill(3000, VEX, 1, 571, 4197))  # Not a ganker.
        await settle(sapp.realm)
        emotes = [c for c in sapp.app.sent if type(c) is protocol.RealmEmote]
        assert [(e.guid, e.target_guid) for e in emotes] == [(GRIM, 1), (GRIM, 1)]
        assert emotes[0].emote in {"spit", "laugh"} and emotes[1].emote == "wave"
        assert sapp.llm.calls == [] and lines(sapp.store) == []
        assert sapp.speech.emote_for(protocol.PvpKill(4000, 999, 1, 571, 4197)) is None
        await sapp.realm.close()
    run(go())


# -- item links ----------------------------------------------------------------------------------

def test_a_rival_line_links_its_items_and_keeps_the_plain_form(tmp_path, store, clock, monkeypatch):
    from leeroy_mind.talk.items import ItemLinker
    s = make_app(tmp_path, store, clock, monkeypatch,
                 llm=FakeLLM("wts [frozen orb] x4, also {{item:6948}} cheap"))
    s.app.engine.items = ItemLinker(lambda: [dict(id=43102, name="Frozen Orb", quality=3, ilvl=80,
                                                  req_level=0)], clock.now)
    place(s.store, VEX, "uc_bank")

    async def go():
        await start(s)
        assert await speak(s) == "trade"
        await pump(s)
        [line] = says(s, "trade")
        assert line.text == "wts {{item:43102}} x4, also {item:6948}} cheap"
        [row] = lines(s.store)
        assert row["text"] == "wts [Frozen Orb] x4, also {item:6948}} cheap"
        await s.realm.close()
    run(go())


def test_a_rival_line_without_item_data_is_defused(tmp_path, store, clock, monkeypatch):
    s = make_app(tmp_path, store, clock, monkeypatch, llm=FakeLLM("wts {{item:6948}} [frozen orb]"))
    place(s.store, VEX, "uc_bank")

    async def go():
        await start(s)
        assert await speak(s) == "trade"
        await pump(s)
        [line] = says(s, "trade")
        assert line.text == "wts {item:6948}} [frozen orb]" == lines(s.store)[0]["text"]
        await s.realm.close()
    run(go())
