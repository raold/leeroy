"""Room proximity uses P1c's live API, or logs without inventing a fallback gain."""
import logging
import sys
from dataclasses import replace
from types import ModuleType

import pytest

from conftest import run

from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.voice.together import RoomTogether
from voice.conftest import HUMAN, status
from voice.test_voice_movement import elapse, group, ready, set_persona


@pytest.fixture
def attraction(monkeypatch):
    module = ModuleType("leeroy_mind.nightly.attraction")
    module.TOGETHER_MIN_S, module.TOGETHER_BLOCK_S = 600, 1800
    module.calls, module.gains, module.science_calls = [], [], []
    module.reconcile = lambda store, guid, now: ("drawn", False)
    def science(store, guid, **kw):
        assert store._depth > 0
        module.science_calls.append((guid, kw))
        return 0.73
    def gain(act, persona, *, science):
        module.gains.append((act, persona, science))
        return 2.0, 0.8
    def apply(store, **kw):
        assert store._depth > 0
        module.calls.append(kw)
        return "fond"
    module.science_for, module.gain, module.apply_act = science, gain, apply
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


async def together(v):
    m = await ready(v)
    for guid in v.ext.on_comms:
        set_persona(v, guid, identity={"player_gender": "male"}, romance={"stance": "none"})
    set_persona(v, 102, identity={"player_gender": "female"}, romance={"stance": "drawn"})
    m.together.tick(v.clock.now())
    return m


@pytest.mark.parametrize("seconds,amount", [(540, None), (1500, 2.0), (2700, 2.0), (4500, 4.0)])
def test_shared_time_threshold_and_blocks(vapp, attraction, seconds, amount):
    async def check():
        m = await together(vapp)
        elapse(vapp, seconds)
        m.move(102, "lore")
        if amount is None:
            assert not attraction.calls
        else:
            assert len(attraction.calls) == 1
            assert attraction.calls[0] == {"human": HUMAN, "guid": 102, "act": "quest_together", "amount": amount,
                                           "mult": 0.8, "ref": f"session:{vapp.app.session_id}",
                                           "ts": vapp.clock.now(), "live": True, "stance": "drawn"}
    run(check())


def test_stored_persona_science_and_after_commit_invalidation(vapp, attraction, monkeypatch):
    async def check():
        m = await together(vapp)
        cached = vapp.app.personas.get(102)
        fresh = replace(cached, real_life=replace(cached.real_life, job="A changed stored job"))
        save_persona(vapp.app.store, fresh, source="seed", ts=vapp.clock.now(), race="Night Elf", class_name="Druid")
        invalidated = []
        monkeypatch.setattr(vapp.app.personas, "invalidate", lambda guid: invalidated.append((guid, vapp.app.store._depth > 0)))
        elapse(vapp, 2700)
        m.move(102, "lore")
        assert attraction.gains == [("quest_together", load_persona(vapp.app.store, 102), 0.73)]
        assert attraction.gains[0][1] != cached
        assert len(attraction.science_calls) == 1
        assert attraction.science_calls[0][1]["local"] == vapp.clock.local()
        assert invalidated == [(102, False)]
    run(check())


@pytest.mark.parametrize("room,in_party", [("raid", False), ("raid2", False), ("guild_hall", True), ("afk", False)])
def test_no_raid_party_or_afk_double_count(vapp, attraction, room, in_party):
    async def check():
        m = await together(vapp)
        if in_party:
            group(vapp, m, (1, 102), raid=False)
        vapp.ext.rooms.move(HUMAN, room)
        vapp.ext.rooms.move(102, room)
        m.together.tick(vapp.clock.now())
        m.together.close_all(elapse(vapp, 2700))
        assert not attraction.calls
    run(check())


def test_men_and_none_never_open(vapp, attraction):
    async def check():
        m = await together(vapp)
        set_persona(vapp, 102, romance={"stance": "none"})
        set_persona(vapp, 101, romance={"stance": "lust"})
        m.together.tick(vapp.clock.now())
        assert not m.together.open
        m.together.close_all(elapse(vapp, 2700))
        assert not attraction.calls
    run(check())


def test_without_p1c_logs_only(vapp, monkeypatch, caplog):
    async def check():
        monkeypatch.setitem(sys.modules, "leeroy_mind.nightly.attraction", None)
        m = await together(vapp)
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            m.together.close_all(elapse(vapp, 2700))
        records = [r for r in caplog.records if getattr(r, "leeroy_event", None) == "room_together"]
        assert len(records) == 1
        assert records[0].leeroy_fields["reason"] == "api_absent"
        assert records[0].leeroy_fields["seconds"] == 2700
        assert not records[0].leeroy_fields["credited"]
    run(check())


def test_service_drop_closes_immediately_and_recovery_starts_fresh(vapp, attraction):
    async def check():
        m = await together(vapp)
        elapse(vapp, 1500)
        vapp.fake.greeted = False
        m.on_connection(False)
        assert len(attraction.calls) == 1 and not m.together.open
        m.together.tick(elapse(vapp, 3600))
        assert not m.together.open
        vapp.fake.greeted, vapp.fake.status = True, status()
        m.on_connection(True)
        m.together.close_all(elapse(vapp, 600))
        assert len(attraction.calls) == 2
    run(check())


def test_logout_and_reset_do_not_reopen_shared_time(vapp, attraction):
    async def check():
        m = await together(vapp)
        m.on_logout(102)  # no elapsed time, no credit
        assert not m.together.open
        m.together.tick(elapse(vapp, 2700))
        assert not attraction.calls
        m.reset(vapp.clock.now())
        assert not m.together.open
    run(check())


def test_failed_credit_rolls_back_but_does_not_block_the_room_move(vapp, attraction, caplog):
    async def check():
        m = await together(vapp)
        def fail(store, **kw):
            store.set_meta("test_room_credit", "must roll back")
            raise RuntimeError("private details must stay out of the log")
        attraction.apply_act = fail
        elapse(vapp, 2700)
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            assert m.move(102, "lore")
        assert vapp.app.store.get_meta("test_room_credit") is None
        assert vapp.ext.rooms.where[102] == "lore"
        assert "credit_failed" in caplog.text
        assert "private details" not in caplog.text
    run(check())


def test_clean_stop_closes_credit_once_before_returns_and_restart_excludes_downtime(vapp, attraction):
    async def check():
        m = await together(vapp)
        m._visit(101, "penalty_box", 4000, "penalty", vapp.clock.now())
        m.prepare_shutdown(elapse(vapp, 1500))
        assert len(attraction.calls) == 1 and not m.together.open
        assert vapp.ext.rooms.where[101] == "guild_hall"
        elapse(vapp, 3600)  # no ticking between shutdown and the restored session
        m.tick_rooms(vapp.clock.now())
        m.together.close_all(elapse(vapp, 600))
        assert len(attraction.calls) == 2
        assert [call["amount"] for call in attraction.calls] == [2.0, 2.0]
    run(check())
