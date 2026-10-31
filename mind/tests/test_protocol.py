import json
from dataclasses import dataclass

import pytest

from leeroy_mind.protocol import (
    CHAT_CHANNELS, MAX_LINE_BYTES, PROTOCOL_VERSION, SAY_CHANNELS, Ack, Afk, AfkEvent, AddFriend,
    Boss, Cast, Chat, Death, Emote, GetCast, HumanSay, LootDrop, LootItem, Ping, ProtocolError,
    Say, SetMotd, SetRank, UnknownType, VersionError, encode_command, parse_event,
    register_command, register_event, sanitize_say_text,
)

CHAT = ('{"v":1,"type":"chat","ts":5,"channel":"guild","from_guid":7,"from_name":"Arkon",'
        '"to_guid":0,"text":"hi","map":1,"zone":1637,"extra":true}')

MEMBER = {"guid": 7, "name": "Arkon", "race": 2, "class": 1, "gender": 0, "level": 80, "rank": 1,
          "online": True, "map": 1, "zone": 1637, "spec_tab": 2}
OFFLINE_MEMBER = {**MEMBER, "guid": 8, "name": "Belra", "online": False, "map": -1, "spec_tab": -1}

# One sample per core event type, shaped like the live C++ encoders (LeeroyProtocol.cpp,
# LeeroyCastModel.cpp): hello.version is a string, boss_name is "", offline members use -1,
# chat and text_emote carry via_bridge, and error carries an id only when one was readable.
ALL_EVENT_SAMPLES = [
    {"v": 1, "type": "hello", "ts": 1, "server": "mod-leeroy", "version": "0.1.0",
     "guild_id": 3, "human_guid": 0},
    {"v": 1, "type": "cast", "ts": 1, "members": [MEMBER, OFFLINE_MEMBER]},
    {"v": 1, "type": "login", "ts": 1, "guid": 7, "human": False},
    {"v": 1, "type": "logout", "ts": 1, "guid": 7, "human": True},
    {"v": 1, "type": "chat", "ts": 1, "channel": "guild", "from_guid": 7, "from_name": "Arkon",
     "to_guid": 0, "text": "hi", "map": 1, "zone": 1637, "via_bridge": False},
    {"v": 1, "type": "text_emote", "ts": 1, "guid": 7, "emote": "hug", "target_guid": 0,
     "via_bridge": True},
    {"v": 1, "type": "afk", "ts": 1, "guid": 7, "on": True},
    {"v": 1, "type": "boss", "ts": 1, "map": 631, "instance_id": 4, "boss_id": 2, "boss_name": "",
     "state": "engaged", "members": [7, 8, 101]},
    {"v": 1, "type": "death", "ts": 1, "guid": 7, "map": 631, "killer_entry": 36612},
    {"v": 1, "type": "resurrect", "ts": 1, "guid": 7},
    {"v": 1, "type": "loot_drop", "ts": 1, "map": 631, "instance_id": 4, "source_entry": 36612,
     "items": [{"item_id": 50000, "name": "Coros Blade", "quality": 4, "index": 0}]},
    {"v": 1, "type": "loot_won", "ts": 1, "guid": 7, "item_id": 50000, "name": "Coros Blade",
     "method": "need"},
    {"v": 1, "type": "achievement", "ts": 1, "guid": 7, "achievement_id": 4530, "name": "Dunna"},
    {"v": 1, "type": "bg_end", "ts": 1, "bg_type": 2, "winner": "horde", "members": [7, 8]},
    {"v": 1, "type": "zone", "ts": 1, "guid": 7, "zone_id": 4395, "zone_name": "Ebbo Flats"},
    {"v": 1, "type": "group", "ts": 1, "leader_guid": 0, "raid": False, "members": []},
    {"v": 1, "type": "dc_status", "ts": 1, "tank_guid": 7, "enabled": True, "paused": False,
     "payload": "{}"},
    {"v": 1, "type": "ack", "ts": 1, "id": "m1", "ok": False, "error": "bad field: guid"},
    {"v": 1, "type": "pong", "ts": 1, "id": "m2"},
    {"v": 1, "type": "error", "ts": 1, "message": "bad json"},
]
EXPECTED_CLASS = {
    "hello": "Hello", "cast": "Cast", "login": "Login", "logout": "Logout", "chat": "Chat",
    "text_emote": "TextEmote", "afk": "AfkEvent", "boss": "Boss", "death": "Death",
    "resurrect": "Resurrect", "loot_drop": "LootDrop", "loot_won": "LootWon",
    "achievement": "Achievement", "bg_end": "BgEnd", "zone": "Zone", "group": "Group",
    "dc_status": "DcStatus", "ack": "Ack", "pong": "Pong", "error": "Error",
}


def test_constants():
    assert PROTOCOL_VERSION == 1 and MAX_LINE_BYTES == 65536
    assert SAY_CHANNELS == {"guild", "officer", "party", "raid", "say", "yell", "whisper"}
    assert CHAT_CHANNELS == SAY_CHANNELS | {"raid_warning", "emote"}


def test_parse_chat_ignores_unknown_fields():
    assert parse_event(CHAT) == Chat(ts=5, channel="guild", from_guid=7, from_name="Arkon",
                                     to_guid=0, text="hi", map=1, zone=1637)


def test_parse_cast_maps_class_key():
    ev = parse_event('{"v":1,"type":"cast","ts":1,"members":[{"guid":7,"name":"Arkon","race":2,'
                     '"class":1,"gender":0,"level":80,"rank":1,"online":true,"map":1,"zone":1637,'
                     '"spec_tab":2}]}')
    assert ev.members[0].class_ == 1 and ev.members[0].spec_tab == 2


@pytest.mark.parametrize("line,exc,match", [
    ('{"v":2,"type":"pong","ts":1,"id":"m1"}', VersionError, "v"),
    ('{"v":1,"type":"bogus","ts":1}', UnknownType, "bogus"),
    ('{"v":1,"type":"chat","ts":1,"channel":"guild"}', ProtocolError, "from_guid"),
    ('{"v":1,"type":"login","ts":1,"guid":"x","human":false}', ProtocolError, "guid"),
    ("not json", ProtocolError, "json"), ("[1]", ProtocolError, "object")])
def test_bad_lines(line, exc, match):
    with pytest.raises(exc, match=match):
        parse_event(line)


def test_line_too_long():
    with pytest.raises(ProtocolError):
        parse_event(b"x" * 65537)


def test_every_roadmap_event_parses():
    assert len(ALL_EVENT_SAMPLES) == 20
    for obj in ALL_EVENT_SAMPLES:
        assert type(parse_event(json.dumps(obj))).__name__ == EXPECTED_CLASS[obj["type"]]


def test_as_built_shapes():
    hello = parse_event(json.dumps(ALL_EVENT_SAMPLES[0]))
    assert hello.version == "0.1.0" and hello.human_guid == 0
    cast = parse_event(json.dumps(ALL_EVENT_SAMPLES[1]))
    assert cast.members[1].online is False and cast.members[1].map == -1
    assert cast.members[1].spec_tab == -1 and isinstance(cast.members, tuple)
    boss = parse_event(json.dumps(ALL_EVENT_SAMPLES[7]))
    assert boss == Boss(ts=1, map=631, instance_id=4, boss_id=2, boss_name="", state="engaged",
                        members=(7, 8, 101))
    drop = parse_event(json.dumps(ALL_EVENT_SAMPLES[10]))
    assert drop.items == (LootItem(item_id=50000, name="Coros Blade", quality=4, index=0),)
    assert isinstance(drop, LootDrop)
    assert parse_event(json.dumps(ALL_EVENT_SAMPLES[15])).members == ()
    assert parse_event(json.dumps(ALL_EVENT_SAMPLES[5])).via_bridge is True


def test_death_has_no_instance_id():
    assert not hasattr(parse_event(json.dumps(ALL_EVENT_SAMPLES[8])), "instance_id")
    assert isinstance(parse_event(json.dumps(ALL_EVENT_SAMPLES[8])), Death)


def test_cast_and_error_without_id_parse():
    assert isinstance(parse_event(json.dumps(ALL_EVENT_SAMPLES[1])), Cast)
    assert parse_event(json.dumps(ALL_EVENT_SAMPLES[19])).id == ""


def test_new_core_fields():
    assert parse_event(CHAT.replace('"extra":true', '"via_bridge":true')).via_bridge is True
    assert parse_event('{"v":1,"type":"afk","ts":1,"guid":1,"on":true}') == AfkEvent(
        ts=1, guid=1, on=True)
    assert parse_event('{"v":1,"type":"text_emote","ts":1,"guid":1,"emote":"hug",'
                       '"target_guid":101,"via_bridge":false}').target_guid == 101
    assert parse_event('{"v":1,"type":"error","ts":1,"message":"unknown type: x","id":"m4"}').id == "m4"
    assert parse_event('{"v":1,"type":"error","ts":1,"message":"bad json"}').id == ""


def test_ack_and_error_before_hello_parse_normally():
    assert parse_event('{"v":1,"type":"ack","ts":1,"id":"m1","ok":true,"error":""}') == Ack(
        ts=1, id="m1", ok=True, error="")


@pytest.mark.parametrize("line", [
    '{"v":1,"type":"login","ts":1,"guid":true,"human":false}',          # bool for int
    '{"v":1,"type":"login","ts":1,"guid":1.5,"human":false}',
    '{"v":1,"type":"login","ts":1,"guid":1,"human":1}',                  # int for bool
    '{"v":1,"type":"hello","ts":1,"server":"s","version":1,"guild_id":1,"human_guid":0}',
    '{"v":1,"type":"cast","ts":1,"members":{}}',
    '{"v":1,"type":"cast","ts":1,"members":[1]}',
    '{"v":1,"type":"cast","ts":1,"members":[{"guid":1}]}',
    '{"v":1,"type":"group","ts":1,"leader_guid":1,"raid":false,"members":["a"]}',
    '{"v":1,"type":"chat","ts":1,"channel":"guild","from_guid":1,"from_name":"A","to_guid":0,'
    '"text":"x","map":1,"zone":1,"via_bridge":null}',
    '{"v":true,"type":"pong","ts":1,"id":"m1"}',
    '{"type":"pong","ts":1,"id":"m1"}',
    '{"v":1,"type":5,"ts":1}',
    '{"v":1,"type":"pong","id":"m1"}',
    '{"v":1,"type":"pong","ts":"1","id":"m1"}',
    '{"v":1,"type":"pong","ts":NaN,"id":"m1"}',
    "", "   ", "null", '"str"', "{", "[" * 100000,
    b"\xff\xfe{\x00",
])
def test_garbage_never_raises_anything_but_protocol_error(line):
    with pytest.raises(ProtocolError):
        parse_event(line)


def test_trailing_newline_and_bytes_accepted():
    assert parse_event(b'{"v":1,"type":"pong","ts":1,"id":"m1"}\n').id == "m1"


def test_invalid_utf8_in_string_is_replaced_not_fatal():
    ev = parse_event(b'{"v":1,"type":"error","ts":1,"message":"bad \xff byte"}')
    assert "bad" in ev.message


def test_encode_say_sanitizes():
    obj = json.loads(encode_command(Say(id="m3", guid=7, channel="guild", text="hey\nthere|r"),
                                    ts_ms=9))
    assert obj == {"v": 1, "type": "say", "ts": 9, "id": "m3", "guid": 7, "channel": "guild",
                   "text": "hey therer", "to_guid": 0}


def test_encode_line_shape_and_every_core_command():
    line = encode_command(Ping(id="m1"), 5)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    cmds = [
        (Ping(id="a"), {"type": "ping", "id": "a"}),
        (GetCast(id="a"), {"type": "get_cast", "id": "a"}),
        (Emote(id="a", guid=7, emote="cheer"),
         {"type": "emote", "id": "a", "guid": 7, "emote": "cheer", "target_guid": 0}),
        (HumanSay(id="a", channel="whisper", text="hi", to_name="Testmage"),
         {"type": "human_say", "id": "a", "channel": "whisper", "text": "hi", "to_name": "Testmage"}),
        (SetRank(id="a", guid=7, rank=3), {"type": "set_rank", "id": "a", "guid": 7, "rank": 3}),
        (SetMotd(id="a", text="a|b\nc"), {"type": "set_motd", "id": "a", "text": "ab c"}),
        (AddFriend(id="a", guid=7, note="n"), {"type": "add_friend", "id": "a", "guid": 7, "note": "n"}),
        (Afk(id="a", guid=7, on=True), {"type": "afk", "id": "a", "guid": 7, "on": True, "message": ""}),
    ]
    for cmd, expected in cmds:
        assert json.loads(encode_command(cmd, 4)) == {"v": 1, "ts": 4, **expected}


def test_encode_non_ascii_round_trips():
    obj = json.loads(encode_command(Say(id="m1", guid=7, channel="guild", text="café ☃"), 1))
    assert obj["text"] == "café ☃"


def test_sanitize_clamps_utf8_without_splitting():
    s = sanitize_say_text("é" * 200)
    assert s == "é" * 127 and len(s.encode()) == 254


@pytest.mark.parametrize("ch,size", [("a", 1), ("é", 2), ("☃", 3), ("\U0001f600", 4)])
def test_sanitize_never_splits_any_width(ch, size):
    s = sanitize_say_text(ch * 300)
    assert len(s.encode()) <= 255 and set(s) == {ch}
    assert len(s.encode()) > 255 - size


def test_sanitize_strips_controls_and_collapses():
    assert sanitize_say_text("a\x00b\x07c\x85d\x9fe") == "abcde"
    assert sanitize_say_text("  a \t\r\n  b   c ") == "a b c"
    assert sanitize_say_text("x" * 254 + " y") == "x" * 254
    assert sanitize_say_text("lone \ud800 surrogate") == "lone surrogate"


def test_bad_commands_refused():
    with pytest.raises(ValueError):
        encode_command(Say(id="m1", guid=7, channel="raid_warning", text="x"), 1)
    for t in (".gm on", "!x"):
        with pytest.raises(ValueError):
            encode_command(HumanSay(id="m1", channel="guild", text=t), 1)
    with pytest.raises(ValueError):
        encode_command(HumanSay(id="m1", channel="emote", text="x"), 1)
    with pytest.raises(ValueError):
        encode_command(Ack(ts=1, id="m1", ok=True, error=""), 1)


def test_human_say_prefix_checked_after_sanitizing():
    with pytest.raises(ValueError):
        encode_command(HumanSay(id="m1", channel="guild", text="  \n.gm on"), 1)
    with pytest.raises(ValueError):
        encode_command(HumanSay(id="m1", channel="guild", text="|.gm on"), 1)


def test_register_command():          # the registry is process-wide: never use a name P1d registers
    @dataclass(frozen=True)
    class TestExt:
        id: str
        size: int

    register_command("test_ext", TestExt)
    register_command("test_ext", TestExt)      # same class again: no-op
    assert json.loads(encode_command(TestExt(id="m9", size=25), 1))["type"] == "test_ext"
    with pytest.raises(ValueError):
        register_command("test_ext", Ack)
    with pytest.raises(ValueError):
        register_command("say", TestExt)
    with pytest.raises(ValueError):
        register_command("test_ext_notdc", int)


def test_register_event():
    @dataclass(frozen=True)
    class TestJob:
        ts: int
        job: str

    register_event("test_job", TestJob)
    register_event("test_job", TestJob)
    assert parse_event('{"v":1,"type":"test_job","ts":3,"job":"x","extra":1}') == TestJob(ts=3, job="x")
    with pytest.raises(ValueError):
        register_event("test_job", Ack)
    with pytest.raises(ValueError):
        register_event("chat", TestJob)
    with pytest.raises(ProtocolError, match="job"):
        parse_event('{"v":1,"type":"test_job","ts":3}')
