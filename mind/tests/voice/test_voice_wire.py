import json
from pathlib import Path

import pytest

from leeroy_mind.voice import wire
from leeroy_mind.voice.wire import (ANOTHER_MIND, FROM_MIND, FROM_VOICE, MESSAGE_TYPES, Speak,
                                    WireError, as_fields, encode, parse, type_name)

GOLDEN = Path(__file__).resolve().parents[3] / "voice" / "tests" / "fixtures" / "wire_golden.json"


def golden() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_golden_lines():
    for case in golden():
        msg = parse(case["line"].encode())
        assert type_name(msg) == case["type"] and as_fields(msg) == case["fields"]
        assert encode(msg, case["ts"]) == case["line"].encode() + b"\n"


def test_golden_covers_every_type():
    assert {c["type"] for c in golden()} == set(MESSAGE_TYPES)
    assert len(MESSAGE_TYPES) == 16 and len(FROM_MIND) == 9 and len(FROM_VOICE) == 7
    assert ANOTHER_MIND == "another mind is connected"


def test_speak_template_defaults_false():
    s = Speak("s1", 101, "hi", "guild_hall", False, "p4", "banter", "none", "sober", 1, "")
    assert s.template is False and as_fields(s)["template"] is False
    assert type_name(wire.VoiceDef(101, "Arkon", {})) == "voice"


def test_rejects_bad_word():
    with pytest.raises(WireError):
        parse(b'{"v":1,"type":"raid","ts":1,"phase":"nope","lead":0}')
