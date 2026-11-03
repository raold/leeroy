// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyProtocol.h"
#include "TestHarness.h"
#include <nlohmann/json.hpp>

using namespace Leeroy;
using json = nlohmann::json;

LEEROY_TEST(Protocol_HelloShape)
{
    auto j = json::parse(EncodeHello(1700000000000, 21, 0));
    CHECK_EQ(j["v"], 1); CHECK_EQ(j["type"], "hello"); CHECK_EQ(j["ts"], 1700000000000);
    CHECK_EQ(j["server"], "mod-leeroy"); CHECK_EQ(j["version"], "0.1.0");
    CHECK_EQ(j["guild_id"], 21); CHECK_EQ(j["human_guid"], 0);
}

LEEROY_TEST(Protocol_ChatShapeAndOneLine)
{
    std::string line = EncodeChat(5, {Channel::Whisper, 7, "Anna", 1, "hi\nthere", 571, 4395});
    CHECK(line.find('\n') == std::string::npos);
    auto j = json::parse(line);
    CHECK_EQ(j["type"], "chat"); CHECK_EQ(j["channel"], "whisper"); CHECK_EQ(j["from_guid"], 7);
    CHECK_EQ(j["from_name"], "Anna"); CHECK_EQ(j["to_guid"], 1); CHECK_EQ(j["text"], "hi\nthere");
    CHECK_EQ(j["map"], 571); CHECK_EQ(j["zone"], 4395); CHECK_EQ(j["via_bridge"], false);
    CHECK_EQ(json::parse(EncodeChat(5, {Channel::Guild, 7, "Anna", 0, "x", 0, 0, true}))["via_bridge"], true);
}

LEEROY_TEST(Protocol_InvalidUtf8DoesNotThrow)
{
    std::string line = EncodeChat(5, {Channel::Say, 7, "A\xFF", 0, "bad\xC3", 0, 0});
    CHECK(json::accept(line));
}

LEEROY_TEST(Protocol_EventShapes)
{
    auto b = json::parse(EncodeBoss(1, {631, 9, 3, "Lord Marrowgar", BossState::Killed, {2, 3}}));
    CHECK_EQ(b["state"], "killed"); CHECK_EQ(b["boss_name"], "Lord Marrowgar"); CHECK_EQ(b["members"], json({2, 3}));
    auto l = json::parse(EncodeLootDrop(1, {631, 9, 36597, {{50274, "Shadowfrost Shard", 4, 2}}}));
    CHECK_EQ(l["source_entry"], 36597); CHECK_EQ(l["items"][0]["item_id"], 50274); CHECK_EQ(l["items"][0]["index"], 2);
    CHECK_EQ(json::parse(EncodeLootWon(1, 7, 50274, "x", LootMethod::Greed))["method"], "greed");
    CHECK_EQ(json::parse(EncodeBgEnd(1, 2, BgWinner::None, {7}))["winner"], "none");
    CHECK_EQ(json::parse(EncodeLogin(1, false, 7, true))["type"], "logout");
    auto a = json::parse(EncodeAck(1, "c1", true, ""));
    CHECK_EQ(a["id"], "c1"); CHECK_EQ(a["ok"], true); CHECK_EQ(a["error"], "");
    CHECK_EQ(json::parse(EncodePong(1, "p"))["id"], "p");
    CHECK_EQ(json::parse(EncodeDcStatus(1, 9, true, false, "STATUS\tx"))["payload"], "STATUS\tx");
    auto dc = json::parse(EncodeDcStatus(1, 9, false, false, ""));   // a run's edge has no addon payload
    CHECK_EQ(dc["enabled"], false); CHECK_EQ(dc["paused"], false); CHECK_EQ(dc["payload"], "");
    auto te = json::parse(EncodeTextEmote(1, 7, "hug", 1, false));
    CHECK_EQ(te["type"], "text_emote"); CHECK_EQ(te["emote"], "hug");
    CHECK_EQ(te["target_guid"], 1); CHECK_EQ(te["via_bridge"], false);
    auto af = json::parse(EncodeAfk(1, 1, true)); CHECK_EQ(af["type"], "afk");
    CHECK_EQ(af["guid"], 1); CHECK_EQ(af["on"], true);
    CHECK_EQ(json::parse(EncodeError(1, "unknown type: dance", "q"))["id"], "q");
    CHECK(!json::parse(EncodeError(1, "bad json")).contains("id"));
    auto ev = json::parse(EncodeEvent(7, {{"type", "job"}, {"id", "t1"}}));
    CHECK_EQ(ev["v"], 1); CHECK_EQ(ev["ts"], 7); CHECK_EQ(ev["type"], "job"); CHECK_EQ(ev["id"], "t1");
}

LEEROY_TEST(Protocol_ChannelNamesRoundTrip)
{
    for (auto n : {"guild", "officer", "party", "raid", "raid_warning", "say", "yell", "whisper", "emote"})
        CHECK_EQ(ChannelName(*ParseChannel(n)), n);
    CHECK(!ParseChannel("trade"));
}

LEEROY_TEST(Protocol_BossTransitions)   // NOT_STARTED 0, IN_PROGRESS 1, FAIL 2, DONE 3, SPECIAL 4, TO_BE_DECIDED 5
{
    CHECK(ClassifyBossTransition(1, 0) == BossState::Engaged);
    CHECK(ClassifyBossTransition(1, 2) == BossState::Engaged);
    CHECK(ClassifyBossTransition(3, 1) == BossState::Killed);  CHECK(ClassifyBossTransition(0, 1) == BossState::Wiped);
    CHECK(ClassifyBossTransition(2, 1) == BossState::Wiped);
    CHECK(!ClassifyBossTransition(3, 5)); CHECK(!ClassifyBossTransition(1, 1));   // loading, no change
    CHECK(!ClassifyBossTransition(0, 3)); CHECK(!ClassifyBossTransition(4, 1));   // reset after kill, SPECIAL
}

LEEROY_TEST(Decode_PingAndIgnoresUnknownFields)
{
    auto r = DecodeCommand(R"({"v":1,"type":"ping","id":"a1","ts":5,"extra":true})");
    CHECK(r.command); CHECK(r.command->type == CommandType::Ping); CHECK_EQ(r.command->id, "a1");
}

LEEROY_TEST(Decode_SayFields)
{
    auto r = DecodeCommand(R"({"v":1,"type":"say","id":"s","guid":42,"channel":"whisper","text":"yo","to_guid":1})");
    CHECK(r.command); CHECK_EQ(r.command->guid, 42u);
    CHECK(r.command->channel == Channel::Whisper); CHECK_EQ(r.command->toGuid, 1u);
    auto w = DecodeCommand(R"({"v":1,"type":"say","id":"s","guid":42,"channel":"whisper","text":"yo"})");
    CHECK(!w.command); CHECK(w.ackable); CHECK_EQ(w.id, "s"); CHECK_EQ(w.error, "bad field: to_guid");
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"say","id":"s","guid":42,"channel":"raid_warning","text":"x"})").error,
        "bad field: channel");
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"say","id":"s","guid":"42","channel":"guild","text":"x"})").error,
        "bad field: guid");
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"say","id":"s","guid":0,"channel":"guild","text":"x"})").error,
        "bad field: guid");
}

LEEROY_TEST(Decode_OtherCommands)
{
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"afk","id":"a","guid":3,"on":"yes"})").error,
        "bad field: on");
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"human_say","id":"h","channel":"whisper","text":"x"})").error,
        "bad field: to_name");
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"set_rank","id":"r","guid":3,"rank":10})").error,
        "bad field: rank");
    auto e = DecodeCommand(R"({"v":1,"type":"emote","id":"e","guid":3,"emote":"hug"})");
    CHECK(e.command); CHECK_EQ(e.command->targetGuid, 0u);
    auto f = DecodeCommand(R"({"v":1,"type":"add_friend","id":"f","guid":3})");
    CHECK(f.command); CHECK_EQ(f.command->note, "");
    auto fm = DecodeCommand(R"({"v":1,"type":"add_friend","id":"f","guid":3,"message":"hi"})");
    CHECK(fm.command); CHECK_EQ(fm.command->message, "");
    auto am = DecodeCommand(R"({"v":1,"type":"afk","id":"a","guid":3,"on":true,"message":"brb"})");
    CHECK(am.command); CHECK_EQ(am.command->message, "brb");
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"afk","id":"a","guid":3,"on":true,"message":5})").error,
        "bad field: message");
    CHECK(DecodeCommand(R"({"v":1,"type":"set_motd","id":"m","text":""})").command);
}

LEEROY_TEST(Decode_MessageLevelErrors)
{
    auto bad = DecodeCommand("not json");
    CHECK(!bad.command); CHECK(!bad.ackable); CHECK_EQ(bad.error, "bad json");
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"ping"})").error, "missing id");
    auto ver = DecodeCommand(R"({"v":2,"type":"ping","id":"a"})");
    CHECK_EQ(ver.error, "unsupported version"); CHECK_EQ(ver.id, "a"); CHECK(!ver.ackable);
    auto u = DecodeCommand(R"({"v":1,"type":"dance","id":"q"})");
    CHECK(!u.ackable); CHECK_EQ(u.error, "unknown type: dance"); CHECK_EQ(u.id, "q");
}

LEEROY_TEST(Decode_ExtensionCommand)   // registry is process-wide: use a name no other test (P1d's Guild_*) registers
{
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"test_ext","id":"x","size":10})").error, "unknown type: test_ext");
    CHECK(RegisterExtensionCommand("test_ext")); CHECK(!RegisterExtensionCommand("test_ext"));
    CHECK(!RegisterExtensionCommand("say")); CHECK(IsExtensionCommand("test_ext"));
    auto r = DecodeCommand(R"({"v":1,"type":"test_ext","id":"x","size":10})");
    CHECK(r.command); CHECK(r.command->type == CommandType::Extension); CHECK_EQ(r.command->extType, "test_ext");
    CHECK_EQ(r.command->id, "x"); CHECK_EQ(r.command->raw["size"], 10);
    CHECK_EQ(DecodeCommand(R"({"v":1,"type":"test_ext","size":10})").error, "missing id");
}
