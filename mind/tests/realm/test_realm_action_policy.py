"""Exact native wire bounds and policy coverage; no registration side effects."""
from dataclasses import replace
import json

import pytest

from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_policy import classify, decode

TOKEN = "a" * 32 + "-" + "b" * 16
CASES = [
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
    (p.GuildNote("note", 2, ""), "ack", (2,)),
    (p.Announce("announce", 0, 1, 0, "Example", "week"), "ack", ()),
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


def test_registry_accounts_for_all_32_realm_types():
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
    p.GuildRename("bad", 50, "é" * 13), p.Announce("bad", 0, 0, 0, "B", "ever"),
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
