"""D6 Part A task 3: intents, strings that are nudges, carried out in her own way or reported
back with a reason."""
import asyncio
import logging
import random
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run, seed_test_cast
from selfies.fakes import mind_app
from leeroy_mind.awareness.card import CardContext, card
from leeroy_mind.discord import store as dstore
from leeroy_mind.endpoint import BadRequest, string_args
from leeroy_mind.godmode import intents as I
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import PersonaCache, load_persona
from leeroy_mind.protocol import Chat
from leeroy_mind.server import engine as se, records, runtime
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.tuning import changes as C

HUMAN = HumanIdentity(1, "Testmage", "Zan", "Quillon", True)
GODMODE = "[server]\nenabled=true\n[guards]\nenabled=true\n[godmode]\nenabled=true\n"


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    store.conn.commit()
    return store


def pic(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def make(store, member, kind, now=None, **args):
    return I.create(store, member=member, kind=kind, args=args, human=HUMAN,
                    now=store.clock.now() if now is None else now, pic=pic(store))


def test_intent_reaches_her_card(cast):
    i = make(cast, 102, "post", channel="irl", about="the new kitten she adopted")
    lines = I.card_lines(cast.conn, 102, "discord:irl", human=1)
    assert lines == ["You meant to post here about the new kitten she adopted."]
    assert I.card_lines(cast.conn, 102, "discord:general", human=1) == []
    assert I.card_lines(cast.conn, 103, "discord:irl", human=1) == []
    rendered = card(cast.conn, 102, context=CardContext(
        channel="discord:irl", readers=(101, 102, 103), server=True, intents=tuple(lines))).render()
    assert "You meant to post here about the new kitten she adopted." in rendered
    assert "order" not in rendered.casefold() and "he asked" not in rendered.casefold()
    # in game, the game part carries it (part B's game kinds use this)
    game = card(cast.conn, 102, context=CardContext(channel="guild", intents=("You meant to sit.",)))
    assert "You meant to sit." in game.parts[0].render()
    # a plain card without intents is what it was
    plain = card(cast.conn, 102, context=CardContext(channel="discord:irl", readers=(101, 102, 103),
                                                    server=True))
    assert "meant to" not in plain.render()
    dm = make(cast, 103, "dm_him", about="the raid schedule")
    assert I.card_lines(cast.conn, 103, "dm:103", to_guid=1, human=1) == [
        "You meant to message him about the raid schedule."]
    assert I.card_lines(cast.conn, 103, "discord:general", human=1) == []
    bring = make(cast, 104, "bring_up", topic="the Saurfang wipe")  # one channel: general
    assert I.get(cast.conn, bring)["args"]["channel"] == "general"
    assert I.card_lines(cast.conn, 104, "discord:general", human=1) == ["You meant to bring up the Saurfang wipe."]
    assert I.card_lines(cast.conn, 104, "discord:strategy", human=1) == []  # never every channel
    assert I.card_lines(cast.conn, 104, "dm:104", to_guid=1, human=1) == ["You meant to bring up the Saurfang wipe."]
    assert I.card_lines(cast.conn, 104, "gdm:1", human=1) == []  # never in a group DM
    assert {i, dm, bring} == {r["id"] for r in I.pending(cast.conn)}


def test_outreach_and_an_intent_never_share_a_turn(tmp_path):
    """The card shows no intent line on a turn that carries an outreach item or an intent."""
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind
    from leeroy_mind.llm.scheduler import Priority
    app = mind_app(tmp_path, toml=GODMODE)
    try:
        app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, aware=True))
        I.create(app.store, member=102, kind="bring_up", args={"topic": "the Saurfang wipe"},
                 human=app.engine.human, now=app.clock.now(), pic=app.tuning.picture())
        turn = Turn(TurnKind.OPEN, 102, "discord:general", priority=Priority.BACKGROUND)
        plain = app.engine.live_review.block(turn, TurnMeta("t1", review_required=True), close=False)
        assert "Saurfang" in plain
        carried = TurnMeta("t2", review_required=True, outreach_item=5)
        assert "Saurfang" not in app.engine.live_review.block(turn, carried, close=False)
        own = TurnMeta("t3", review_required=True, godmode_intent=1)
        assert "Saurfang" not in app.engine.live_review.block(turn, own, close=False)
    finally:
        run(app.stop())


@pytest.mark.parametrize("kind,setup,args,reason", [
    ("react", "feud", dict(emoji="👍"), "feud"),
    ("react", "rival", dict(emoji="❤️"), "stance"),
    ("react", "deleted", dict(emoji="👍"), "deleted"),
    ("post", "away", dict(channel="irl", about="her weekend"), "away"),
    ("dm_him", "cold", dict(about="the raid"), "stance"),
    ("bring_up", "feud_named", dict(topic="how much she loves Coros"), "feud"),
    ("post", "claim", dict(channel="general", about="I don't hate Saurfang"), "claims"),
])
def test_unfit_intent_is_reported_not_taken_with_reason(cast, kind, setup, args, reason):
    now = cast.clock.now()
    message = dstore.post(cast, channel="general", author=103, origin="mind", text="A thought",
                          now=now)
    if kind == "react":
        args = dict(args, message=message)
    ident = make(cast, 102, kind, **args)
    if setup == "feud" or setup == "feud_named":
        cast.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES (102,103,?,'x')", (now,))
    elif setup == "rival":
        cast.put_relationship(Relationship(a=102, b=103, affinity=-50))
    elif setup == "deleted":
        dstore.delete(cast, message, author=103, now=now)
    elif setup == "away":
        cast.conn.execute("INSERT INTO away(guid,reason,left_at) VALUES (102,'trip',?)", (now,))
    elif setup == "cold":
        cast.put_relationship(Relationship(a=102, b=1, affinity=-60))
    elif setup == "claim":
        cast.conn.execute(
            "INSERT INTO member_claim(guid,kind,subject,text,polarity,source,source_line,"
            "first_seen,last_seen,seen,expires,superseded_by,embedding) "
            "VALUES (102,'opinion','saurfang','I hate Saurfang',-1,'chat',NULL,?,?,1,NULL,NULL,?)",
            (now, now, bytes(2048)))
    cast.conn.commit()
    intent = I.get(cast.conn, ident)
    ok, why = I.fit(cast.conn, intent, PersonaCache(cast), now=now)
    assert (ok, why) == (False, reason)
    app = SimpleNamespace(store=cast, clock=cast.clock, personas=PersonaCache(cast), server=None,
                          human_guid=1)
    from leeroy_mind.godmode.service import Godmode
    run(Godmode(app, None).tick(now))
    row = I.get(cast.conn, ident)
    assert (row["status"], row["reason"]) == ("not_taken", reason)


def test_a_fit_intent_stays_pending_and_his_cancel_settles_it(cast):
    ident = make(cast, 102, "post", channel="irl", about="her weekend plans")
    assert I.fit(cast.conn, I.get(cast.conn, ident), PersonaCache(cast), now=cast.clock.now()) == (True, "ok")
    assert I.settle(cast, ident, status="cancelled", reason="owner", now=cast.clock.now())
    assert not I.settle(cast, ident, status="taken", now=cast.clock.now())
    assert I.get(cast.conn, ident)["status"] == "cancelled"


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = mind_app(tmp_path, toml=GODMODE)
    app.server.lease._held = True
    app.engine._login_at = float("-inf")
    app.world.human_online = False
    app.server.engine.rng = random.Random(19)
    monkeypatch.setattr(se.rhythm, "length", lambda rng: 2)
    monkeypatch.setattr(se, "answer_delay", lambda state, rng: 0)
    app.server.engine.rng.random = lambda: .5
    app.turns = []

    async def speak(turn, meta):
        app.turns.append((turn, meta))
        meta.turn_id = "test-" + str(len(app.turns))
        with app.store.transaction():
            channel = turn.channel.removeprefix("discord:")
            message = dstore.post(app.store, channel=channel, author=turn.speaker, origin="mind",
                                  text="An invented thought", turn=meta.turn_id,
                                  reply_to=meta.discord.reply_to if meta.discord else None,
                                  now=app.clock.now())
            records.posted(app.store, app.clock.local().date().isoformat(), app.clock.now(),
                           conversation=meta.server_conversation)
            if meta.on_discord_record:
                meta.on_discord_record(message, message, app.clock.now())
        return ["An invented thought"]
    monkeypatch.setattr(app.engine, "speak_meta", speak)
    yield app
    run(app.stop())


def _string(app, member, kind, **args):
    reply = run(app.godmode.handle({"action": "create", "member": member, "kind": kind, "args": args}))
    assert reply["ok"], reply
    return reply["id"]


async def _finished(engine):
    for _ in range(1000):  # its snapshots read in a worker thread
        if not engine.live():
            return
        await asyncio.sleep(0.005)
    pytest.fail("conversation did not end")


def _only_intents(app, monkeypatch):
    real = runtime.snapshot

    async def snapshot(a, now, *, include_seeds=False):
        observed = await real(a, now, include_seeds=include_seeds)
        observed.seeds = [s for s in observed.seeds if s.kind == "intent"]
        return observed
    monkeypatch.setattr(runtime, "snapshot", snapshot)


@pytest.mark.parametrize("kind", ["post", "thread"])
def test_post_and_thread_are_taken_through_the_server_engine(app, monkeypatch, kind):
    _only_intents(app, monkeypatch)
    args = dict(channel="irl", about="the kitten she adopted")
    if kind == "thread":
        args["name"] = "Kitten pics"
    ident = _string(app, 102, kind, **args)

    async def go():
        await app.server.engine.tick(app.clock.now())
        await _finished(app.server.engine)
    run(go())
    first_turn, first_meta = app.turns[0]
    assert first_turn.speaker == 102 and first_turn.channel == "discord:irl"
    assert "the kitten she adopted" in first_turn.instruction and "own words" in first_turn.instruction
    assert first_meta.godmode_intent == ident and first_meta.why == "server:intent"
    row = I.get(app.store.conn, ident)
    assert (row["status"], row["reason"]) == ("taken", "sent")
    opener = app.store.conn.execute("SELECT id FROM discord_message WHERE author=102 ORDER BY id").fetchone()[0]
    threads = app.store.conn.execute("SELECT parent,name FROM discord_thread").fetchall()
    assert [tuple(t) for t in threads] == ([(opener, "Kitten pics")] if kind == "thread" else [])
    assert len(app.turns) == 2  # the talk went on after her opener


def test_react_is_taken_by_the_reactor(app, monkeypatch):
    message = dstore.post(app.store, channel="general", author=103, origin="mind",
                          text="Raid tonight?", now=app.clock.now())
    ident = _string(app, 102, "react", message=message, emoji="🎉")
    monkeypatch.setattr("leeroy_mind.server.reacting.presence_of",
                        lambda *a, **k: SimpleNamespace(state="online"))
    monkeypatch.setattr(app.clock, "sleep", lambda s: asyncio.sleep(0))

    async def go():
        await app.godmode.tick(app.clock.now())
        for _ in range(100):
            if not app.godmode._tasks:
                break
            await asyncio.sleep(0)
    run(go())
    row = I.get(app.store.conn, ident)
    assert (row["status"], row["reason"]) == ("taken", "reacted")
    reacted = app.store.conn.execute("SELECT member,emoji FROM discord_reaction WHERE message=?",
                                     (message,)).fetchall()
    assert [tuple(r) for r in reacted] == [(102, "🎉")]


def test_dm_him_is_taken_on_the_dm_opener_path_without_outreach(app, monkeypatch):
    ident = _string(app, 103, "dm_him", about="the raid schedule")
    monkeypatch.setattr("leeroy_mind.server.dms.presence_of",
                        lambda *a, **k: SimpleNamespace(state="online"))
    app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, chattiness=4))

    async def go():
        await app.godmode.tick(app.clock.now())
        for _ in range(100):
            if not app.godmode._tasks:
                break
            await asyncio.sleep(0)
    run(go())
    turn, meta = app.turns[-1]
    assert turn.channel == "dm:103" and turn.to_guid == app.human_guid and "the raid schedule" in turn.instruction
    assert meta.godmode_intent == ident and meta.outreach_item is None and meta.why == "server:dm"
    row = I.get(app.store.conn, ident)
    assert (row["status"], row["reason"]) == ("taken", "sent")


def test_bring_up_expires_when_never_mentioned(app):
    ident = _string(app, 104, "bring_up", topic="the Saurfang wipe")
    other = _string(app, 105, "bring_up", topic="the Saurfang wipe")
    app.godmode.on_line(1, Chat(ts=0, channel="discord:general", from_guid=104, from_name="",
                                to_guid=0, text="anyone up for dailies", map=0, zone=0))
    app.godmode.on_line(2, Chat(ts=0, channel="gdm:1", from_guid=105, from_name="",
                                to_guid=0, text="still mad about saurfang", map=0, zone=0))
    app.godmode.on_line(3, Chat(ts=0, channel="guild", from_guid=105, from_name="",
                                to_guid=0, text="still mad about saurfang", map=0, zone=0))
    assert I.get(app.store.conn, ident)["status"] == "pending"
    assert I.get(app.store.conn, other)["status"] == "pending"  # group DMs and game lines never count
    app.godmode.on_line(4, Chat(ts=0, channel="discord:strategy", from_guid=105, from_name="",
                                to_guid=0, text="still thinking about that saurfang mess", map=0, zone=0))
    assert I.get(app.store.conn, other)["status"] == "pending"  # not her intent's channel
    app.godmode.on_line(5, Chat(ts=0, channel="discord:irl", from_guid=105, from_name="",
                                to_guid=0, text="still thinking about that saurfang mess", map=0, zone=0))
    assert I.get(app.store.conn, other)["status"] == "taken"
    run(app.godmode.tick(app.clock.now() + I.DUE_S + 1))
    row = I.get(app.store.conn, ident)
    assert (row["status"], row["reason"]) == ("expired", "due")


def test_intent_text_is_screened_and_never_logged(app, caplog):
    caplog.set_level(logging.INFO)
    secret = "the secret plan for Icecrown"
    ident = _string(app, 102, "post", channel="general", about=secret)
    assert secret not in caplog.text and "Icecrown" not in caplog.text
    for args, code in ((dict(channel="general", about="her nude photos"), "explicit"),
                       (dict(channel="general", about="what Pat Reeve said"), "real_name"),
                       (dict(channel="rules", about="anything"), "channel"),
                       (dict(channel="officer-chat", about="the bank"), "audience")):
        reply = run(app.godmode.handle({"action": "create", "member": 101, "kind": "post",
                                        "args": args}))
        assert not reply["ok"] and reply["code"] == code
        assert "nude" not in caplog.text and "Reeve" not in caplog.text
    # Part B: a game string is carried out at once, here reported back (he is not in game).
    reply = run(app.godmode.handle({"action": "create", "member": 101, "kind": "sit", "args": {}}))
    assert reply["ok"] and (reply["state"], reply["reason"]) == ("not_taken", "human_offline")
    listed = run(app.godmode.handle({"action": "list", "member": 102}))
    assert listed["intents"][0]["id"] == ident
    run(app.godmode.handle({"action": "cancel", "id": ident}))
    assert I.get(app.store.conn, ident)["status"] == "cancelled"
    assert secret not in caplog.text


def test_string_op_shape():
    assert string_args({"action": "list"}) == {"action": "list"}
    assert string_args({"action": "cancel", "id": 3}) == {"action": "cancel", "id": 3}
    with pytest.raises(BadRequest):
        string_args({"action": "create", "member": 101, "kind": "post", "args": {"about": "x"}})
    with pytest.raises(BadRequest):
        string_args({"action": "create", "member": 101, "kind": "hug", "args": {}})


def test_godmode_off_attaches_nothing(tmp_path):
    app = mind_app(tmp_path, toml="[server]\nenabled=true\n[guards]\nenabled=true\n")
    try:
        assert app.godmode is None and not getattr(app.engine, "godmode", None)
        observed = run(runtime.snapshot(app, app.clock.now(), include_seeds=True))
        assert not [s for s in observed.seeds if s.kind == "intent"]
    finally:
        run(app.stop())


def test_a_cancel_stops_a_reaction_already_on_its_way(app, monkeypatch):
    message = dstore.post(app.store, channel="general", author=103, origin="mind",
                          text="Raid tonight?", now=app.clock.now())
    ident = _string(app, 102, "react", message=message, emoji="🎉")
    monkeypatch.setattr("leeroy_mind.server.reacting.presence_of",
                        lambda *a, **k: SimpleNamespace(state="online"))
    gate = asyncio.Event()

    async def sleep(s):
        await gate.wait()
    monkeypatch.setattr(app.clock, "sleep", sleep)

    async def go():
        await app.godmode.tick(app.clock.now())
        assert app.godmode._busy == {ident}
        run_cancel = await app.godmode.handle({"action": "cancel", "id": ident})
        assert run_cancel["ok"]
        gate.set()
        for _ in range(100):
            if not app.godmode._tasks:
                break
            await asyncio.sleep(0)
    run(go())
    assert I.get(app.store.conn, ident)["status"] == "cancelled"
    assert not app.store.conn.execute("SELECT 1 FROM discord_reaction").fetchone()


def test_reactions_off_reports_a_react_not_taken(app):
    from dataclasses import replace as dc_replace
    message = dstore.post(app.store, channel="general", author=103, origin="mind",
                          text="Raid tonight?", now=app.clock.now())
    ident = _string(app, 102, "react", message=message, emoji="🎉")
    app.server.config = dc_replace(app.server.config, reactions=False)
    run(app.godmode.tick(app.clock.now()))
    row = I.get(app.store.conn, ident)
    assert (row["status"], row["reason"]) == ("not_taken", "reactions_off")


def _tick(app, now):
    """One tick and whatever it handed out, in one event loop."""
    async def go():
        await app.godmode.tick(now)
        for _ in range(200):
            if not app.godmode._tasks:
                return
            await asyncio.sleep(0)
    run(go())


def test_a_dropped_dm_him_backs_off_and_then_reports_why(app, monkeypatch):
    ident = _string(app, 103, "dm_him", about="the raid schedule")
    monkeypatch.setattr("leeroy_mind.server.dms.presence_of",
                        lambda *a, **k: SimpleNamespace(state="online"))
    app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, chattiness=4))
    calls = []

    async def dropped(turn, meta):  # the guards drop her draft: nothing is recorded
        calls.append(app.clock.now())
        return []
    monkeypatch.setattr(app.engine, "speak_meta", dropped)
    start = app.clock.now()
    for step in range(10):  # ten ticks, 30 s apart: five minutes
        monkeypatch.setattr(app.clock, "now", lambda t=start + 30 * step: t)
        _tick(app, app.clock.now())
    assert len(calls) == 1 and I.get(app.store.conn, ident)["status"] == "pending"
    t = start
    for _ in range(I.MAX_TRIES - 1):
        t += I.BACKOFF_S * 2 ** I.MAX_TRIES
        monkeypatch.setattr(app.clock, "now", lambda t=t: t)
        _tick(app, t)
    assert len(calls) == I.MAX_TRIES
    row = I.get(app.store.conn, ident)
    assert (row["status"], row["reason"]) == ("not_taken", "dropped")


@pytest.mark.parametrize("setting,reason", [("dms", "dms_off"), ("chattiness", "chattiness_off")])
def test_dm_him_says_why_when_her_openers_are_off(app, setting, reason):
    ident = _string(app, 103, "dm_him", about="the raid schedule")
    if setting == "dms":
        app.server.config = replace(app.server.config, dms_per_day=0)
    else:
        app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, chattiness=0))
    run(app.godmode.tick(app.clock.now()))
    row = I.get(app.store.conn, ident)
    assert (row["status"], row["reason"]) == ("not_taken", reason)


def test_dm_him_waits_after_an_opener_of_her_own(app, monkeypatch):
    from leeroy_mind.server.dms import OPEN_KEY
    _string(app, 103, "dm_him", about="the raid schedule")
    monkeypatch.setattr("leeroy_mind.server.dms.presence_of",
                        lambda *a, **k: SimpleNamespace(state="online"))
    app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, chattiness=4))
    app.store.set_meta(OPEN_KEY.format(guid=103), str(app.clock.now() - 30))
    _tick(app, app.clock.now())
    assert not app.turns
    app.store.set_meta(OPEN_KEY.format(guid=103), str(app.clock.now() - I.DM_FLOOR_S - 1))
    _tick(app, app.clock.now())
    assert app.turns and app.turns[-1][0].channel == "dm:103"


def test_bring_up_is_taken_only_when_the_opener_names_it(app, monkeypatch):
    _only_intents(app, monkeypatch)
    ident = _string(app, 104, "bring_up", topic="the Saurfang wipe")

    async def go():
        await app.server.engine.tick(app.clock.now())
        await _finished(app.server.engine)
    run(go())
    assert app.turns and app.turns[0][0].speaker == 104
    assert I.get(app.store.conn, ident)["status"] == "pending"  # "An invented thought"
    assert I.tries(app.store.conn, ident)[0] == 1
    assert not I.may_try(app.store.conn, ident, app.clock.now())  # backs off before trying again
    real = app.engine.speak_meta

    async def names_it(turn, meta):
        out = await real(turn, meta)
        with app.store.transaction():
            app.store.conn.execute("UPDATE discord_message SET text='that saurfang wipe though' "
                                   "WHERE turn=?", (meta.turn_id,))
        return out
    monkeypatch.setattr(app.engine, "speak_meta", names_it)
    row = app.store.conn.execute("SELECT id FROM discord_message ORDER BY id DESC").fetchone()
    with app.store.transaction():
        app.store.conn.execute("UPDATE discord_message SET text='that saurfang wipe though' "
                               "WHERE id=?", (row[0],))
    app.godmode.taken(ident, row[0], app.clock.now())
    assert I.get(app.store.conn, ident)["status"] == "taken"


def test_a_reaction_rechecks_her_and_the_intent_after_its_delay(app, monkeypatch):
    message = dstore.post(app.store, channel="general", author=103, origin="mind",
                          text="Raid tonight?", now=app.clock.now())
    ident = _string(app, 102, "react", message=message, emoji="🎉")
    states = iter(["online", "online"])
    monkeypatch.setattr("leeroy_mind.server.reacting.presence_of",
                        lambda *a, **k: SimpleNamespace(state=next(states, "online")))

    async def sleep(s):  # a feud begins while she waits to react
        app.store.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES (102,103,?,'x')",
                               (app.clock.now(),))
        app.store.conn.commit()
    monkeypatch.setattr(app.clock, "sleep", sleep)
    _tick(app, app.clock.now())
    row = I.get(app.store.conn, ident)
    assert (row["status"], row["reason"]) == ("not_taken", "feud")
    assert not app.store.conn.execute("SELECT 1 FROM discord_reaction").fetchone()


def test_a_reaction_waits_when_she_went_offline_during_its_delay(app, monkeypatch):
    message = dstore.post(app.store, channel="general", author=103, origin="mind",
                          text="Raid tonight?", now=app.clock.now())
    ident = _string(app, 102, "react", message=message, emoji="🎉")
    states = iter(["online", "offline"])
    monkeypatch.setattr("leeroy_mind.server.reacting.presence_of",
                        lambda *a, **k: SimpleNamespace(state=next(states, "offline")))

    async def sleep(s):
        return None
    monkeypatch.setattr(app.clock, "sleep", sleep)
    _tick(app, app.clock.now())
    assert I.get(app.store.conn, ident)["status"] == "pending"
    assert not app.store.conn.execute("SELECT 1 FROM discord_reaction").fetchone()
