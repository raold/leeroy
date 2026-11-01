import json
from pathlib import Path

import pytest

from leeroy_voice import wire
from leeroy_voice.wire import (ANOTHER_MIND, DROP_REASONS, FROM_MIND, FROM_VOICE,
                               MAX_LINE_BYTES, MESSAGE_TYPES, PROTOCOL_VERSION, TIERS, Cancel,
                               Channels, Error, Heard, Hello, Invite, Join, Played, Ptt, Room,
                               Speak, Status, WireError, as_fields, encode, parse, type_name)

GOLDEN = Path(__file__).parent / "fixtures" / "wire_golden.json"


def golden() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_golden_lines():
    for case in golden():
        msg = parse(case["line"].encode())
        assert type_name(msg) == case["type"] and as_fields(msg) == case["fields"]
        assert encode(msg, case["ts"]) == case["line"].encode() + b"\n"


def test_golden_covers_every_type():
    assert {c["type"] for c in golden()} == set(MESSAGE_TYPES)
    assert len(golden()) == len(MESSAGE_TYPES) == 16  # M1: one case per type


def test_the_protocol_table():
    assert PROTOCOL_VERSION == 1 and MAX_LINE_BYTES == 65536
    assert FROM_MIND == {"session", "channels", "moved", "invite", "refused", "voice", "speak",
                         "cancel", "raid"}
    assert FROM_VOICE == {"hello", "join", "played", "ptt", "heard", "status", "error"}
    assert set(MESSAGE_TYPES) == FROM_MIND | FROM_VOICE and not FROM_MIND & FROM_VOICE
    assert TIERS == ("p0", "p1", "p2", "p3", "p4") and ANOTHER_MIND == "another mind is connected"
    assert DROP_REASONS == ("expired", "moved", "not_his_channel", "no_voice", "muted", "empty",
                            "raid", "after_dropped", "offline", "cancelled", "engine_error",
                            "mind_gone")
    assert type_name(wire.VoiceDef(101, "Arkon", {})) == "voice"  # the class is not "Voice"


def padded_join(size: int) -> bytes:
    head, tail = b'{"v":1,"type":"join","ts":1,"channel":"', b'"}'
    line = head + b"a" * (size - len(head) - len(tail)) + tail
    assert len(line) == size
    return line


def reply_of(line: bytes) -> str:
    with pytest.raises(WireError) as e:
        parse(line)
    return e.value.reply


def test_rejects():
    assert reply_of(b'[{"v":1,"type":"join","ts":1,"channel":"raid"}]') == "bad json"
    assert reply_of(b'{"v":2,"type":"join","ts":1,"channel":"raid"}') == "unsupported version"
    assert reply_of(b'{"v":1,"type":"nope","ts":1}') == "unknown type: nope"
    assert reply_of(padded_join(65537)) == "line too long"
    assert reply_of(b'{"v":1,"type":"join","ts":1}') == "bad field: channel"
    assert reply_of(b"GET / HTTP/1.1\r\n") == "bad json"
    assert reply_of(b"\xff\xfe{}") == "bad json"
    assert reply_of(b'{"v":1,"type":"join","channel":"raid"}') == "bad field: ts"
    assert reply_of(b'{"type":"join","ts":1,"channel":"raid"}') == "unsupported version"
    assert reply_of(b'{"v":true,"type":"join","ts":1,"channel":"raid"}') == "unsupported version"
    assert reply_of(b'{"v":1,"type":7,"ts":1}') == "unknown type: "
    assert reply_of(b'{"v":1,"type":"heard","ts":1,"text":"a","raw":"a","confidence":NaN,'
                    b'"channel":"raid","speech_ms":1,"latency_ms":1}') == "bad json"
    assert reply_of(b"[" * 100000) == "line too long"
    assert reply_of(b"[" * 60000) == "bad json"  # nested past the recursion limit


def test_line_limit_is_64_kib():
    assert parse(padded_join(MAX_LINE_BYTES)).channel.startswith("aaa")
    assert parse(padded_join(MAX_LINE_BYTES) + b"\r\n") == parse(padded_join(MAX_LINE_BYTES))
    assert reply_of(padded_join(MAX_LINE_BYTES + 1)) == "line too long"


def test_unknown_fields_ignored():
    assert parse(b'{"v":1,"type":"join","ts":1,"channel":"raid","extra":3}') == Join("raid")
    room = parse(b'{"v":1,"type":"channels","ts":1,"channels":[{"id":"raid","name":"Raid",'
                 b'"parent":"","locked":false,"allowed":[],"capacity":0,"colour":"red"}]}')
    assert room == Channels((Room("raid", "Raid", "", False, (), 0),))


HEARD = (b'{"v":1,"type":"heard","ts":1,"text":"a","raw":"a","confidence":%s,"channel":"raid",'
         b'"speech_ms":1,"latency_ms":1}')


def test_types_are_checked():
    assert reply_of(HEARD % b'"high"') == "bad field: confidence"
    assert reply_of(b'{"v":1,"type":"ptt","ts":1,"down":"yes","channel":"raid"}') == (
        "bad field: down")
    heard = parse(HEARD % b"1")
    assert heard.confidence == 1.0 and type(heard.confidence) is float
    assert reply_of(HEARD % b"true") == "bad field: confidence"  # booleans are never numbers
    assert reply_of(b'{"v":1,"type":"ptt","ts":1,"down":1,"channel":"raid"}') == "bad field: down"
    assert reply_of(b'{"v":1,"type":"moved","ts":1,"guid":true,"channel":"raid","by":0}') == (
        "bad field: guid")
    assert reply_of(b'{"v":1,"type":"moved","ts":1,"guid":1.0,"channel":"raid","by":0}') == (
        "bad field: guid")
    assert reply_of(b'{"v":1,"type":"channels","ts":1,"channels":[{"id":"raid","name":"Raid",'
                    b'"parent":"","locked":false,"allowed":["1"],"capacity":0}]}') == (
        "bad field: channels[0].allowed[0]")
    assert reply_of(b'{"v":1,"type":"voice","ts":1,"guid":101,"name":"Arkon","spec":"x"}') == (
        "bad field: spec")
    assert reply_of(b'{"v":1,"type":"status","ts":1,"ok":true,"mic":"ok","gpu":"none",'
                    b'"stt":"off","ui":true,"queue":0,"errors":"none"}') == "bad field: errors"
    assert reply_of(b'{"v":1,"type":"join","ts":"1","channel":"raid"}') == "bad field: ts"


def speak_line(**over) -> bytes:
    fields = {"id": "s1", "guid": 101, "text": "pulling", "channel": "raid", "to_human": False,
              "priority": "p2", "kind": "callout", "attitude": "none", "state": "sober",
              "expires_ms": 5000, "after": ""}
    fields.update(over)
    return json.dumps({"v": 1, "type": "speak", "ts": 1, **fields}).encode()


def test_protocol_values_are_checked():
    assert reply_of(speak_line(priority="p5")) == "bad field: priority"
    assert reply_of(speak_line(attitude="angry")) == "bad field: attitude"
    assert reply_of(speak_line(state="sleepy")) == "bad field: state"
    assert parse(speak_line(state="hungover", attitude="lust", priority="p0")).priority == "p0"
    assert reply_of(b'{"v":1,"type":"raid","ts":1,"phase":"boss","lead":0}') == "bad field: phase"
    assert reply_of(b'{"v":1,"type":"refused","ts":1,"channel":"x","reason":"busy"}') == (
        "bad field: reason")
    assert reply_of(b'{"v":1,"type":"played","ts":1,"id":"s1","state":"paused","reason":""}') == (
        "bad field: state")
    assert reply_of(b'{"v":1,"type":"played","ts":1,"id":"s1","state":"dropped","reason":""}') == (
        "bad field: reason")
    assert reply_of(b'{"v":1,"type":"played","ts":1,"id":"s1","state":"done",'
                    b'"reason":"moved"}') == (
        "bad field: reason")
    assert parse(b'{"v":1,"type":"played","ts":1,"id":"s1","state":"cut","reason":""}') == (
        Played("s1", "cut"))
    assert reply_of(b'{"v":1,"type":"status","ts":1,"ok":true,"mic":"loud","gpu":"none",'
                    b'"stt":"off","ui":true,"queue":0,"errors":[]}') == "bad field: mic"
    assert reply_of(b'{"v":1,"type":"status","ts":1,"ok":true,"mic":"ok","gpu":"none",'
                    b'"stt":"vosk","ui":true,"queue":0,"errors":[]}') == "bad field: stt"
    assert reply_of(b'{"v":1,"type":"channels","ts":1,"channels":[{"id":"raid","name":"Raid",'
                    b'"parent":"","locked":false,"allowed":[],"capacity":-1}]}') == (
        "bad field: channels[0].capacity")


def test_speak_template():
    """R1: speak's last field marks a turn composed without the model; a speak without it is
    read as false, as from a mind that predates it."""
    for flag, tail in ((False, b',"after":"","template":false}\n'),
                       (True, b',"after":"","template":true}\n')):
        msg = Speak("s1", 101, "gz!", "guild_hall", False, "p4", "gz", "none", "sober", 9000,
                    template=flag)
        line = encode(msg, 1)
        assert line.endswith(tail) and parse(line) == msg
    assert parse(speak_line()).template is False
    assert parse(speak_line(template=True)).template is True
    assert reply_of(speak_line(template=1)) == "bad field: template"


def test_cancel_names_exactly_one():
    assert parse(b'{"v":1,"type":"cancel","ts":1,"guid":101}') == Cancel(guid=101)
    assert parse(b'{"v":1,"type":"cancel","ts":1,"id":"s1","guid":0,"below":""}') == Cancel(id="s1")
    assert parse(b'{"v":1,"type":"cancel","ts":1,"below":"p3"}') == Cancel(below="p3")
    for line in (b'{"v":1,"type":"cancel","ts":1,"id":"s1","guid":101}',
                 b'{"v":1,"type":"cancel","ts":1,"id":"","guid":0,"below":""}'):
        assert reply_of(line) == "bad field: id/guid/below"
    assert reply_of(b'{"v":1,"type":"cancel","ts":1,"below":"p9"}') == "bad field: below"


def test_encode():
    line = encode(Heard("a", "a", 1, "raid", 1, 1), 5)  # a float field given an int
    assert line == (b'{"v":1,"type":"heard","ts":5,"text":"a","raw":"a","confidence":1.0,'
                    b'"channel":"raid","speech_ms":1,"latency_ms":1}\n')
    assert encode(Invite(102, "private1"), 1) == (
        b'{"v":1,"type":"invite","ts":1,"from":102,"channel":"private1"}\n')
    assert encode(Error(ANOTHER_MIND), 1).endswith(b'"message":"another mind is connected"}\n')
    for bad in (Played("s1", "paused"), Ptt("yes", "raid"), Cancel(),
                Status(True, "loud", "none", "off", True, 0, ()),
                wire.VoiceDef(101, "Arkon", {"speed": float("nan")})):
        with pytest.raises(WireError):
            encode(bad, 1)
    with pytest.raises(WireError):
        encode(object(), 1)
    with pytest.raises(WireError):
        encode(Join("raid"), True)
    long = Speak("s1", 101, "x" * MAX_LINE_BYTES, "raid", False, "p4", "banter", "none", "sober", 1)
    with pytest.raises(WireError) as e:
        encode(long, 1)
    assert e.value.reply == "line too long"


def test_defaults_and_from():
    assert Played("s1", "started") == Played("s1", "started", "")
    assert Speak("s1", 101, "hi", "raid", False, "p4", "banter", "none", "sober", 9).after == ""
    assert as_fields(Invite(102, "private1")) == {"from": 102, "channel": "private1"}
    assert parse(b'{"v":1,"type":"invite","ts":1,"from":102,"channel":"private1"}').from_ == 102
    hello = Hello("leeroy-voice", "0.1.0", ("kokoro",), "Mic A", "", "\\")
    assert as_fields(hello)["engines"] == ["kokoro"]  # tuples are JSON arrays
    with pytest.raises(WireError):
        as_fields(object())
