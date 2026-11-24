"""Her flirts at him on the game's public channels ([talk] wow_topic, talk/wowtopic.py; the
re-measure of 2026-11-24): while he raids a flirt goes by whisper or not at all, never on a
public channel; elsewhere a public flirt is rare, one line, generated short and never cut down;
no romance on a public channel while he raids and no wording past the flirty band otherwise.
Invented cast and lines only."""
import logging
from dataclasses import replace

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import OVERLAY, engine_clock, make_rig, save_member
from leeroy_mind.protocol import Chat, Group
from leeroy_mind.talk import engine as engine_mod
from leeroy_mind.talk import wowtopic as w
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.romance import BUILTIN_ROMANCE
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.fixture
def clock():
    return engine_clock()


def rig(store, clock, tmp_path, *, raid=False, wow_topic=True, **kw):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="lust", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    save_member(store, 104, player_gender="female", stance="lust", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path, wow_topic=wow_topic, **kw)
    r.engine.bits.pick = lambda *a, **k: None  # her social turn, never a bit
    if raid:
        r.sync(r.event(Group(ts=0, leader_guid=HUMAN, raid=True,
                             members=(HUMAN, 101, 102, 103, 104))))
    r.world.human_last_active = clock.now()  # he is playing: flirts are on
    return r


@pytest.fixture
def lands(monkeypatch):
    """Her flirt chance always lands, and so does the public share."""
    monkeypatch.setattr(engine_mod, "flirt_chance", lambda p, candour: 1.0)
    monkeypatch.setattr(w, "PUBLIC_FLIRT_SHARE", 1.0)


def social(r, guid=102, channel="guild", kind=TurnKind.OPEN):
    return r.engine._social_turn(kind, guid, channel, addressee=None, trigger=None, room=2,
                                 now=r.clock.now(), delay=0.0)


def queued(r, monkeypatch):
    """Every (turn, meta) the engine queues from here on."""
    seen = []
    monkeypatch.setattr(r.engine, "_queue", lambda items: seen.extend(items))
    return seen


# -- while he raids ------------------------------------------------------------------------------

@pytest.mark.parametrize("channel", ["raid", "guild"])
def test_a_flirt_while_he_raids_goes_by_whisper(store, clock, tmp_path, lands, monkeypatch,
                                                caplog, channel):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    r = rig(store, clock, tmp_path, raid=True)
    try:
        seen = queued(r, monkeypatch)
        turn, _ = social(r, channel=channel)
        assert turn.kind is TurnKind.OPEN and turn.channel == channel  # never a public flirt
        (whisper, meta), = seen
        assert (whisper.kind, whisper.channel, whisper.to_guid) == (TurnKind.WHISPER, "whisper",
                                                                    HUMAN)
        assert meta.why == "whisper:raid_flirt" and meta.initiates and not meta.casual
        assert w.RAID_FLIRT_WHY.format(name=r.engine.human.called, they="he") in whisper.instruction
        assert "raid_flirt: guid=102 sent=whisper" in caplog.text
        # her next flirt this soon: the whisper planner's own pacing says no, and nothing goes
        turn, _ = social(r, channel=channel)
        assert turn.kind is TurnKind.OPEN and len(seen) == 1
        assert "raid_flirt: guid=102 sent=none reason=" in caplog.text
    finally:
        r.close()


def test_a_flirt_in_a_raid_instance_without_a_raid_group(store, clock, tmp_path, lands,
                                                          monkeypatch):
    r = rig(store, clock, tmp_path)
    try:
        r.world.apply(Chat(ts=0, channel="say", from_guid=HUMAN, from_name="Testmage", to_guid=0,
                           text="anyone here", map=631, zone=4812), clock.now())
        assert r.world.human_raiding()
        seen = queued(r, monkeypatch)
        turn, _ = social(r)
        assert turn.kind is TurnKind.OPEN and [t.channel for t, _ in seen] == ["whisper"]
    finally:
        r.close()


def test_a_held_whisper_clock_means_no_flirt_at_all(store, clock, tmp_path, lands, monkeypatch):
    r = rig(store, clock, tmp_path, raid=True)
    try:
        r.world.human_afk = True  # nothing starts a conversation with him now
        r.world.human_last_active = clock.now()
        seen = queued(r, monkeypatch)
        turn, _ = social(r, channel="raid")
        assert turn.kind is TurnKind.OPEN and seen == []
    finally:
        r.close()


def test_a_rival_never_cuts_in_while_he_raids(store, clock, tmp_path, monkeypatch):
    r = rig(store, clock, tmp_path, raid=True)
    try:
        monkeypatch.setattr("leeroy_mind.talk.reactions.rivalry_reply", lambda *a: 104)
        seen = queued(r, monkeypatch)
        flirt = Turn(kind=TurnKind.FLIRT, speaker=102, channel="raid", addressee=HUMAN)
        r.engine._rival(flirt, r.engine.personas.get(102), clock.now())
        assert seen == []
        r.sync(r.event(Group(ts=0, leader_guid=0, raid=False, members=())))  # the raid is over
        r.engine._rival(replace(flirt, channel="guild"), r.engine.personas.get(102), clock.now())
        (rival, meta), = seen
        assert rival.kind is TurnKind.FLIRT and rival.max_lines == 1 and meta.rivalry
        assert rival.instruction.endswith(w.public_flirt_line("guild"))
    finally:
        r.close()


# -- elsewhere: rare and one line -------------------------------------------------------------------

def test_a_public_flirt_is_rare_and_one_light_line(store, clock, tmp_path, lands):
    r = rig(store, clock, tmp_path)
    try:
        turn, _ = social(r)
        assert (turn.kind, turn.channel, turn.addressee, turn.max_lines) == (
            TurnKind.FLIRT, "guild", HUMAN, 1)
        assert turn.instruction == w.public_flirt_line("guild")
        again, _ = social(r, guid=104)  # one every PUBLIC_FLIRT_GAP_S, guild-wide
        assert again.kind is TurnKind.OPEN
        r.run(w.PUBLIC_FLIRT_GAP_S + 1, step=60.0)
        r.world.human_last_active = r.clock.now()
        later, _ = social(r, guid=104)
        assert later.kind is TurnKind.FLIRT
    finally:
        r.close()


def test_the_public_share_keeps_most_of_her_flirt_chances_as_they_were(store, clock, tmp_path,
                                                                       monkeypatch):
    monkeypatch.setattr(engine_mod, "flirt_chance", lambda p, candour: 1.0)
    monkeypatch.setattr(w, "PUBLIC_FLIRT_SHARE", 0.0)
    r = rig(store, clock, tmp_path)
    try:
        assert all(social(r)[0].kind is not TurnKind.FLIRT for _ in range(20))
    finally:
        r.close()


def test_wow_topic_off_keeps_her_flirts_as_they_were(store, clock, tmp_path, lands):
    r = rig(store, clock, tmp_path, raid=True, wow_topic=False)
    try:
        turn, _ = social(r, channel="raid")
        assert (turn.kind, turn.channel, turn.max_lines, turn.instruction) == (
            TurnKind.FLIRT, "raid", 2, "")
    finally:
        r.close()


# -- her romance on a public channel ----------------------------------------------------------------

def prompt(r, turn, meta=None):
    c = r.sync(r.engine._compose(turn, meta or TurnMeta("t"), live=False))
    return "\n\n".join(m["content"] for m in c.messages)


def flirt(channel="guild", guid=102):
    return Turn(kind=TurnKind.FLIRT, speaker=guid, channel=channel, addressee=HUMAN, max_lines=1)


def test_a_public_flirt_reads_no_further_than_the_flirty_band(store, clock, tmp_path):
    r = rig(store, clock, tmp_path, dial=10)
    try:
        text = prompt(r, flirt())
        assert BUILTIN_ROMANCE["flirty"] in text and OVERLAY not in text
        whisper = prompt(r, Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper",
                                 to_guid=HUMAN, addressee=HUMAN))
        assert OVERLAY in whisper  # a whisper keeps her band
    finally:
        r.close()
    r = rig(store, clock, tmp_path, dial=10, wow_topic=False)
    try:
        assert OVERLAY in prompt(r, flirt())  # the switch off: as it was
    finally:
        r.close()


def test_no_romance_on_a_public_channel_while_he_raids(store, clock, tmp_path):
    r = rig(store, clock, tmp_path, raid=True, dial=10)
    try:
        meta = TurnMeta("gz", loot_cheer=True)
        cheer = Turn(kind=TurnKind.GZ, speaker=102, channel="raid", addressee=HUMAN)
        text = prompt(r, cheer, meta)
        assert OVERLAY not in text and BUILTIN_ROMANCE["flirty"] not in text
        assert "INVARIANTS" not in text and "only ever with Testmage" not in text
    finally:
        r.close()


def test_his_raid_zone_counts_as_raiding(store, clock, tmp_path):
    from leeroy_mind.protocol import Zone
    r = rig(store, clock, tmp_path)
    try:
        r.world.apply(Zone(ts=0, guid=HUMAN, zone_id=4812, zone_name="Icecrown Citadel"),
                      clock.now())
        assert r.world.human_raiding()
        r.world.apply(Zone(ts=0, guid=HUMAN, zone_id=4395, zone_name="Dalaran"), clock.now())
        assert not r.world.human_raiding()
    finally:
        r.close()


# -- generated short, never cut down ----------------------------------------------------------------

LONG = ("you know what, every time you pull like that i keep thinking about you and how good "
        "you look in that new tier set tonight")


def public(store, clock, tmp_path, *replies, **kw):
    r = rig(store, clock, tmp_path, **kw)
    save_member(store, 102, player_gender="female", stance="lust", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={}, words_per_line=[3, 14])
    r.script(*replies)
    turn = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN, max_lines=1,
                instruction=w.public_flirt_line("guild"))
    return r, turn


def test_a_public_flirt_is_generated_short_and_never_cut(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    r, turn = public(store, clock, tmp_path, LONG, "nice pull, show off")
    try:
        assert r.speak_turn(turn) == ["nice pull, show off"]
        first, second = r.chat_calls()
        assert first["options"]["num_predict"] == w.predict(w.PUBLIC_FLIRT_WORDS)
        assert w.public_flirt_line("guild") in first["messages"][-1]["content"]
        assert "Shorter: 3-10 words" in second["messages"][-1]["content"]
        assert "wow_length_trim" not in caplog.text  # never cut down to a fragment
    finally:
        r.close()


def test_a_public_flirt_still_too_long_is_dropped(store, clock, tmp_path):
    r, turn = public(store, clock, tmp_path, LONG)
    try:
        assert r.speak_turn(turn) == []
        assert len(r.chat_calls()) == 2
        assert [u["dropped"] for u in r.utterances()] == ["tells"]
    finally:
        r.close()


def test_a_public_flirt_the_num_predict_cut_off_is_asked_again(store, clock, tmp_path):
    from leeroy_mind.llm.diagnostics import capture_response
    r, turn = public(store, clock, tmp_path, "you look good tonight. and later i", "hi you")
    original = r.llm.chat
    calls = []

    async def chat(model, messages, *, options, think=None, **kw):
        result = await original(model, messages, options=options, think=think, **kw)
        calls.append(result.text)
        done = "length" if len(calls) == 1 else "stop"
        return replace(result, diagnostics=capture_response(
            {"message": {"content": result.text}, "done": True, "done_reason": done}))
    r.llm.chat = chat
    try:
        assert r.speak_turn(turn) == ["hi you"]
    finally:
        r.close()


def test_a_whisper_flirt_keeps_its_length(store, clock, tmp_path):
    r, _ = public(store, clock, tmp_path, LONG)
    try:
        whisper = Turn(kind=TurnKind.FLIRT, speaker=102, channel="whisper", to_guid=HUMAN,
                       addressee=HUMAN)
        r.speak_turn(whisper)
        assert r.chat_calls()[0]["options"]["num_predict"] != w.predict(w.PUBLIC_FLIRT_WORDS)
    finally:
        r.close()


# -- the hard screen on sexual wording (talk/publicscreen.py) ---------------------------------------

SEXUAL = "so horny watching you tank tonight"


def opener(guid=102, channel="guild"):
    return Turn(kind=TurnKind.OPEN, speaker=guid, channel=channel)


@pytest.mark.parametrize("wow_topic", [True, False])
def test_a_sexual_draft_on_a_public_channel_is_asked_again_clean(store, clock, tmp_path,
                                                                 wow_topic):
    from leeroy_mind.talk import publicscreen
    r = rig(store, clock, tmp_path, wow_topic=wow_topic)
    try:
        r.script(SEXUAL, "who's up for heroics later")
        assert r.speak_turn(opener()) == ["who's up for heroics later"]
        assert publicscreen.HINT in r.chat_calls()[-1]["messages"][-1]["content"]
    finally:
        r.close()


def test_a_sexual_draft_never_goes_out_on_a_public_channel(store, clock, tmp_path):
    r = rig(store, clock, tmp_path)
    try:
        r.script(SEXUAL)
        assert r.speak_turn(opener()) == []
        assert r.sent.says() == [] and [u["dropped"] for u in r.utterances()] == ["tells"]
    finally:
        r.close()


def test_a_sexual_draft_in_a_whisper_is_hers(store, clock, tmp_path):
    r = rig(store, clock, tmp_path)
    try:
        r.script(SEXUAL)
        whisper = Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper", to_guid=HUMAN,
                       addressee=HUMAN)
        assert r.speak_turn(whisper) == [SEXUAL]
    finally:
        r.close()


def test_his_answer_sexual_every_time_goes_to_him_by_whisper(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    r = rig(store, clock, tmp_path, raid=True)
    try:
        r.script(SEXUAL)
        r.line_from_him("belra you around?", channel="raid")  # owed: the engine answers
        r.run(120)
        (say,) = r.sent.says()
        assert (say.guid, say.channel, say.to_guid, say.text) == (102, "whisper", HUMAN, SEXUAL)
        assert "public_sexual_whispered: guid=102" in caplog.text
        assert not [s for s in r.sent.says() if s.channel == "raid"]
    finally:
        r.close()


def test_the_last_check_before_the_bridge(store, clock, tmp_path):
    r = rig(store, clock, tmp_path)
    try:
        p = r.engine.personas.get(102)
        meta = TurnMeta("n")

        async def send(turn, text):
            return await r.engine._send_line(turn, meta, p, text, [], first=True)
        assert r.sync(send(opener(), SEXUAL)) == "public_sexual"
        assert r.sent.says() == []
        whisper = Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper", to_guid=HUMAN,
                       addressee=HUMAN)
        assert r.sync(send(whisper, SEXUAL)) is None
    finally:
        r.close()


def test_the_planner_off_takes_no_whisper_of_hers():
    import random
    from leeroy_mind.talk.romance import WhisperPlanner
    off = WhisperPlanner(random.Random(1), per_hour=0, events=False)
    assert not off.take(1000.0, 102)  # [talk] whispers_per_hour 0: no unprompted whisper at all
    on = WhisperPlanner(random.Random(1), per_hour=4)
    assert on.take(1000.0, 102) and not on.take(1001.0, 102)  # then her own gap
    assert not on.take(1010.0, 104)  # two whispers never 2 minutes apart
    assert on.take(1200.0, 104)


def test_a_public_flirt_planned_before_he_raids_never_goes_once_he_does(store, clock, tmp_path):
    r = rig(store, clock, tmp_path)
    try:
        r.script("you look good tonight")
        flirt = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN,
                     max_lines=1, instruction=w.public_flirt_line("guild"), delay=10.0)
        r.sync(r.event(Group(ts=0, leader_guid=HUMAN, raid=True,
                             members=(HUMAN, 101, 102, 103, 104))))  # he forms a raid meanwhile
        assert r.speak_turn(flirt) == [] and r.sent.says() == []
        assert [u["dropped"] for u in r.utterances()] == ["raid_flirt"]
        assert r.chat_calls() == []  # dropped before it is composed
    finally:
        r.close()
