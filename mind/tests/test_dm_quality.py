"""DM quality (owner's live DM test, 2026-11-16: "mixup in context, not answering the questions,
repeating back what I said"). Four fixes, none scoped to a member or a medium:

1. what the beat brings in: its items shuffled per turn by a seeded draw, an item already brought
   in during the conversation never offered again, at most one Bring in per three of her turns
   there, and `use` null unless it bears directly on his last line;
3. the repetition hint names content words only, and an answer to his question is told to answer
   it first, plainly, in place of the hint's "do something else";
4. when his line asks her something the beat's answering wants lead and flirt reads as for when
   he is flirting; the DM length asks for as much or as little as the moment needs;
5. stock epithets.

Each rule is exercised on a guild turn and a whisper beside the DM. The relationship-note clean
(2) has its own tests (test_clean_notes.py, nightly/test_night_relations.py). The test cast and
invented lines only."""
import asyncio
import json
from dataclasses import replace

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.guards import Draft, phrases
from leeroy_mind.guards.review import Reader
from leeroy_mind.life.day import DayEvent
from leeroy_mind.life.seed import seeded
from leeroy_mind.protocol import Chat
from leeroy_mind.talk import asking
from leeroy_mind.talk import beat as beats
from leeroy_mind.talk import intent as I
from leeroy_mind.talk import moves
from leeroy_mind.talk.movelog import MoveRow
from leeroy_mind.talk.pipeline import Composed, TurnMeta
from leeroy_mind.talk.prompt import DISCORD_DM, render_instruction
from leeroy_mind.talk.turns import Turn, TurnKind

CHANNELS = [("guild", 0), ("whisper", HUMAN), ("dm:102", HUMAN)]
ITEMS = ["the glider race at the docks", "a bet about the fishing contest",
         "the lantern festival plan"]
KEYS = ["m11", "m12", "c13"]
QUESTION_FIRST = "Answer his question first, plainly, then whatever else"


@pytest.fixture
def clock():
    return engine_clock()


def _rig(store, clock, path, *, beat):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    return make_rig(store, clock, path, beat=beat)


@pytest.fixture
def rig(store, clock, tmp_path):
    r = _rig(store, clock, tmp_path, beat=True)
    yield r
    r.close()


@pytest.fixture
def plain(store, clock, tmp_path):
    """The beat off: the line's own prompt, as most channels have it today."""
    r = _rig(store, clock, tmp_path, beat=False)
    yield r
    r.close()


def last_user(call):
    return call["messages"][-1]["content"]


def beat_calls(rig):
    return [c for c in rig.chat_calls() if c["format"] is not None]


def line_calls(rig):
    return [c for c in rig.chat_calls() if c["format"] is None]


def his_turn(rig, channel, to_guid, text):
    """His line on `channel` (stored as the app stores it) and her answer's turn and meta."""
    if channel.startswith("dm:"):
        mid = dstore.post(rig.store, channel=channel, author=HUMAN, origin="human", text=text,
                          now=rig.clock.now())
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel=channel, to_guid=HUMAN,
                    addressee=HUMAN)
        meta = TurnMeta(rig.engine._new_id(), answers_human=True, why="discord:reply",
                        discord=DiscordTarget(reply_to=mid, thread=None))
        return turn, meta
    line = rig.store.insert_chat_line(  # his whisper goes to her
        Chat(ts=0, channel=channel, from_guid=HUMAN, from_name="Zan",
             to_guid=102 if channel == "whisper" else 0, text=text, map=0, zone=10),
        session_id=None, origin="human")
    rig.world.add_line(rig.store.chat_lines([line])[0], rig.clock.now())
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel=channel, to_guid=to_guid,
                addressee=HUMAN, trigger_line=line)
    return turn, TurnMeta(rig.engine._new_id(), answers_human=True,
                          whisper_reply=channel == "whisper")


def speak(rig, turn, meta):
    async def go():
        task = asyncio.create_task(rig.engine._speak(turn, meta))
        await rig.advance(200)
        return task.result()
    return rig.sync(go())


def keyed(rig, items=ITEMS, keys=KEYS, *, asking_=False):
    return Composed([{"role": "system", "content": "SYSTEM"},
                     {"role": "user", "content": "THE INSTRUCTION"}], [],
                    rig.engine.personas.get(102), False, beat_items=list(items),
                    beat_keys=list(keys), asking=asking_)


def direct_turn(channel, to_guid):
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel=channel, to_guid=to_guid,
                addressee=HUMAN, trigger_line=1)
    turn.deadline = 1e12
    return turn


_said = [0]


def first_listed(model_name, messages):
    """A beat that wants the first want the ask lists and brings in the first item offered;
    lines answer plainly."""
    last = messages[-1]["content"]
    if beats.MARK not in last:
        _said[0] += 1
        return f"sounds good, plan number {_said[0]}"
    want = last.split("One of:\n- ", 1)[1].split(":", 1)[0]
    return json.dumps({"read": "he is asking about tonight", "lands": "warm", "want": want,
                       "use": 1 if "\n1. " in last else None})


def offered(call) -> list[str]:
    text = last_user(call)
    if text.endswith(beats.USE_NONE) and "\n1. " not in text:
        return []
    listed = text.split("last line, else null:\n", 1)[1]
    return [row.split(". ", 1)[1] for row in listed.split("\n")]


# -- 1. what the beat brings in -----------------------------------------------------------------

def test_the_shuffle_is_the_turns_own_seeded_draw(rig):
    one = beats.shuffled(ITEMS, seeded("beat_items", "2026-10-29", 102, 7, 3))
    assert one == beats.shuffled(ITEMS, seeded("beat_items", "2026-10-29", 102, 7, 3))
    assert sorted(one) == sorted(ITEMS)
    turn, now = direct_turn("whisper", HUMAN), rig.clock.now()
    meta = TurnMeta("t1.5")
    order = beats.shuffled(ITEMS, rig.engine._beat_rng(turn, meta, now))
    assert order == beats.shuffled(ITEMS, rig.engine._beat_rng(turn, meta, now))
    firsts = {beats.shuffled(ITEMS, rig.engine._beat_rng(turn, TurnMeta(f"t1.{n}"), now))[0]
              for n in range(30)}
    assert firsts == set(ITEMS)  # position carries no bias: each item comes first on some turn


def test_the_ledger_keeps_ids_for_a_day_and_her_last_ten_turns():
    now = 100_000.0
    old = [{"at": now - beats.LEDGER_TTL - 1, "use": "m1"}]
    fresh = [{"at": now - 60 * i, "use": None} for i in range(12, 0, -1)]
    kept = beats.ledger(json.dumps(old + fresh), now)
    assert len(kept) == beats.LEDGER_TURNS and all(e["use"] is None for e in kept)
    assert beats.ledger("not json", now) == [] and beats.ledger(None, now) == []
    stored = json.loads(beats.recorded(kept, now, "c13"))
    assert stored[-1] == {"at": now, "use": "c13"} and len(stored) == beats.LEDGER_TURNS
    assert all(set(e) == {"at", "use"} for e in stored)


def test_offerable_keeps_out_what_was_brought_in_and_rests_two_turns():
    items = list(zip(KEYS, ITEMS))
    assert beats.offerable(items, []) == items
    used = [{"at": 1.0, "use": "m12"}]
    assert beats.offerable(items, used) == []  # her last turn brought something in
    assert beats.offerable(items, [*used, {"at": 2.0, "use": None}]) == []
    later = beats.offerable(items, [*used, {"at": 2.0, "use": None}, {"at": 3.0, "use": None}])
    assert later == [("m11", ITEMS[0]), ("c13", ITEMS[2])]
    assert beats.offerable([("", "no id")], [{"at": 1.0, "use": None}] * 2) == [("", "no id")]


@pytest.mark.parametrize("channel,to_guid", CHANNELS)
def test_one_bring_in_per_three_turns_and_never_the_same_item_twice(rig, channel, to_guid):
    """Wherever a beat runs (his whispers and DMs today; Task 8 adds the game channels), with a
    model that always brings in the first item it is offered."""
    rig.llm.set_script(first_listed)
    turn, brought = direct_turn(channel, to_guid), []
    for n in range(4):
        meta = TurnMeta(rig.engine._new_id(), answers_human=True)
        lines = rig.sync(rig.engine._beat(turn, meta, keyed(rig)))
        brought.append(lines.split("Bring in: ", 1)[1] if "Bring in: " in lines else None)
        rig.engine._record_beat(turn, meta, rig.clock.now())
    calls = beat_calls(rig)
    assert len(offered(calls[0])) == 3 and brought[0] in ITEMS
    assert offered(calls[1]) == offered(calls[2]) == [] and brought[1:3] == [None, None]
    for call in calls[1:3]:  # nothing offered: `use` can only be null
        assert call["format"]["properties"]["use"]["type"] == "null"
    assert brought[0] not in offered(calls[3]) and len(offered(calls[3])) == 2
    assert brought[3] in ITEMS and brought[3] != brought[0]
    stored = rig.store.get_meta(beats.ledger_key(102, channel, to_guid))
    assert [e["use"] for e in json.loads(stored)] == [
        KEYS[ITEMS.index(brought[0])], None, None, KEYS[ITEMS.index(brought[3])]]
    assert not any(item in stored for item in ITEMS)  # ids only


def test_a_conversations_ledger_is_its_own(rig):
    rig.llm.set_script(first_listed)
    whisper, dm = direct_turn("whisper", HUMAN), direct_turn("dm:102", HUMAN)
    meta = TurnMeta(rig.engine._new_id(), answers_human=True)
    rig.sync(rig.engine._beat(whisper, meta, keyed(rig)))
    rig.engine._record_beat(whisper, meta, rig.clock.now())
    meta = TurnMeta(rig.engine._new_id(), answers_human=True)
    assert "Bring in: " in rig.sync(rig.engine._beat(dm, meta, keyed(rig)))
    assert beats.ledger_key(102, "whisper", HUMAN) != beats.ledger_key(102, "dm:102", HUMAN)
    assert beats.ledger_key(102, "whisper", HUMAN) != beats.ledger_key(102, "whisper", 103)


def test_a_day_old_ledger_rests_nothing(rig):
    rig.llm.set_script(first_listed)
    turn = direct_turn("whisper", HUMAN)
    key = beats.ledger_key(102, "whisper", HUMAN)
    rig.store.set_meta(key, json.dumps([{"at": rig.clock.now() - beats.LEDGER_TTL - 5,
                                         "use": "m11"}]))
    meta = TurnMeta(rig.engine._new_id(), answers_human=True)
    rig.sync(rig.engine._beat(turn, meta, keyed(rig)))
    assert sorted(offered(beat_calls(rig)[0])) == sorted(ITEMS)


def test_her_day_old_ledgers_go_when_she_is_next_recorded(rig):
    old = json.dumps([{"at": rig.clock.now() - beats.LEDGER_TTL - 5, "use": "m11"}])
    fresh = json.dumps([{"at": rig.clock.now() - 60, "use": None}])
    rig.store.set_meta(beats.ledger_key(102, "guild", 0), old)
    rig.store.set_meta(beats.ledger_key(102, "dm:102", HUMAN), fresh)
    rig.store.set_meta(beats.ledger_key(103, "guild", 0), old)  # another member's: hers to keep
    turn = direct_turn("whisper", HUMAN)
    meta = TurnMeta("t9", beat_conversation=beats.ledger_key(102, "whisper", HUMAN))
    rig.engine._record_beat(turn, meta, rig.clock.now())
    assert rig.store.get_meta(beats.ledger_key(102, "guild", 0)) is None
    assert rig.store.get_meta(beats.ledger_key(102, "dm:102", HUMAN)) == fresh
    assert rig.store.get_meta(beats.ledger_key(103, "guild", 0)) == old
    # a turn whose beat was never asked records nothing
    rig.engine._record_beat(turn, TurnMeta("t10"), rig.clock.now())
    assert len(json.loads(rig.store.get_meta(beats.ledger_key(102, "whisper", HUMAN)))) == 1


@pytest.mark.parametrize("his", ["hey you", "long shift, dead on my feet"])
def test_a_said_whisper_answer_is_recorded_in_its_ledger(rig, his):
    rig.llm.set_script(first_listed)
    rig.whisper_from_him(102, his)
    rig.run()
    entries = json.loads(rig.store.get_meta(beats.ledger_key(102, "whisper", HUMAN)))
    assert len(entries) == 1 and entries[0]["use"] is None  # nothing before his line to bring in


@pytest.mark.parametrize("channel,to_guid", CHANNELS)
def test_use_is_null_unless_it_bears_on_his_last_line(rig, channel, to_guid):
    rig.llm.set_script(lambda model, messages: json.dumps(
        {"read": "he says hi", "lands": "warm", "want": "share", "use": None})
        if beats.MARK in messages[-1]["content"] else "hi")
    meta = TurnMeta(rig.engine._new_id(), answers_human=True)
    lines = rig.sync(rig.engine._beat(direct_turn(channel, to_guid), meta, keyed(rig)))
    assert lines and "Bring in" not in lines and meta.beat_use is None
    (call,) = beat_calls(rig)
    assert "only if it bears directly on Zan's last line, else null:" in last_user(call)
    assert call["format"]["properties"]["use"]["description"] == (
        "The number of one offered memory or line, only if it bears directly on Zan's last "
        "line; otherwise null, as for most answers.")


# -- 3. the repetition hint and his question ----------------------------------------------------

def test_topic_words_are_content_words_only():
    for word in ("actually", "three", "honestly", "literally", "something", "line", "still",
                 "really", "want", "this", "they", "telling", "thinking", "saying", "wanted",
                 "getting", "making"):
        assert not moves.topic_word(word), word
    for word in ("glider", "lantern", "fishing", "kayaking", "festival", "cooking", "gliders"):
        assert moves.topic_word(word), word


def test_the_hint_never_names_a_stopword_as_a_topic():
    row = lambda topics: MoveRow(ts=100.0, channel="whisper", to_guid=HUMAN,  # noqa: E731
                                       moves=frozenset(), main="", topics=topics, clause="",
                                       angle="")
    forms = ("he", "him", "his")
    assert moves.moves_line([row(("actually", "three")), row(("actually", "three"))],
                            night_start=0.0, forms=forms) == ""
    assert "brought up the lantern twice" in moves.moves_line(
        [row(("actually", "lantern")), row(("lantern",))], night_start=0.0, forms=forms)


def test_question_first_replaces_do_something_else():
    hint = "Tonight you've already called him a pet name twice: do something else this time."
    assert moves.question_first(hint, "his") == (
        f"{QUESTION_FIRST}; tonight you've already called him a pet name twice.")
    assert moves.question_first("", "her") == "Answer her question first, plainly, then whatever else."


@pytest.mark.parametrize("line,question", [
    ("are you coming to the raid tonight?", True), ("what time do we pull", True),
    ("so do you want to queue", True), ("lol where did you go :)", True),
    ("where did you go? :)", True), ("is it raid night", True), ("have fun out there", False),
    ("do it", False), ("long shift today", False), ("", False)])
def test_his_question_is_read_from_his_words(line, question):
    assert asking.is_question(line) is question


@pytest.mark.parametrize("line,asks_her", [
    ("tell me about your day", True), ("send me that pic", True), ("pick one for me pls", True),
    ("long shift today", False), ("can you help me with gems", False)])
def test_his_request_is_read_from_his_words(line, asks_her):
    assert asking.is_request(line) is asks_her
    assert asking.asks(line) is (asks_her or asking.is_question(line))


def _seed_hint(rig, channel, to_guid):
    now = rig.clock.now()
    for ago in (300, 200):  # two of her lines to him: a pet name and a joke, twice
        rig.engine.moves.record(guid=102, counterpart=HUMAN, channel=channel, to_guid=to_guid,
                                text="hey babe lol", ts=now - ago)


@pytest.mark.parametrize("channel,to_guid", CHANNELS)
def test_question_with_a_hint_answers_first_and_keeps_what_she_did(plain, channel, to_guid):
    _seed_hint(plain, channel, to_guid)
    plain.script("yes, eight sharp")
    speak(plain, *his_turn(plain, channel, to_guid, "are you coming to the raid tonight?"))
    text = plain.last_user()
    assert f"{QUESTION_FIRST}; " in text and "called him a pet name twice" in text
    assert "do something else this time" not in text


@pytest.mark.parametrize("channel,to_guid", CHANNELS)
def test_question_without_a_hint_answers_first(plain, channel, to_guid):
    plain.script("yes, eight sharp")
    speak(plain, *his_turn(plain, channel, to_guid, "what time is the pull tonight"))
    text = plain.last_user()
    assert f"{QUESTION_FIRST}." in text and "you've already" not in text


@pytest.mark.parametrize("channel,to_guid", CHANNELS)
def test_a_statement_keeps_the_hint_as_it_was(plain, channel, to_guid):
    _seed_hint(plain, channel, to_guid)
    plain.script("ha, sounds about right")
    speak(plain, *his_turn(plain, channel, to_guid, "long shift today, dead on my feet"))
    text = plain.last_user()
    assert "do something else this time." in text and QUESTION_FIRST not in text


def test_a_second_answer_to_his_guild_question_adds_what_the_first_did_not(plain):
    plain.script("eight sharp")
    turn, meta = his_turn(plain, "guild", 0, "what time is the pull tonight?")
    with plain.store.transaction():
        plain.store.conn.execute(
            "INSERT INTO chat_line(ts, channel, from_guid, from_name, to_guid, text, origin) "
            "VALUES (?, 'guild', 103, 'Coros', 0, 'eight, same as always', 'mind')",
            (plain.clock.now(),))
    plain.world.add_line(plain.store.chat_lines([turn.trigger_line + 1])[0], plain.clock.now())
    plain.engine._answer_lines[turn.trigger_line] = {turn.trigger_line + 1}  # sent as an answer
    speak(plain, turn, meta)
    assert "Coros already answered him" in plain.last_user()
    assert QUESTION_FIRST not in plain.last_user()


# -- 4. his questions lead the answering wants --------------------------------------------------

def test_the_answering_wants_lead_when_he_asks():
    asked = beats.wants_for(True, asking=True)
    assert asked[:5] == ("react", "logistics", "boundary", "tease", "share")
    assert set(asked) == set(beats.wants_for(True))
    assert beats.wants_for(True, tail=True, asking=True)[:3] == ("react", "tease", "share")
    flirt = beats.schema(asked, "Zan", "his", asking=True)["properties"]["want"]["description"]
    assert "flirt: you want to flirt with Zan, for when Zan is flirting with you, not for " \
           "answering a question." in flirt
    assert "for when Zan is flirting" not in beats.schema(beats.wants_for(True), "Zan", "his")[
        "properties"]["want"]["description"]


@pytest.mark.parametrize("family", ["game", "whisper", "dm"])
def test_every_turn_to_him_leads_with_the_answering_wants(family):
    ctx = I.IntentContext(kind=TurnKind.REPLY_HUMAN, to_him=True, owed=True,
                          his_line="when are we raiding tonight?", family=family, female=True,
                          stance="lust", style="bold", attachment="secure", humour="dry",
                          agreeableness=6, extraversion=6, tail=False, attention=None,
                          state=None, event=DayEvent.NONE, block=None, kids=False, last=None)
    wants = I.allowed(ctx)
    lead = [w for w in I.ASKING_FIRST if w in wants]
    assert lead and wants[:len(lead)] == tuple(lead) and wants[0] is not I.Intent.FLIRT
    told = I.allowed(replace(ctx, his_line="long shift today"))
    assert told == tuple(i for i in I.Intent if i in told)  # enum order when he asks nothing
    member = I.allowed(replace(ctx, to_him=False, owed=False))  # a member's question: as before
    assert member == tuple(i for i in I.Intent if i in member)


@pytest.mark.parametrize("channel", ["whisper", "dm:102"])
def test_his_logistics_question_draws_no_flirt(rig, channel):
    """A model that takes the first want it is offered: on his question that is no longer flirt
    or vent (the enum's first), on his statement it is as before."""
    rig.llm.set_script(first_listed)
    speak(rig, *his_turn(rig, channel, HUMAN, "when are we raiding tonight?"))
    asked = beat_calls(rig)[-1]
    assert asked["format"]["properties"]["want"]["enum"][0] == "react"
    assert "for when Zan is flirting with you" in last_user(asked)
    assert "\nYou want to: just react" in last_user(line_calls(rig)[0])
    calls = len(rig.chat_calls())
    speak(rig, *his_turn(rig, channel, HUMAN, "long shift today, dead on my feet"))
    told = rig.chat_calls()[calls:]
    assert told[0]["format"]["properties"]["want"]["enum"][0] == "vent"
    assert "\nYou want to: get something off your chest" in last_user(told[1])


def test_a_guild_beat_follows_the_same_order(rig):
    rig.llm.set_script(first_listed)
    meta = TurnMeta(rig.engine._new_id(), answers_human=True)
    rig.sync(rig.engine._beat(direct_turn("guild", 0), meta, keyed(rig, asking_=True)))
    (call,) = beat_calls(rig)
    assert call["format"]["properties"]["want"]["enum"][0] == "react"


@pytest.mark.parametrize("forms,word", [(("he", "him", "his"), "he"),
                                        (("she", "her", "her"), "she")])
def test_the_dm_length_asks_for_what_the_moment_needs(forms, word):
    text = render_instruction(TurnKind.REPLY_HUMAN, addressee="Zan", forms=forms,
                              channel="dm:102", discord_words=200)
    assert DISCORD_DM.format(words=200, they=word) in text
    assert f"a quick line when {word} asked something quick" in text
    assert "write as fully as you feel" not in text


# -- 5. stock epithets --------------------------------------------------------------------------

STOCK_NOW = 2_000_000_000.0


def _put(store, text, channel, to):
    with store.transaction():
        store.conn.execute(
            "INSERT INTO chat_line(ts, channel, from_guid, from_name, to_guid, text, origin) "
            "VALUES (?,?,?,?,?,?,?)", (STOCK_NOW - 3600, channel, 102, "Belra", to, text, "mind"))


@pytest.mark.parametrize("channel,to,mine", [
    ("guild", 0, dict(channel="guild")), ("whisper", HUMAN, dict(channel="whisper", to_guid=HUMAN)),
    ("dm:102", HUMAN, dict(channel="dm", room="102", to_guid=HUMAN))])
def test_stock_epithets_are_rationed_on_every_channel(store, channel, to, mine):
    names = {name for name, _ in phrases.stock()}
    assert {"you absolute tease", "youre actually mental", "proper mental", "youre proper"} <= names
    _put(store, "omg you absolute legend", channel, to)
    found = phrases.check(Reader(store.conn), Draft(102, now=STOCK_NOW, text=(
        "ok you absolute weapon, eight it is"), **mine), STOCK_NOW)
    assert "stock_member" in found.codes and "you absolute weapon" in found.phrases
    _put(store, "you're actually mental for that", channel, to)
    found = phrases.check(Reader(store.conn), Draft(102, now=STOCK_NOW, text=(
        "honestly you're actually mental"), **mine), STOCK_NOW)
    assert "stock_member" in found.codes


def test_proper_mental_counts_only_where_a_clause_opens_with_it(store):
    _put(store, "proper mental, that pull", "guild", 0)
    opens = Draft(102, "guild", "proper mental run tonight", now=STOCK_NOW)
    assert "stock_member" in phrases.check(Reader(store.conn), opens, STOCK_NOW).codes
    health = Draft(102, "guild", "taking a proper mental health day", now=STOCK_NOW)
    assert "stock_member" not in phrases.check(Reader(store.conn), health, STOCK_NOW).codes


def test_no_rule_reads_a_members_name_or_a_medium():
    """The ledger is keyed by the turn's member and conversation only: any member reads alike."""
    assert beats.ledger_key(104, "dm:104", HUMAN) == "beat:used:104:dm:104"
    assert beats.ledger_key(104, "guild", 0) == "beat:used:104:guild"
    assert beats.ledger_key(104, "whisper", HUMAN) == f"beat:used:104:whisper:{HUMAN}"
