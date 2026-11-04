"""Members' reactions to the rivals' lines (talk.py): one guild aside through the Realm's own
turn port, at most ten an hour, or now and then a member's own Trade answer, which only
realm_line records. Over the real Realm service with the small RealmApp surface."""
import sys
from types import SimpleNamespace

import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind.protocol import Hello
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.realm.speech import SpokenLine
from leeroy_mind.realm.talk import RealmTurnSource
from leeroy_mind.talk.turns import Turn, TurnKind
from realm.fakes import RealmApp, enabled
from realm.test_realm_speech import (DURATOR, FakeLLM, ORGRIMMAR, OTHERGUILD, TESTGUILD, UNDERCITY, VEX,
                                     later, lines, make_app, place, retire, says, settle, speak, start, step)


def online(store, guid, zone, *, on=True):
    with store.transaction():
        store.conn.execute("UPDATE cast_member SET online=?, zone=? WHERE guid=?", (int(on), zone, guid))


def rival_line(n, text="lf1m heroic voa"):
    return SpokenLine(id=n, kind="rival", guid=VEX, name="Vex", guild=TESTGUILD, guild_name="Testguild",
                      channel="trade", text=text, zone=UNDERCITY)


@pytest.fixture
def tapp(tmp_path, store, clock, monkeypatch):
    s = make_app(tmp_path, store, clock, monkeypatch)
    s.talk = s.realm.part("talk")
    s.talk.trade_chance = 0.0
    return s


def turns(s):
    return [call.args[0] for call in s.app.engine.speak.await_args_list]


def test_attaches_as_a_part(tapp):
    assert type(tapp.talk) is RealmTurnSource
    run(tapp.realm.close())


def test_reactions_need_no_speech_part(tmp_path, store, clock, monkeypatch):
    """Without the speech part (absent or broken) talk still attaches; every reaction is an aside."""
    monkeypatch.setitem(sys.modules, "leeroy_mind.realm.speech", None)
    monkeypatch.delitem(sys.modules, "leeroy_mind.realm.talk", raising=False)
    app = RealmApp(tmp_path, store, clock)
    app.personas = seed_test_cast(store)
    enabled(tmp_path, True)
    realm = attach_realm(app)
    talk = realm.part("talk")
    assert realm.part("speech") is None and type(talk).__name__ == "RealmTurnSource"
    talk.trade_chance = 1.0
    online(store, 101, ORGRIMMAR)

    async def go():
        await app.connection(True)
        await app.event(Hello(0, "test", "1", 21, 1))
        assert talk.react(None, rival_line(1)) == "aside"
        await realm.close()
    run(go())


def test_members_react_through_turns(tapp):
    place(tapp.store, VEX, "uc_bank")
    retire(tapp.store, OTHERGUILD)
    online(tapp.store, 101, ORGRIMMAR)

    async def go():
        await start(tapp)
        assert await speak(tapp) == "trade"
        [said] = says(tapp)
        await step(tapp, 2)
        [turn] = turns(tapp)
        assert type(turn) is Turn and turn.kind is TurnKind.ASIDE and turn.channel == "guild"
        assert turn.speaker == 101 and turn.max_lines == 1
        assert turn.instruction == (f'Vex from Testguild just said in Trade: "{said.text}". '
                                    "React in a few words, your way.")
        assert tapp.app.engine.speak.await_args.kwargs["why"] == "source:Realm"
        await step(tapp, 2)
        assert len(turns(tapp)) == 1  # One turn at most per line.
        await tapp.realm.close()
    run(go())


def test_at_most_ten_reactions_an_hour(tapp):
    online(tapp.store, 101, ORGRIMMAR)

    async def go():
        await start(tapp)
        results = [tapp.talk.react(None, rival_line(n)) for n in range(1, 13)]
        assert results.count("aside") == 10 and results[10:] == [None, None]
        assert len(await tapp.talk.propose(tapp.clock.now())) == 10
        await later(tapp, 3600)
        assert tapp.talk.react(None, rival_line(13)) == "aside"
        await tapp.realm.close()
    run(go())


def test_one_reaction_per_line_or_fact(tapp):
    online(tapp.store, 101, ORGRIMMAR)
    kill = SimpleNamespace(id=9, text="Testcast killed Patchwerk (25).")

    async def go():
        await start(tapp)
        assert tapp.talk.react(None, rival_line(1)) == "aside"
        assert tapp.talk.react(None, rival_line(1)) is None
        assert tapp.talk.react(kill, rival_line(2)) == "aside"
        assert tapp.talk.react(kill, rival_line(3)) is None  # The same kill again.
        assert len(await tapp.talk.propose(tapp.clock.now())) == 2
        assert await tapp.talk.propose(tapp.clock.now()) == []
        await tapp.realm.close()
    run(go())


def test_reacting_member_is_in_a_city_or_competitive_or_social(tapp):
    online(tapp.store, 101, DURATOR)              # An achiever outside the cities: not this one.
    online(tapp.store, 102, ORGRIMMAR, on=False)  # Offline.
    tapp.app.personas[103] = make_persona(103, "Coros", motivation="competitor")
    with tapp.store.transaction():
        tapp.store.conn.execute("INSERT INTO realm_kick(guid,ts,rank) VALUES (104,1.0,3)")
    online(tapp.store, 104, ORGRIMMAR)            # He kicked her: never in the audience.

    async def go():
        await start(tapp)
        assert tapp.talk.react(None, rival_line(1)) is None
        online(tapp.store, 103, DURATOR)          # Competitive: hears about it anyway.
        assert tapp.talk.react(None, rival_line(2)) == "aside"
        [turn] = await tapp.talk.propose(tapp.clock.now())
        assert turn.speaker == 103
        await tapp.realm.close()
    run(go())


def test_no_reaction_while_he_is_offline(tapp):
    online(tapp.store, 101, ORGRIMMAR)

    async def go():
        await start(tapp, online=False)
        assert tapp.talk.react(None, rival_line(1)) is None
        assert await tapp.talk.propose(tapp.clock.now()) == []
        await tapp.realm.close()
    run(go())


def test_member_trade_answer_is_realm_line_only(tapp):
    tapp.talk.trade_chance = 1.0
    online(tapp.store, 101, ORGRIMMAR)

    async def go():
        await start(tapp)
        assert tapp.talk.react(None, rival_line(1)) == "trade"
        await settle(tapp.realm)
        [answer] = says(tapp, "trade")
        assert answer.guid == 101 and answer.to_guid == 0
        [row] = lines(tapp.store)
        assert (row["guid"], row["guild"], row["channel"], row["kind"], row["state"]) == (
            101, 21, "trade", "member", "sent")
        assert await tapp.talk.propose(tapp.clock.now()) == []  # Instead of an aside, not as well.
        tapp.app.engine.say_as.assert_not_called()
        tapp.app.engine.speak.assert_not_called()
        assert tapp.store.conn.execute("SELECT COUNT(*) FROM utterance").fetchone()[0] == 0
        prompt = tapp.llm.calls[0]["messages"][-1]["content"]
        assert "Arkon" in prompt and "lf1m heroic voa" in prompt
        await tapp.realm.close()
    run(go())


def test_member_trade_answer_is_screened(tmp_path, store, clock, monkeypatch):
    s = make_app(tmp_path, store, clock, monkeypatch, llm=FakeLLM("hey cutie ;)"))
    talk = s.realm.part("talk")
    talk.trade_chance = 1.0
    online(store, 101, ORGRIMMAR)

    async def go():
        await start(s)
        assert talk.react(None, rival_line(1)) == "trade"
        await settle(s.realm)
        assert says(s) == [] and len(s.llm.calls) == 2
        assert [(r["kind"], r["state"], r["reason"]) for r in lines(store)] == [("member", "dropped", "flirt")] * 2
        await s.realm.close()
    run(go())


def test_member_answer_breaks_the_rivals_run(tapp):
    place(tapp.store, VEX, "uc_bank")
    place(tapp.store, 211, "tb_bank")
    online(tapp.store, 101, ORGRIMMAR)

    async def go():
        await start(tapp)
        for _ in range(2):
            await speak(tapp)
            await later(tapp, 200)
        assert len(says(tapp)) == 2 and await speak(tapp) is None  # Two in a row.
        tapp.talk.trade_chance = 1.0
        assert tapp.talk.react(None, rival_line(99)) == "trade"
        await settle(tapp.realm)
        assert [c.guid for c in says(tapp)][-1] == 101
        assert await speak(tapp) == "trade"  # The member's line broke the run.
        await tapp.realm.close()
    run(go())
