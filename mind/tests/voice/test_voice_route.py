"""The tiers, the expiries and the speak message (route.py), and where a turn goes (Comms)."""
import pytest

from conftest import HUMAN, run
from engine_rig import engine_clock
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.voice import route, wire
from leeroy_mind.voice.comms import SPOKEN_HINT, typed
from voice.conftest import status

LEAD = 102


@pytest.fixture
def clock():
    return engine_clock()


def tier(kind, speaker=101, *, room="guild_hall", lead=LEAD, officer=False, in_raid=False,
         addressee=None, to_guid=0):
    turn = Turn(kind=kind, speaker=speaker, channel=f"voice:{room}", addressee=addressee,
                to_guid=to_guid)
    return route.tier_of(turn, room=room, lead=lead, officer=officer, in_raid=in_raid,
                         human=HUMAN)


def speak(kind, room="guild_hall", tier_="p4", **meta):
    turn = Turn(kind=kind, speaker=101, channel=f"voice:{room}")
    return route.speak_for(turn, TurnMeta("t1", **meta), "Hello there.", speak_id="r.t1",
                           room=room, tier=tier_, attitude="none", state="sober", after="",
                           now=1000.0)


def test_tiers():
    """I1."""
    assert tier(TurnKind.CALLOUT, LEAD, room="raid", in_raid=True) == "p0"
    assert tier(TurnKind.CALLOUT, LEAD, room="raid2", in_raid=True) == "p0"
    assert tier(TurnKind.URGENT) == "p1"
    assert tier(TurnKind.URGENT, LEAD, room="raid", in_raid=True) == "p1"  # the lead's too
    assert tier(TurnKind.CALLOUT, 104, room="raid", officer=True, in_raid=True) == "p2"
    assert tier(TurnKind.CALLOUT, LEAD, room="guild_hall", in_raid=True) == "p2"
    for kind in (TurnKind.REPLY_HUMAN, TurnKind.KILL, TurnKind.WIPE_REACT):
        assert tier(kind, 104, room="raid", officer=True, in_raid=True) == "p2"
        assert tier(kind, LEAD, room="raid", in_raid=True) == "p2"
    assert tier(TurnKind.REPLY_HUMAN, addressee=HUMAN) == "p3"
    assert tier(TurnKind.WHISPER, room="whisper") == "p3"
    assert tier(TurnKind.OPEN, room="private1") == "p3"
    assert tier(TurnKind.ASIDE) == "p4"
    assert tier(TurnKind.OPEN) == "p4"
    assert tier(TurnKind.CALLOUT, 101, room="raid", in_raid=True) == "p4"  # a plain raider
    assert tier(TurnKind.CALLOUT, LEAD, room="raid", lead=None, in_raid=True) == "p4"


def test_no_human_means_no_turn_is_his():
    turn = Turn(kind=TurnKind.OPEN, speaker=101, channel="voice:raid")
    assert route.tier_of(turn, room="raid", lead=None, officer=False, in_raid=False,
                         human=0) == "p4"


@pytest.mark.parametrize("kind,tier_,room,ms", [
    (TurnKind.CALLOUT, "p0", "raid", 3000), (TurnKind.URGENT, "p1", "raid", 3000),
    (TurnKind.CALLOUT, "p2", "raid", 5000), (TurnKind.REPLY_HUMAN, "p3", "guild_hall", 20000),
    (TurnKind.OPEN, "p3", "private1", 20000), (TurnKind.ASIDE, "p3", "private1", 20000),
    (TurnKind.WHISPER, "p3", "whisper", 30000),
    (TurnKind.OPEN, "p4", "guild_hall", 15000), (TurnKind.ASIDE, "p4", "guild_hall", 8000)])
def test_expiry(kind, tier_, room, ms):
    assert speak(kind, room, tier_).expires_ms == 1000000 + ms


def test_urgent_in_a_whisper_keeps_its_three_seconds():
    assert speak(TurnKind.URGENT, "whisper", "p1").expires_ms == 1000000 + 3000


def test_expiry_table():
    assert route.EXPIRY_MS == {"p0": 3000, "p1": 3000, "p2": 5000, "p3": 20000, "p4": 15000}


def test_speak_fields():
    s = speak(TurnKind.OPEN)
    assert (s.id, s.guid, s.text, s.channel, s.priority, s.kind, s.after) == (
        "r.t1", 101, "Hello there.", "guild_hall", "p4", "open", "")
    assert type(s.kind) is str and s.template is False
    wire.encode(s, 1)


def test_a_hot_mic_turn_says_so():
    assert speak(TurnKind.OPEN, why="hotmic").kind == "hotmic"


def test_a_template_turn_is_marked():
    """Revision 3.2: every turn composed without the model."""
    turn = Turn(kind=TurnKind.GZ, speaker=101, channel="voice:raid", template="gg")
    s = route.speak_for(turn, TurnMeta("t1"), "gg", speak_id="r.t1", room="raid", tier="p4",
                        attitude="none", state="sober", after="", now=1000.0)
    assert s.template is True


def test_to_human():
    for turn in (Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="voice:raid",
                      addressee=HUMAN),
                 Turn(kind=TurnKind.WHISPER, speaker=101, channel="whisper", to_guid=HUMAN)):
        s = route.speak_for(turn, TurnMeta("t1"), "x", speak_id="r.t1", room="raid", tier="p3",
                            attitude="none", state="sober", after="", now=1000.0, human=HUMAN)
        assert s.to_human is True
    s = route.speak_for(Turn(kind=TurnKind.CONTINUE, speaker=101, channel="voice:raid",
                             addressee=102), TurnMeta("t1"), "x", speak_id="r.t1", room="raid",
                        tier="p4", attitude="none", state="sober", after="", now=1000.0,
                        human=HUMAN)
    assert s.to_human is False


def test_the_spoken_hint_is_the_plans():
    assert SPOKEN_HINT == ("You're talking out loud on vent: say it the way you'd say it, with "
                           "no chat abbreviations or emoticons.")


def turn_on(channel, speaker=101, **kw):
    return Turn(kind=kw.pop("kind", TurnKind.OPEN), speaker=speaker, channel=channel, **kw)


def test_typed_channel_of_a_voice_turn():
    """N6: a turn that arrived on a voice channel has no game channel of its own."""
    meta = TurnMeta("t")
    assert typed(turn_on("voice:officers"), meta) == ("officer", 0, None)
    assert typed(turn_on("voice:guild_hall", addressee=HUMAN), meta) == ("guild", 0, HUMAN)
    assert typed(turn_on("raid"), meta) == ("raid", 0, None)  # not a voice turn: as it was
    assert typed(turn_on("whisper", to_guid=HUMAN, addressee=HUMAN), meta) == (
        "whisper", HUMAN, HUMAN)


def test_typed_channel_of_a_turn_that_came_on_a_game_channel():
    """I3: the turn's own channel, never guild."""
    for channel in ("raid", "party", "say", "raid_warning", "officer"):
        meta = TurnMeta("t", typed_on=(channel, 0))
        assert typed(turn_on("voice:raid", addressee=HUMAN), meta) == (channel, 0, HUMAN)


def test_a_whisper_never_goes_back_to_a_game_channel():
    """A private-room line came on guild and was stored as a whisper: it stays one."""
    meta = TurnMeta("t", typed_on=("guild", 0))
    assert typed(turn_on("whisper", 102, to_guid=HUMAN, addressee=HUMAN), meta) == (
        "whisper", HUMAN, HUMAN)


def test_available(vrig):
    assert vrig.comms.available()
    vrig.fake.status = None  # greeted, no status yet
    assert not vrig.comms.available()
    vrig.fake.status = status(ok=False)  # M22: the output closed under him
    assert not vrig.comms.available()
    vrig.fake.status = status()
    vrig.world.human_online = False
    assert not vrig.comms.available()
    vrig.world.human_online = True
    vrig.fake.greeted = False
    assert not vrig.comms.available()


def test_room_of_and_hearable(vrig):
    v = vrig
    v.seat(101, "guild_hall")
    v.seat(102, "raid")
    v.him("guild_hall")
    assert (v.comms.room_of(101), v.comms.room_of(103)) == ("guild_hall", None)
    assert v.comms.human_room() == "guild_hall"
    assert v.comms.hearable(101) and v.comms.hearable(103)  # his room, off comms
    assert not v.comms.hearable(102)
    v.fake.greeted = False  # voice down: everyone is as in phase 1
    assert v.comms.room_of(101) is None and v.comms.hearable(102)


def test_unheard(vrig):
    v = vrig
    v.him("guild_hall")
    plain, there = TurnMeta("t"), TurnMeta("t", elsewhere=True)
    assert v.comms.unheard("voice:raid", plain) and v.comms.unheard("voice:smoke_room", there)
    assert not v.comms.unheard("voice:guild_hall", plain)
    assert not v.comms.unheard("guild", plain) and not v.comms.unheard("whisper", plain)
    assert not v.comms.unheard("guild", there)  # a typed line is always heard
    assert v.comms.unheard("voice:guild_hall", there)  # the other rooms' talk stays text


def test_audience_is_the_rooms_cast(vrig):
    v = vrig
    v.seat(101, "guild_hall")
    v.seat(102, "guild_hall")
    v.seat(103, "raid")
    v.him("guild_hall")
    assert v.comms.audience("voice:guild_hall") == frozenset({101, 102})  # him not included
    assert v.world.audience("voice:guild_hall", from_guid=101) == frozenset({101, 102})
    v.world.online.discard(102)  # seated, but gone from the game
    assert v.world.audience("voice:guild_hall", from_guid=101) == frozenset({101})


def test_place_while_down_or_off_comms(vrig):
    v = vrig
    v.him("guild_hall")
    meta = TurnMeta("t")
    # off comms: unchanged, but a turn on voice types (N6)
    assert v.comms.place(turn_on("guild"), meta) == ("guild", 0, None)
    assert v.comms.place(turn_on("voice:guild_hall", addressee=HUMAN), meta) == (
        "guild", 0, HUMAN)
    assert v.comms.place(turn_on("voice:officers"), meta) == ("officer", 0, None)
    v.seat(101, "guild_hall")
    v.fake.greeted = False
    assert v.comms.place(turn_on("guild"), meta) == ("guild", 0, None)
    assert v.comms.place(turn_on("voice:guild_hall"), meta) == ("guild", 0, None)


def test_place_of_a_whisper_is_left_alone(vrig):
    v = vrig
    v.seat(101, "guild_hall")
    turn = turn_on("whisper", kind=TurnKind.WHISPER, to_guid=HUMAN, addressee=HUMAN)
    assert v.comms.place(turn, TurnMeta("t")) == ("whisper", HUMAN, HUMAN)
    v.seat(102, "private1")
    v.him("private1")  # a whisper to someone else is never turned into one to him
    assert v.comms.place(turn_on("whisper", 102, to_guid=104), TurnMeta("t")) == (
        "whisper", 104, None)


def test_place_game_channels_into_the_room(vrig):
    v = vrig
    v.seat(101, "raid")
    v.him("raid")
    for channel in ("guild", "party", "raid", "raid_warning", "say"):
        assert v.comms.place(turn_on(channel, addressee=HUMAN), TurnMeta("t")) == (
            "voice:raid", 0, HUMAN)


def test_place_keeps_a_game_channel_when_he_cant_hear_the_room(vrig):
    """I1, the ruling: seated in a room he is not in, a line on a game channel stays typed."""
    v = vrig
    v.seat(101, "guild_hall")
    v.him("raid")
    for channel in ("guild", "party", "raid", "raid_warning", "say"):
        assert v.comms.place(turn_on(channel, addressee=HUMAN), TurnMeta("t")) == (
            channel, 0, HUMAN)
    meta = TurnMeta("t", typed_on=("raid", 0))  # it was placed in a room, and he has left it
    assert v.comms.place(turn_on("voice:guild_hall", addressee=HUMAN), meta) == (
        "raid", 0, HUMAN)


def test_place_of_elsewhere_talk_stays_in_its_room(vrig):
    v = vrig
    v.seat(104, "smoke_room")
    v.him("guild_hall")
    assert v.comms.place(turn_on("guild", 104), TurnMeta("t", elsewhere=True)) == (
        "voice:smoke_room", 0, None)


def test_place_follows_the_room(vrig):
    """M5."""
    v = vrig
    v.him("guild_hall")
    v.seat(101, "raid")
    turn = turn_on("voice:guild_hall", kind=TurnKind.CONTINUE, addressee=102)
    assert v.comms.place(turn, TurnMeta("t")) == ("voice:raid", 0, 102)
    v.seat(104, "guild_hall")
    assert v.comms.place(turn_on("officer", 104), TurnMeta("t")) == ("officer", 0, None)
    v.seat(104, "officers")
    assert v.comms.place(turn_on("officer", 104), TurnMeta("t")) == ("officer", 0, None)
    v.him("officers")
    assert v.comms.place(turn_on("officer", 104), TurnMeta("t")) == ("voice:officers", 0, None)


def test_place_keeps_an_answer_to_a_typed_line_typed(vrig):
    v = vrig
    v.seat(101, "guild_hall")
    v.him("guild_hall")

    def his(channel):
        return v.store.insert_chat_line(
            Chat(ts=0, channel=channel, from_guid=HUMAN, from_name="Testmage", to_guid=0,
                 text="hello there", map=0, zone=10), session_id=None, origin="human")
    answer = turn_on("guild", kind=TurnKind.REPLY_HUMAN, addressee=HUMAN,
                     trigger_line=his("guild"))
    assert v.comms.place(answer, TurnMeta("t")) == ("guild", 0, HUMAN)
    # on a game channel, answering his spoken line (a goodnight's raid call), it is spoken
    answer.trigger_line = his("voice:guild_hall")
    assert v.comms.place(answer, TurnMeta("t")) == ("voice:guild_hall", 0, HUMAN)


def test_place_in_a_private_room_is_a_whisper(vrig):
    v = vrig
    v.seat(102, "private1")
    v.him("private1")
    assert v.comms.place(turn_on("guild", 102), TurnMeta("t")) == ("whisper", HUMAN, HUMAN)
    assert v.comms.place(turn_on("officer", 102), TurnMeta("t")) == ("whisper", HUMAN, HUMAN)


def test_place_keeps_a_raid_turn_on_raid_from_a_private_room(vrig):
    """The ruling: typed where the raid sees it. Ordinary talk still goes to him as a whisper."""
    v = vrig
    v.seat(102, "private1")
    v.him("private1")
    for channel in ("raid", "raid_warning"):
        assert v.comms.place(turn_on(channel, 102), TurnMeta("t")) == (channel, 0, None)
    for channel in ("guild", "party", "say", "officer"):
        assert v.comms.place(turn_on(channel, 102), TurnMeta("t")) == ("whisper", HUMAN, HUMAN)
    # placed in the raid room, and its speaker has gone private since
    came_on_raid = TurnMeta("t", typed_on=("raid", 0))
    assert v.comms.place(turn_on("voice:raid", 102), came_on_raid) == ("raid", 0, None)
    # a reply in the raid room came on no game channel: ordinary talk
    assert v.comms.place(turn_on("voice:raid", 102), TurnMeta("t")) == ("whisper", HUMAN, HUMAN)


def test_a_whisper_stays_one_whatever_it_came_on(vrig):
    v = vrig
    v.seat(102, "private1")
    v.him("private1")
    turn = turn_on("whisper", 102, to_guid=HUMAN, addressee=HUMAN)
    assert v.comms.place(turn, TurnMeta("t", typed_on=("raid", 0))) == ("whisper", HUMAN, HUMAN)


def test_a_voice_turn_never_lands_in_a_private_room_as_a_room_line(vrig):
    """Settled item 5: a burst's next speaker who has gone private with him is stored as a
    whisper between the two, never on a voice: channel that every prompt may carry."""
    v = vrig
    v.seat(102, "private1")
    v.him("private1")
    turn = turn_on("voice:guild_hall", 102, kind=TurnKind.CONTINUE, addressee=101)
    assert v.comms.place(turn, TurnMeta("t")) == ("whisper", HUMAN, HUMAN)


def test_voiced(vrig):
    v = vrig
    v.seat(102, "guild_hall")
    v.him("guild_hall")
    whisper = turn_on("whisper", 102, kind=TurnKind.WHISPER, to_guid=HUMAN, addressee=HUMAN)
    assert v.comms.voiced(turn_on("voice:raid"), TurnMeta("t"))
    assert not v.comms.voiced(turn_on("guild"), TurnMeta("t"))
    assert v.comms.voiced(whisper, TurnMeta("t"))
    assert not v.comms.voiced(whisper, TurnMeta("t", whisper_reply=True))  # his typed whisper
    assert not v.comms.voiced(turn_on("whisper", 103, to_guid=HUMAN), TurnMeta("t"))  # off comms
    assert not v.comms.voiced(turn_on("whisper", 102, to_guid=104), TurnMeta("t"))  # not to him
    v.fake.greeted = False
    assert not v.comms.voiced(whisper, TurnMeta("t"))


def test_voiced_in_a_private_room_answers_even_his_whisper(vrig):
    v = vrig
    v.seat(102, "private1")
    v.him("private1")
    whisper = turn_on("whisper", 102, kind=TurnKind.REPLY_HUMAN, to_guid=HUMAN, addressee=HUMAN)
    assert v.comms.voiced(whisper, TurnMeta("t", whisper_reply=True))


def test_voiced_invite_ask_needs_the_ui(vrig):
    v = vrig
    v.seat(102, "guild_hall")
    whisper = turn_on("whisper", 102, kind=TurnKind.WHISPER, to_guid=HUMAN, addressee=HUMAN)
    assert v.comms.voiced(whisper, TurnMeta("t", why="invite:private"))
    v.fake.status = status(ui=False)
    assert not v.comms.voiced(whisper, TurnMeta("t", why="invite:private"))
    assert v.comms.voiced(whisper, TurnMeta("t", why="whisper:idle"))


def test_tier_reads_the_raid(vrig):
    v = vrig
    v.world.group = None

    async def go():
        from engine_rig import RAID
        from leeroy_mind.protocol import Group
        await v.event(Group(ts=0, leader_guid=102, raid=True, members=RAID))
    run(go())
    callout = Turn(kind=TurnKind.CALLOUT, speaker=102, channel="voice:raid")
    assert v.comms.tier(callout, "voice:raid") == "p0"
    assert v.comms.tier(callout, "voice:guild_hall") == "p2"
    callout.speaker = 104  # an officer
    assert v.comms.tier(callout, "voice:raid") == "p2"
    callout.speaker = 101
    assert v.comms.tier(callout, "voice:raid") == "p4"
    assert v.comms.tier(Turn(kind=TurnKind.WHISPER, speaker=101, channel="whisper"),
                        "whisper") == "p3"
