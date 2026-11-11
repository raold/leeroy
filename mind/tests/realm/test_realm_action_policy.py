"""Exact native wire bounds and policy coverage; no registration side effects."""
from dataclasses import replace
import json

import pytest

from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_policy import classify, decode, document

TOKEN = "a" * 32 + "-" + "b" * 16
KILL_ID = "0123456789abcdef0123456789abcdef:1"


def announce(**changes):
    values = dict(id="announce", kill_id=KILL_ID, native_epoch=7, guild_id=50, human_guid=1,
                  map=0, instance_id=1, boss_id=0, difficulty=0, size=10, heroic=False,
                  boss_name="Example", first="week")
    return p.Announce(**{**values, **changes})


CASES = [
    (p.RealmVisit("visit", "org_inn"), "ack", ()),
    (p.RealmActors("actors", (3, 2), (2,), (50,)), "ack", ()),
    (p.RealmSay("say", 2, "trade", "hello"), "ack", ()),
    (p.RealmEmote("emote", 2, "wave"), "ack", ()),
    (p.RealmMove("move", 2, "home"), "ordinary", (2,)),
    (p.RpgStatus("rpg", 2, "do quest", 1), "ordinary", (2,)),
    (p.LeavePvp("leave", (3, 2)), "ordinary", (2, 3)),
    (p.Rearm("rearm", (3, 2)), "ordinary", (2, 3)),
    (p.WgJoin("wg", (3, 2)), "wg", (2, 3)),
    (p.ArenaTeam("team", "create", 2, 2, "Example"), "ordinary", (2,)),
    (p.Duel("duel", 3, 2), "ordinary", (2, 3)),
    (p.Craft("craft", 2, 100, 5), "ordinary", (2,)),
    (p.ItemCount("count", 2, (100, 200)), "result", ()),
    (p.Equip("equip", 2, 5976), "ordinary", (2,)),
    (p.SetProfession("profession", 2, 171), "ordinary", (2,)),
    (p.TransmogSet("transmog", 2, 0, 0), "ordinary", (2,)),
    (p.CalendarPost("post", 2, "Title", "raid", 1), "result", (2,)),
    (p.CalendarSignup("signup", 2**64-1, 2, "accepted"), "ordinary", (2,)),
    (p.CalendarUpdate("update", 2**64-1), "ack", ()),
    (p.MailSend("mail", 2, "Subject", items=(p.MailItem(100, 2, "bags"),)), "ordinary", (2,)),
    (p.BankTabBuy("tab", 2, 0, "One", ""), "ack", (2,)),
    (p.BankMoney("money", 1), "ack", ()),
    (p.BankDeposit("deposit", 2, 100, 0, 0), "ordinary", (2,)),
    (p.BankWithdraw("withdraw", 2, 100, 1, 0), "ordinary", (2,)),
    (p.BankStock("stock", 2, 0, 100, 1), "ordinary", (2,)),
    (p.GrantAchievement("achievement", 2, 3117), "ack", (2,)),
    (p.GuildRename("rename", 50, "Example"), "ack", ()),
    (p.GuildGrow("grow", 50, (3, 2)), "ordinary", (2, 3)),
    (p.GuildAdmit("admit", 2), "ack", (2,)),
    (p.GuildReadmit("readmit", 2, 1700000000000, 4), "ack", (2,)),
    (p.GuildNote("note", 2, ""), "ack", (2,)),
    (announce(), "ack", ()),
    (p.GetRealmPvp("query", origin_id="wg"), "query", ()),
    (BotDo("defaults", (3, 2), "nc", "-bg,-lfg"), "defaults", (2, 3)),
]


@pytest.mark.parametrize("cmd,policy,scope", CASES, ids=lambda v: str(getattr(v, "id", v)))
def test_exact_registry_scope_and_lossless_payload_roundtrip(cmd, policy, scope):
    c = classify(cmd, child=True)
    assert c.policy == policy
    assert c.reserved_manifest == scope
    restored, again = decode(c.payload)
    assert restored == cmd
    assert again == c
    if hasattr(cmd, "guids"):
        assert json.loads(c.payload)["guids"] == list(cmd.guids)


def test_registry_accounts_for_all_realm_types():
    assert {type(c) for c, _, _ in CASES if type(c) is not BotDo} | {p.ArenaQueue} == set(p.COMMANDS.values())


@pytest.mark.parametrize("cmd", [
    p.ArenaQueue("arena", 1, 2), p.ArenaTeam("add", "add", team_id=1, guid=2),
    p.ArenaTeam("drop", "drop", team_id=1, guid=2),
    BotDo("bot", (2,), "cast", "100"), BotDo("bot", (2,), "nc", "-bg"),
    BotDo("bot", (2,), "nc", "-bg,-lfg", 2), BotDo("bot", (2,), "nc", "-bg,-lfg", training_run="run"),
    p.LeavePvp("cleanup", (2,), TOKEN), p.GetRealmPvp("query", origin_id="wg"),
])
def test_unsupported_shapes_and_unattached_children_reject(cmd):
    with pytest.raises(ValueError): classify(cmd)


@pytest.mark.parametrize("cmd", [
    p.RealmMove("bad id", 2, "home"), p.RealmMove("bad", True, "home"),
    p.RealmMove("bad", 0, "home"), p.RealmMove("bad", 2**32, "home"),
    p.RealmMove("bad", 2, "Home"), p.RealmMove("bad", 2, "home", 120),
    p.Rearm("bad", (2, 2)), p.Rearm("bad", (True,)), p.Rearm("bad", ()),
    p.WgJoin("bad", tuple(range(1, 42))), p.Duel("bad", 2, 2),
    p.Craft("bad", 2, 100, 6), p.Equip("bad", 2, 100), p.SetProfession("bad", 2, 1),
    p.TransmogSet("bad", 2, 19, 0), p.ItemCount("bad", 2, (100, 100)),
    p.RealmSay("bad", 2, "guild", "hello"), p.RealmSay("bad", 2, "say", "x", 3),
    p.RealmSay("bad", 2, "whisper", "x", 0), p.RealmEmote("bad", 2, "a\0b"),
    p.RealmActors("bad", (2,), (3,)), p.RpgStatus("bad", 2, "idle", 3),
    p.CalendarPost("bad", 2, "T", "raid", 1, guild_event=1),
    p.CalendarUpdate("bad", True), p.MailSend("bad", 2, "S", money=1000001),
    p.MailSend("bad", 2, "S", items=(p.MailItem(1, 21, "new"),)),
    p.BankTabBuy("bad", 2, 6, "T", ""), p.BankMoney("bad", 60001),
    p.BankWithdraw("bad", 2, 100, 0, 0), p.GuildAdmit("bad", 2, 1),
    p.GuildNote("bad", 2, "a" * 32), p.GrantAchievement("bad", 2, 1),
    p.GuildRename("bad", 50, "é" * 13),
])
def test_native_field_bounds_reject_before_journaling(cmd):
    with pytest.raises(ValueError): classify(cmd)


def test_exact_type_no_subclass_authority():
    class Move(p.RealmMove): pass
    with pytest.raises(ValueError): classify(Move("move", 2, "home"))


def test_payload_copies_mutable_input_preserving_order():
    source = [3, 2]
    result = classify(p.Rearm("rearm", source))
    source.clear()
    assert result.reserved_manifest == (2, 3)
    assert json.loads(result.payload)["guids"] == [3, 2]


@pytest.mark.parametrize("field,value", [
    ("id", "bad id"), ("kill_id", ""), ("kill_id", "a" * 32 + ":0"),
    ("kill_id", "a" * 32 + ":01"), ("kill_id", "A" * 32 + ":1"),
    ("kill_id", "a" * 32 + ":18446744073709551616"), ("kill_id", True),
    ("native_epoch", 0), ("native_epoch", True), ("native_epoch", 2**64),
    ("guild_id", 0), ("guild_id", 2**32), ("human_guid", False), ("human_guid", "1"),
    ("map", -1), ("map", 2**32), ("instance_id", 0), ("instance_id", True),
    ("boss_id", -1), ("boss_id", 2**32), ("difficulty", -2), ("difficulty", 4),
    ("difficulty", True), ("difficulty", 0.0), ("size", 25), ("size", "10"),
    ("heroic", True), ("heroic", 0), ("boss_name", ""), ("boss_name", "é" * 25),
    ("boss_name", "bad\0name"), ("boss_name", "\ud800"),
    ("attempts", -1), ("attempts", 100), ("attempts", True),
    ("fight_s", -1), ("fight_s", 3601), ("fight_s", 1.0), ("first", "month"),
])
def test_exact_announce_constructor_and_mutated_dataclass_reject(field, value):
    with pytest.raises(ValueError):
        announce(**{field: value})
    cmd = announce()
    object.__setattr__(cmd, field, value)
    with pytest.raises(ValueError):
        classify(cmd)


@pytest.mark.parametrize("difficulty,size,heroic", [
    (0, 10, False), (1, 25, False), (2, 10, True), (3, 25, True),
    (-1, 10, True), (-1, 25, False),
])
def test_exact_announce_modes_and_upper_identity_bounds_roundtrip(difficulty, size, heroic):
    cmd = announce(kill_id="f" * 32 + ":18446744073709551615", native_epoch=2**64 - 1,
                   guild_id=2**32 - 1, human_guid=2**32 - 1, map=2**32 - 1,
                   instance_id=2**32 - 1, boss_id=2**32 - 1, difficulty=difficulty, size=size,
                   heroic=heroic, boss_name="é" * 24, attempts=99, fight_s=3600, first="ever")
    result = classify(cmd)
    assert result.policy == "ack" and result.reserved_manifest == ()
    assert decode(result.payload) == (cmd, result)


@pytest.mark.parametrize("field", [
    "kill_id", "native_epoch", "guild_id", "human_guid", "difficulty", "size", "heroic",
    "attempts", "fight_s", "first",
])
def test_announce_decode_requires_every_serialized_proof_and_metric_field(field):
    raw = json.loads(classify(announce()).payload)
    del raw[field]
    with pytest.raises((TypeError, ValueError)):
        decode(document(raw))


def test_announce_old_shape_unknown_fields_and_duplicate_keys_are_not_live_payloads():
    payload = classify(announce()).payload
    raw = json.loads(payload)
    old = {key: value for key, value in raw.items() if key not in {
        "kill_id", "native_epoch", "guild_id", "human_guid", "difficulty", "size", "heroic"}}
    with pytest.raises(TypeError):
        decode(document(old))
    with pytest.raises(TypeError):
        decode(document({**raw, "native_proof": KILL_ID}))
    with pytest.raises(ValueError):
        decode(payload[:-1] + ',"kill_id":"' + KILL_ID + '"}')
    with pytest.raises(ValueError):
        decode(payload + " ")


def test_announce_metrics_default_locally_but_are_always_serialized():
    result = classify(announce())
    raw = json.loads(result.payload)
    assert raw["attempts"] == raw["fight_s"] == 0
    assert set(raw) == {"type", *p.Announce.__dataclass_fields__}
