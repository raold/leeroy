// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyRealmCommands.h"
#include "LeeroyRealmContinuations.h"
#include "LeeroyProtocol.h"
#include "LeeroyWorldProtocol.h"
#include "TestHarness.h"
#include <fstream>
#include <iterator>
#include <limits>
#include <set>
#include <stdexcept>
#include <tuple>

using namespace Leeroy::Realm;
using json = nlohmann::json;

namespace
{
std::vector<json> Fixtures()
{
    std::ifstream file(LEEROY_TEST_FIXTURES "/realm_protocol.jsonl");
    if (!file) throw std::runtime_error("cannot open Realm protocol fixture");
    std::vector<json> rows;
    for (std::string line; std::getline(file, line);)
        if (!line.empty()) rows.push_back(json::parse(line));
    return rows;
}
json Message(std::string_view name)
{
    for (auto const& row : Fixtures())
        if (row.at("name").get<std::string>() == name) return row.at("msg");
    throw std::runtime_error("missing Realm protocol fixture");
}
bool Parse(std::string_view type, json const& j, std::string& error)
{
    if (type == "realm_actors") return ParseActors(j, error).has_value();
    if (type == "realm_say") return ParseSay(j, error).has_value();
    if (type == "get_world_roster") return Leeroy::World::RosterRequest(j,error).has_value();
    if (type == "world_say") return ParseWorldSay(j, error).has_value();
    if (type == "realm_emote") return ParseEmote(j, error).has_value();
    if (type == "realm_move") return ParseMove(j, error).has_value();
    if (type == "rpg_status") return ParseRpgStatus(j, error).has_value();
    if (type == "leave_pvp") return ParseLeavePvp(j, error).has_value();
    if (type == "get_realm_pvp") return ParseGetRealmPvp(j, error).has_value();
    if (type == "rearm") return ParseRearm(j, error).has_value();
    if (type == "wg_join") return ParseWgJoin(j, error).has_value();
    if (type == "arena_team") return ParseArenaTeam(j, error).has_value();
    if (type == "arena_queue") return ParseArenaQueue(j, error).has_value();
    if (type == "duel") return ParseDuel(j, error).has_value();
    if (type == "craft") return ParseCraft(j, error).has_value();
    if (type == "item_count") return ParseItemCount(j, error).has_value();
    if (type == "equip") return ParseEquip(j, error).has_value();
    if (type == "set_profession") return ParseSetProfession(j, error).has_value();
    if (type == "transmog_set") return ParseTransmogSet(j, error).has_value();
    if (type == "calendar_post") return ParseCalendarPost(j, error).has_value();
    if (type == "calendar_signup") return ParseCalendarSignup(j, error).has_value();
    if (type == "calendar_update") return ParseCalendarUpdate(j, error).has_value();
    if (type == "mail_send") return ParseMailSend(j, error).has_value();
    if (type == "bank_tab_buy") return ParseBankTabBuy(j, error).has_value();
    if (type == "bank_money") return ParseBankMoney(j, error).has_value();
    if (type == "bank_deposit") return ParseBankDeposit(j, error).has_value();
    if (type == "bank_withdraw") return ParseBankWithdraw(j, error).has_value();
    if (type == "bank_stock") return ParseBankStock(j, error).has_value();
    if (type == "grant_achievement") return ParseGrantAchievement(j, error).has_value();
    if (type == "guild_rename") return ParseGuildRename(j, error).has_value();
    if (type == "guild_grow") return ParseGuildGrow(j, error).has_value();
    if (type == "guild_admit") return ParseGuildAdmit(j, error).has_value();
    if (type == "guild_note") return ParseGuildNote(j, error).has_value();
    if (type == "guild_readmit") return ParseGuildReadmit(j, error).has_value();
    if (type == "announce") return ParseAnnounce(j, error).has_value();
    throw std::runtime_error("unhandled Realm command parser");
}
void Reject(json const& msg, std::string const& field)
{
    std::string error;
    CHECK(!Parse(msg.at("type").get<std::string>(), msg, error));
    CHECK_EQ(error, "bad field: " + field);
}
void RejectField(std::string_view fixture, char const* field, json bad)
{
    auto msg = Message(fixture); msg[field] = std::move(bad); Reject(msg, field);
}
json Event(std::string_view type)
{
    if (type == "world_roster") return *Leeroy::World::RosterPage("roster-token",21,1,0,1,json::array({
        json{{"guid",101},{"account",11},{"guild_id",50},{"guild_name","Test rivals"},
             {"name","Fixture"},{"race",2},{"class",1},{"gender",0},{"level",24},{"rank",3},
             {"online",true},{"map",1},{"zone",1637}}}));
    if (type == "realm_kill" || type == "realm_dungeon_kill")
    {
        Kill kill;
        kill.killId = "0123456789abcdef0123456789abcdef:1";
        kill.nativeEpoch = 7; kill.guildId = 12001; kill.humanGuid = 11002;
        kill.map = 631; kill.instanceId = 15001; kill.bossId = 0;
        kill.difficulty = 1; kill.size = 25; kill.heroic = false;
        kill.killedAtMs = 1700000000000; kill.verifiedAtMs = 1700000000001;
        kill.humanPresent = true; kill.castMembers = {11001, 11003};
        if (type == "realm_kill") return MakeRealmKill(kill);
        kill.map = 574; kill.instanceId = 15002; kill.bossId = 2;
        kill.difficulty = 1; kill.size = 5; kill.heroic = true; kill.attempts = 2;
        return MakeRealmDungeonKill(kill);
    }
    if (type == "realm_job") return MakeRealmJob({"realm-test", "rearm", true, "", 11001, 2});
    if (type == "realm_pvp")
    {
        PvpOwner owner{"0123456789abcdef0123456789abcdef-0000000000000001", "queue-one",
            PvpKind::RatedArena, 7, 3, 30, 1, {101, 102}, PvpStage::Cleaning, true, "", "cleanup-one"};
        return MakeRealmPvp("query-one", owner);
    }
    if (type == "channel_chat") return MakeChannelChat("trade", 11001, "Test Member", 1637, "Ready for a raid.", true);
    if (type == "pvp_kill") return MakePvpKill(11001, 11002, 571, 4197);
    if (type == "duel_end") return MakeDuelEnd(11001, 11002, "won", 0, 33, false);
    if (type == "realm_achievement")
        return MakeRealmAchievement(11002, 4576, "Realm First! Fall of the Lich King", 81, 512, true);
    if (type == "pvp_flag") return MakePvpFlag(11001, true);
    if (type == "wg") return MakeWg("soon", "horde", 900, 3.5);
    if (type == "arena_queue") return MakeArenaQueue(13001, 3, true);
    if (type == "arena_end") return MakeArenaEnd(3, 13001, 13002, 12, -12, {11001, 11002});
    if (type == "game_event") return MakeGameEvent(20, "start");
    if (type == "calendar_rsvp") return MakeCalendarRsvp(14001, "accepted");
    if (type == "mail_taken") return MakeMailTaken(16001, 43015, 2);
    if (type == "guard") return MakeGuard(11001, "readded", 4, "");
    throw std::runtime_error("unhandled Realm event maker");
}
// Outgoing native events have no wire decoder. These strict conversions of the
// shared fixture lines exercise the exact native wire types and validators;
// nullopt is a line the native maker could never produce.
bool SameKeys(json const& msg, json const& body)
{
    std::set<std::string> expected{"v", "ts"}, actual;
    for (auto const& [key, value] : body.items()) { (void)value; expected.insert(key); }
    for (auto const& [key, value] : msg.items()) { (void)value; actual.insert(key); }
    return actual == expected;
}
std::optional<std::int64_t> Signed(json const& value)
{
    if (!value.is_number_integer()) return std::nullopt;
    if (value.is_number_unsigned())
    {
        auto const number = value.get<std::uint64_t>();
        if (number > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) return std::nullopt;
        return static_cast<std::int64_t>(number);
    }
    return value.get<std::int64_t>();
}
template <typename T>
bool Unsigned(json const& value, T& out)
{
    if (!value.is_number_unsigned() || value.get<std::uint64_t>() > std::numeric_limits<T>::max()) return false;
    out = static_cast<T>(value.get<std::uint64_t>());
    return true;
}
std::optional<Kill> FixtureKill(json const& msg)
{
    if (!SameKeys(msg, Event("realm_kill")) || !msg.at("kill_id").is_string() || !msg.at("heroic").is_boolean()
        || !msg.at("human_present").is_boolean() || !msg.at("cast_members").is_array()) return std::nullopt;
    Kill kill;
    kill.killId = msg.at("kill_id").get<std::string>();
    kill.heroic = msg.at("heroic").get<bool>();
    kill.humanPresent = msg.at("human_present").get<bool>();
    auto const difficulty = Signed(msg.at("difficulty"));
    auto const elapsed = Signed(msg.at("fight_ms"));
    if (!difficulty || *difficulty < std::numeric_limits<std::int32_t>::min()
        || *difficulty > std::numeric_limits<std::int32_t>::max() || !elapsed) return std::nullopt;
    kill.difficulty = static_cast<std::int32_t>(*difficulty);
    kill.fightMs = *elapsed;
    if (!Unsigned(msg.at("native_epoch"), kill.nativeEpoch) || !Unsigned(msg.at("guild_id"), kill.guildId)
        || !Unsigned(msg.at("human_guid"), kill.humanGuid) || !Unsigned(msg.at("map"), kill.map)
        || !Unsigned(msg.at("instance_id"), kill.instanceId) || !Unsigned(msg.at("boss_id"), kill.bossId)
        || !Unsigned(msg.at("size"), kill.size) || !Unsigned(msg.at("attempts"), kill.attempts)
        || !Unsigned(msg.at("killed_at_ms"), kill.killedAtMs) || !Unsigned(msg.at("verified_at_ms"), kill.verifiedAtMs)
        || !Unsigned(msg.at("fight_started_at_ms"), kill.fightStartedAtMs)
        || !Unsigned(msg.at("ts"), kill.publishedAtMs)) return std::nullopt;
    for (auto const& member : msg.at("cast_members"))
    {
        std::uint32_t guid = 0;
        if (!Unsigned(member, guid)) return std::nullopt;
        kill.castMembers.push_back(guid);
    }
    return kill;
}
std::optional<json> FixtureBody(json const& msg)
{
    auto const type = msg.at("type").get<std::string>();
    if (type == "realm_kill" || type == "realm_dungeon_kill")
    {
        auto const kill = FixtureKill(msg);
        if (!kill || !ValidKillProof(*kill) || IsDungeonKill(*kill) != (type == "realm_dungeon_kill")) return std::nullopt;
        return type == "realm_kill" ? MakeRealmKill(*kill) : MakeRealmDungeonKill(*kill);
    }
    if (type == "world_roster")
    {
        using namespace Leeroy::World;
        if (!SameKeys(msg,Event(type)) || !Token(msg.at("request"))) return std::nullopt;
        for (auto key : {"own_guild","human","page","pages","total"})
            if (!Number(msg.at(key),0,UINT32_MAX)) return std::nullopt;
        auto body=RosterPage(msg.at("request").get<std::string>(),msg.at("own_guild").get<std::uint32_t>(),
            msg.at("human").get<std::uint32_t>(),msg.at("page").get<std::size_t>(),
            msg.at("total").get<std::size_t>(),msg.at("members"));
        if (body && body->at("pages") != msg.at("pages")) return std::nullopt;
        return body;
    }
    if (type == "duel_end")
    {
        std::uint32_t winner = 0, loser = 0, map = 0, zone = 0;
        if (!SameKeys(msg, Event(type)) || !Unsigned(msg.at("winner"), winner) || !Unsigned(msg.at("loser"), loser)
            || !Unsigned(msg.at("map"), map) || !Unsigned(msg.at("zone"), zone) || !msg.at("city").is_boolean()
            || !msg.at("result").is_string()) return std::nullopt;
        auto const result = msg.at("result").get<std::string>();
        if (result != "won" && result != "fled" && result != "interrupted") return std::nullopt;
        return MakeDuelEnd(winner, loser, result, map, zone, msg.at("city").get<bool>());
    }
    if (type == "realm_achievement")
    {
        std::uint32_t guid = 0, id = 0, category = 0, flags = 0;
        if (!SameKeys(msg, Event(type)) || !Unsigned(msg.at("guid"), guid) || !Unsigned(msg.at("achievement_id"), id)
            || !Unsigned(msg.at("category"), category) || !Unsigned(msg.at("flags"), flags)
            || !msg.at("name").is_string() || !msg.at("realm_first").is_boolean()) return std::nullopt;
        bool const realmFirst = msg.at("realm_first").get<bool>();
        // The native hook names a realm first only from the DBC flag (and RBAC may withhold it).
        if (realmFirst && !IsRealmFirstAchievement(flags)) return std::nullopt;
        return MakeRealmAchievement(guid, id, msg.at("name").get<std::string>(), category, flags, realmFirst);
    }
    return Event(type); // Every other event has only its canonical line.
}
std::string RealmSource(char const* file)
{
    std::ifstream stream(std::string(LEEROY_TEST_FIXTURES) + "/../../src/Realm/" + file);
    if (!stream) throw std::runtime_error("cannot open native source contract");
    std::string source{std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
    std::erase(source, '\r');
    return source;
}
std::string RealmSection(std::string const& source, std::string const& start, std::string const& end)
{
    auto const first = source.find(start);
    auto const last = first == std::string::npos ? first : source.find(end, first + start.size());
    if (first == std::string::npos || last == std::string::npos)
        throw std::runtime_error("native source contract section missing: " + start);
    return source.substr(first, last - first);
}
std::string const kPlacesHeader = "name,map,zone,x,y,z,o,kind\n";
std::string const kItemsHeader = "item_id,use,max_count\n";
}

LEEROY_TEST(Realm_FixtureLines)
{
    RegisterRealmCommandTypes();
    std::set<std::string> names, full, events;
    for (auto const& row : Fixtures())
    {
        CHECK(names.insert(row.at("name").get<std::string>()).second);
        auto msg = row.at("msg"); auto type = msg.at("type").get<std::string>();
        if (row.at("kind") == "event")
        {
            events.insert(type);
            auto const ts = Signed(msg.at("ts"));
            auto const body = FixtureBody(msg);
            bool const produced = ts && body && json::parse(Leeroy::EncodeEvent(*ts, *body)) == msg;
            if (produced != row.value("valid", true))
                LeeroyTest::Fail(__FILE__, __LINE__, row.at("name").get<std::string>() + ": native validity differs");
            if (row.value("full", false))
                CHECK_EQ(json::parse(Leeroy::EncodeEvent(msg.at("ts").get<std::int64_t>(), Event(type))), msg);
            continue;
        }
        auto decoded = Leeroy::DecodeCommand(msg.dump());
        if (row.contains("queued")) CHECK_EQ(IsQueued(type), row.at("queued").get<bool>());
        CHECK(decoded.command.has_value());
        if (!decoded.command) continue;
        CHECK(decoded.command->type == Leeroy::CommandType::Extension);
        CHECK_EQ(decoded.command->raw, msg);
        std::string error = "stale error";
        bool valid = Parse(type, decoded.command->raw, error);
        if (valid != row.at("valid").get<bool>())
            LeeroyTest::Fail(__FILE__, __LINE__, row.at("name").get<std::string>() + ": " + error);
        CHECK_EQ(valid, row.at("valid").get<bool>());
        CHECK_EQ(error, row.at("error").get<std::string>());
        if (row.value("full", false))
        {
            CHECK(valid); CHECK_EQ(row.at("name").get<std::string>(), type + "_ok"); full.insert(type);
        }
    }
    CHECK_EQ(full.size(), kCommandTypes.size()); CHECK_EQ(events.size(), 17u);
    for (auto type : kCommandTypes) CHECK(full.contains(std::string(type)));
}

LEEROY_TEST(Realm_RegisterTypes)
{
    RegisterRealmCommandTypes(); RegisterRealmCommandTypes();
    for (auto type : kCommandTypes) CHECK(Leeroy::IsExtensionCommand(type));
    CHECK(!Leeroy::RegisterExtensionCommand("rearm"));
}

LEEROY_TEST(Realm_PvpOwnerTokenAndSelectorsAreStrictAndBounded)
{
    auto msg = Message("leave_pvp_owner");
    std::string error;
    auto parsed = ParseLeavePvp(msg, error);
    CHECK(parsed && ContinuationLedger::ValidToken(parsed->pvpOwner));
    for (json bad : {json(nullptr), json(42), json("queue-one"), json(std::string(50, 'a')),
        json("0123456789ABCDEF0123456789abcdef-0000000000000001")})
    { msg["pvp_owner"] = bad; Reject(msg, "pvp_owner"); }
    CHECK(ParseLeavePvp(Message("leave_pvp_ok"), error)->pvpOwner.empty());
    auto query = Message("get_realm_pvp_ok");
    query["origin_id"] = "queue-one"; Reject(query, "owner_selector");
    query.erase("pvp_owner"); CHECK(ParseGetRealmPvp(query, error));
    query["origin_id"] = std::string(129, 'x'); Reject(query, "origin_id");
    auto job = MakeRealmJob({"queue-one", "arena_queue", true, "", 0, 2, parsed->pvpOwner});
    CHECK_EQ(job.at("pvp_owner").get<std::string>(), parsed->pvpOwner);
    CHECK(!MakeRealmJob({"ordinary", "rearm", true}).contains("pvp_owner"));
}

LEEROY_TEST(Realm_QueuedAndValue)
{
    std::set<std::string_view> queued = {"realm_move", "rpg_status", "leave_pvp", "rearm", "wg_join", "arena_queue", "duel",
        "craft", "equip", "set_profession", "transmog_set", "calendar_signup", "mail_send", "bank_deposit",
        "bank_withdraw", "bank_stock", "guild_grow", "arena_team"};
    std::set<std::string_view> values = {"arena_team", "item_count", "calendar_post"};
    for (auto type : kCommandTypes)
    {
        CHECK_EQ(IsQueued(type), queued.contains(type)); CHECK_EQ(ReturnsValue(type), values.contains(type));
    }
    CHECK(!IsQueued("")); CHECK(!IsQueued("unknown")); CHECK(!ReturnsValue("unknown"));
}

LEEROY_TEST(Realm_DefaultsAndConditionalFields)
{
    std::string error;
    auto actors = ParseActors(Message("realm_actors_defaults"), error);
    CHECK(actors && actors->guids.empty() && actors->gankers.empty() && actors->guilds.empty());
    CHECK_EQ(ParseSay(Message("realm_say_defaults"), error)->toGuid, 0u);
    CHECK_EQ(ParseEmote(Message("realm_emote_defaults"), error)->targetGuid, 0u);
    CHECK_EQ(ParseMove(Message("realm_move_defaults"), error)->slot, 0u);
    CHECK_EQ(ParseRpgStatus(Message("rpg_status_defaults"), error)->questId, 0u);
    CHECK_EQ(ParseCraft(Message("craft_defaults"), error)->count, 1u);
    auto post = ParseCalendarPost(Message("calendar_post_defaults"), error);
    CHECK(post && post->description.empty() && post->guildEvent && !post->inviteHuman && post->dungeonId == -1);
    auto update = ParseCalendarUpdate(Message("calendar_update_defaults"), error);
    CHECK(update && update->title.empty() && update->description.empty() && update->at == 0);
    auto mail = ParseMailSend(Message("mail_send_defaults"), error);
    CHECK(mail && mail->body.empty() && mail->items.empty() && mail->money == 0 && mail->delayS == 600);
    auto deposit = ParseBankDeposit(Message("bank_deposit_ok"), error);
    CHECK(deposit && deposit->count == 0);  // Explicit zero is the deliberate "all" request.
    Reject(Message("bank_deposit_missing_count"), "count");
    CHECK_EQ(ParseGuildAdmit(Message("guild_admit_defaults"), error)->rank, 4u);
    auto announce = ParseAnnounce(Message("announce_defaults"), error);
    CHECK(announce && announce->attempts == 0 && announce->fightS == 0);
    auto add = ParseArenaTeam(Message("arena_team_add"), error);
    CHECK(add && add->arenaType == 0 && add->captainGuid == 0 && add->name.empty() && add->guid == 11001);
    RejectField("arena_team_add", "arena_type", 3u);
    RejectField("arena_team_ok", "team_id", 13001u);
    RejectField("realm_say_defaults", "to_guid", 11002u);
    auto noArenaType = Message("arena_team_ok"); noArenaType.erase("arena_type"); Reject(noArenaType, "arena_type");
    auto noEventType = Message("calendar_post_ok"); noEventType.erase("event_type"); Reject(noEventType, "event_type");
    auto tab = Message("bank_tab_buy_ok"); tab["icon"] = "";
    CHECK(ParseBankTabBuy(tab, error).has_value());
    tab["icon"] = std::string(32, 'x'); CHECK(ParseBankTabBuy(tab, error).has_value());
    tab["icon"] = std::string(33, 'x'); Reject(tab, "icon");
    RejectField("bank_tab_buy_ok", "icon", 0u);
}

LEEROY_TEST(Realm_NumbersDoNotAcceptBoolFloatStringOrNarrow)
{
    for (auto const& row : Fixtures())
    {
        if (row.at("kind") != "command" || !row.value("full", false)) continue;
        auto msg = row.at("msg");
        for (auto const& [key, value] : msg.items())
        {
            if (!value.is_number_integer() || key == "v" || key == "ts") continue;
            for (auto const& bad : std::vector<json>{true, 1.0, "1", nullptr, json::array(), -2})
            { auto changed = msg; changed[key] = bad; Reject(changed, key); }
            if (key != "dungeon_id" && key != "difficulty")
            { auto changed = msg; changed[key] = json::number_integer_t(1); Reject(changed, key); }
            if (key != "event_id" && key != "native_epoch")
            { auto changed = msg; changed[key] = std::numeric_limits<std::uint64_t>::max(); Reject(changed, key); }
        }
    }
    std::string error;
    auto event = Message("calendar_signup_ok"); event["event_id"] = std::numeric_limits<std::uint64_t>::max();
    auto parsed = ParseCalendarSignup(event, error);
    CHECK(parsed && parsed->eventId == std::numeric_limits<std::uint64_t>::max());
    auto guild = Message("guild_admit_defaults"); guild["guid"] = std::numeric_limits<std::uint32_t>::max();
    CHECK(ParseGuildAdmit(guild, error).has_value());
    guild["guid"] = std::uint64_t{1} << 32; Reject(guild, "guid");
    auto post = Message("calendar_post_ok"); post["dungeon_id"] = std::uint32_t{2147483647};
    CHECK(ParseCalendarPost(post, error).has_value());
    post["dungeon_id"] = std::uint64_t{2147483648}; Reject(post, "dungeon_id");
}

LEEROY_TEST(Realm_AnnounceRequiresExactProofBindingAndEveryPayloadField)
{
    auto const original = Message("announce_ok");
    for (auto key : {"id", "kill_id", "native_epoch", "guild_id", "human_guid", "map", "instance_id",
        "boss_id", "difficulty", "size", "heroic", "boss_name", "attempts", "fight_s", "first"})
    {
        auto msg = original; msg.erase(key); Reject(msg, key);
    }
    for (auto const& bad : {std::string{}, std::string("0123456789ABCDEF0123456789abcdef:1"),
        std::string("0123456789abcdef0123456789abcdef:0"), std::string("0123456789abcdef0123456789abcdef:01"),
        std::string("0123456789abcdef0123456789abcdef:18446744073709551616"),
        std::string("0123456789abcdef0123456789abcdef:1:2")})
        RejectField("announce_ok", "kill_id", bad);
    for (auto field : {"native_epoch", "guild_id", "human_guid", "instance_id"})
        RejectField("announce_ok", field, 0u);
    auto msg = original;
    msg["native_epoch"] = std::numeric_limits<std::uint64_t>::max();
    msg["kill_id"] = "0123456789abcdef0123456789abcdef:18446744073709551615";
    std::string error;
    auto parsed = ParseAnnounce(msg, error);
    CHECK(parsed && parsed->nativeEpoch == std::numeric_limits<std::uint64_t>::max());
    for (auto field : {"v", "type", "ts"})
    {
        msg = original; msg.erase(field);
        CHECK(!ParseAnnounce(msg, error)); CHECK_EQ(error, std::string("bad field: ") + field);
    }
    msg = original; msg["difficulty"] = -1; CHECK(ParseAnnounce(msg, error));
    msg["difficulty"] = 0; Reject(msg, "heroic"); // 25 normal cannot impersonate 10 normal.
    msg = original; msg["heroic"] = true; Reject(msg, "heroic");
    msg = original; msg["size"] = 40u; Reject(msg, "size");
    msg = original; msg["legacy_tuple"] = true; Reject(msg, "legacy_tuple");
    msg = original; msg["attempts"] = 99u; msg["fight_s"] = 3600u;
    parsed = ParseAnnounce(msg, error);
    CHECK(parsed && parsed->attempts == 99 && parsed->fightS == 3600); // Structural ranges stay unchanged.
    auto legacy = original;
    for (auto field : {"kill_id", "native_epoch", "guild_id", "human_guid", "difficulty", "size", "heroic"})
        legacy.erase(field);
    Reject(legacy, "kill_id");
}

LEEROY_TEST(Realm_StringsBooleansAndContainersRequireTheirTypes)
{
    for (auto const& row : Fixtures())
    {
        if (row.at("kind") != "command" || !row.value("full", false)) continue;
        auto msg = row.at("msg");
        for (auto const& [key, value] : msg.items())
        {
            if (key == "id" || key == "type") continue;
            if (value.is_string())
                for (auto const& bad : std::vector<json>{false, 5u, nullptr, json::array(), std::string("a\0b", 3)})
                { auto changed = msg; changed[key] = bad; Reject(changed, key); }
            if (value.is_boolean())
                for (auto const& bad : std::vector<json>{0u, 1u, "true", nullptr})
                { auto changed = msg; changed[key] = bad; Reject(changed, key); }
            if (value.is_array())
                for (auto const& bad : std::vector<json>{false, 1u, "1", nullptr, json::object()})
                { auto changed = msg; changed[key] = bad; Reject(changed, key); }
        }
        std::string error;
        CHECK(!Parse(msg.at("type").get<std::string>(), json::array(), error));
        CHECK(error.starts_with("bad field: "));
    }
}

LEEROY_TEST(Realm_ListBoundsAndNestedMailTypes)
{
    for (auto const& [type, cap] : std::vector<std::pair<std::string, std::uint32_t>>{
            {"realm_actors",16}, {"leave_pvp",150}, {"rearm",150}, {"wg_join",40}, {"guild_grow",30}})
    {
        auto msg = Message(type + "_ok"); msg["guids"] = json::array();
        for (std::uint32_t i = 0; i < cap; ++i) msg["guids"].push_back(11001u+i);
        std::string error; CHECK(Parse(type, msg, error));
        msg["guids"].push_back(11999u); Reject(msg, "guids");
        for (auto const& bad : std::vector<json>{true, 1.0, -1, "11001", nullptr, std::uint64_t{1}<<32})
        { auto changed = Message(type + "_ok"); changed["guids"][0] = bad; Reject(changed, "guids"); }
    }
    for (auto field : {"item_id", "count", "from"})
        for (auto const& bad : std::vector<json>{true, nullptr, json::array(), 1.0})
        { auto msg = Message("mail_send_ok"); msg["items"][0][field] = bad; Reject(msg, "items"); }
    for (auto const& bad : std::vector<json>{false, nullptr, "item", json::array()})
    { auto msg = Message("mail_send_ok"); msg["items"][0] = bad; Reject(msg, "items"); }
    // Two stacks of the same item are distinct mail attachments, not duplicate identities.
    auto mail = Message("mail_send_ok"); mail["items"].push_back(mail["items"][0]);
    std::string error; CHECK(ParseMailSend(mail, error).has_value());
}

LEEROY_TEST(Realm_GrantWhitelist)
{
    CHECK((std::set<std::uint32_t>(kGrantable.begin(), kGrantable.end()) ==
        std::set<std::uint32_t>{3117,4078,4576,2186,3007,1723,1786}));
    for (auto id : kGrantable)
    { auto msg = Message("grant_achievement_ok"); msg["achievement_id"] = id; std::string error; CHECK(ParseGrantAchievement(msg, error)); }
    for (auto id : {1402u,456u,1400u,3259u}) RejectField("grant_achievement_ok", "achievement_id", id);
}

LEEROY_TEST(Realm_EquipOnlyTheTabard)
{
    static_assert(kGuildTabard == 5976u);
    std::string error; CHECK(ParseEquip(Message("equip_ok"), error));
    for (auto item : {6256u,0u,43015u}) RejectField("equip_ok", "item_id", item);
}

LEEROY_TEST(Realm_ProfessionAndStatusWhitelists)
{
    for (auto skill : kProfessionSkills)
    {
        auto msg = Message("set_profession_ok"); msg["skill_id"] = skill; std::string error;
        CHECK(ParseSetProfession(msg, error)); CHECK_EQ(IsPrimaryProfession(skill), skill != 185 && skill != 356 && skill != 129);
    }
    CHECK(!IsPrimaryProfession(0)); CHECK(!IsPrimaryProfession(9999));
    for (auto status : kRpgStatuses)
    { auto msg = Message("rpg_status_defaults"); msg["status"] = status; std::string error; CHECK(ParseRpgStatus(msg, error)); }
    for (auto channel : kSayChannels)
    {
        auto msg = Message("realm_say_defaults"); msg["channel"] = channel;
        if (channel == "whisper") msg["to_guid"] = 11002u;
        std::string error; CHECK(ParseSay(msg, error));
    }
}

LEEROY_TEST(Realm_PlacesCsv)
{
    std::string error;
    auto csv = kPlacesHeader + "test_city,1,1637,1,2,3,0,city\r\n# comment\n\n"
        "test_inn,571,4395,-1.5,2,3,6.28,inn\n test_fish , 0 , 1519 , 1 , 2 , 3 , 0 , fish \n";
    auto places = ParsePlaces(csv, error);
    CHECK(places && places->size() == 3); CHECK(error.empty());
    if (places) { CHECK_EQ((*places)[1].map,571u); CHECK_EQ((*places)[1].x,-1.5f); }
    for (auto row : {"test,9999,1637,1,2,3,0,city", "test,1,1637,1,2,3,0,lounge",
        "test,1,1637,1,2,3,0", "test,1,1637,1,2,3,0,city,extra", "test,-1,1637,1,2,3,0,city",
        "test,4294967296,1637,1,2,3,0,city", "test,1,0,1,2,3,0,city", "test,1,65536,1,2,3,0,city",
        "test,1,1637,nan,2,3,0,city", "test,1,1637,1,inf,3,0,city", "test,1,1637,1,2,1e100,0,city",
        "test,1,1637,20001,2,3,0,city", "test,1,1637,1,2,3,-1,city", "test,1,1637,1,2,3,6.3,city",
        "test,1,1637,1x,2,3,0,city", "../test,1,1637,1,2,3,0,city", "near_human,1,1637,1,2,3,0,city"})
    { CHECK(!ParsePlaces(kPlacesHeader + row, error)); CHECK(error.starts_with("line 2: ")); }
    CHECK(!ParsePlaces(kPlacesHeader + "test,1,1637,1,2,3,0,city\n# comment\ntest,1,1637,1,2,3,0,inn", error));
    CHECK_EQ(error, "line 4: duplicate name");
    for (auto kind : kPlaceKinds)
    { CHECK(ParsePlaces(kPlacesHeader + "test,1,1637,1,2,3,0," + std::string(kind), error)); }
}

LEEROY_TEST(Realm_ItemsCsv)
{
    std::string error;
    auto rules = ParseItems(kItemsHeader + "43015,gift,20\n43015,bank,1000\n34054,holiday,1\n36919,prize,5", error);
    CHECK(rules && rules->size() == 4); CHECK(error.empty());
    for (auto row : {"43015,equip,1", "43015,sell,1", "0,gift,1", "4294967296,gift,1", "-1,gift,1",
        "43015,gift,0", "43015,gift,1001", "43015,gift,1.0", "43015,gift,1,extra", "43015,gift", "43015,gift,1junk"})
    { CHECK(!ParseItems(kItemsHeader + row, error)); CHECK(error.starts_with("line 2: ")); }
    CHECK(!ParseItems(kItemsHeader + "43015,gift,20\n43015,gift,1", error)); CHECK_EQ(error, "line 3: duplicate item/use");
}

LEEROY_TEST(Realm_CsvInputBoundsAndHeaders)
{
    std::string error;
    CHECK(ParsePlaces(kPlacesHeader, error)->empty()); CHECK(ParseItems(kItemsHeader, error)->empty());
    CHECK(!ParsePlaces("", error)); CHECK_EQ(error, "line 1: missing header");
    CHECK(!ParseItems("item,use,count\n", error)); CHECK_EQ(error, "line 1: invalid header");
    CHECK(!ParsePlaces(kPlacesHeader + std::string(kMaxCsvLineBytes + 1, 'x'), error)); CHECK_EQ(error, "line 2: line too long");
    CHECK(!ParseItems(std::string(kMaxCsvBytes + 1, 'x'), error)); CHECK_EQ(error, "line 1: file too large");
    CHECK(!ParseItems(kItemsHeader + "\"43015\",gift,1", error)); CHECK_EQ(error, "line 2: invalid characters");
    CHECK(!ParseItems(kItemsHeader + std::string("43015,gift,1\0", 13), error)); CHECK_EQ(error, "line 2: invalid characters");
    auto csv = kItemsHeader;
    for (std::uint32_t i = 0; i <= kMaxCsvRows; ++i) csv += std::to_string(i+10000) + ",bank,1\n";
    CHECK(!ParseItems(csv, error)); CHECK_EQ(error, "line 4098: too many rows");
}

LEEROY_TEST(Realm_DistributionsParse)
{
    for (auto const& [file, places] : std::vector<std::pair<std::string, bool>>{
            {"leeroy_places.csv.dist", true}, {"leeroy_items.csv.dist", false}})
    {
        std::ifstream input(std::string(LEEROY_TEST_FIXTURES) + "/../../conf/" + file);
        CHECK(input.is_open());
        std::string csv((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
        std::string error;
        if (places) { auto rows = ParsePlaces(csv, error); CHECK(rows && !rows->empty()); }
        else { auto rows = ParseItems(csv, error); CHECK(rows && !rows->empty()); }
        CHECK(error.empty());
    }
}

LEEROY_TEST(Realm_JobEventShape)
{
    RealmJob e{"job", "rearm", false, "busy:combat", 11001, std::numeric_limits<std::uint64_t>::max()};
    auto body = MakeRealmJob(e);
    CHECK_EQ(body.size(), 7u); CHECK_EQ(body.at("state"), "failed"); CHECK_EQ(body.at("value").get<std::uint64_t>(), e.value);
    CHECK(!body.contains("v") && !body.contains("ts") && !body.contains("training_run"));
    e.ok = true; e.reason.clear(); CHECK_EQ(MakeRealmJob(e).at("state"), "done");
    CHECK_EQ(MakeArenaQueue(13001,3,true).at("type"), "arena_queue");
    CHECK_EQ(MakeArenaEnd(3,13001,13002,12,-12,{}).at("arena_type"), 3u);
    auto encoded = json::parse(Leeroy::EncodeEvent(123, MakeRealmJob(e)));
    CHECK_EQ(encoded.at("ts"), 123); CHECK_EQ(encoded.at("v"), 1);
}

LEEROY_TEST(Realm_GuildReadmitIsAFrozenConsentBoundSchema)
{
    std::string error;
    auto parsed = ParseGuildReadmit(Message("guild_readmit_ok"), error);
    CHECK(parsed && parsed->id == "realm-test" && parsed->guid == 11001 && parsed->kickTs == 1700000000000
        && parsed->rank == 4);
    parsed = ParseGuildReadmit(Message("guild_readmit_officer_rank"), error);
    CHECK(parsed && parsed->rank == 2);
    for (auto field : {"id", "guid", "kick_ts", "rank", "v", "type", "ts"})
    {
        auto msg = Message("guild_readmit_ok"); msg.erase(field);
        CHECK(!ParseGuildReadmit(msg, error)); CHECK_EQ(error, std::string("bad field: ") + field);
    }
    auto msg = Message("guild_readmit_ok"); msg["kick_ts"] = 253402300799999u;
    CHECK(ParseGuildReadmit(msg, error));
    msg["type"] = "guild_admit";
    CHECK(!ParseGuildReadmit(msg, error)); CHECK_EQ(error, "bad field: type");
    RejectField("guild_readmit_ok", "rank", 0u);
    RejectField("guild_readmit_ok", "kick_ts", -1);
    // No rank default: guild_admit's rank 4 never applies to a re-admission.
    auto admit = Message("guild_admit_defaults"); CHECK(!ParseGuildReadmit(admit, error));
}

LEEROY_TEST(Realm_RealmFirstUsesTheCoreAchievementFlags)
{
    for (auto flags : {0x100u, 0x200u, 0x300u, 0x302u}) CHECK(IsRealmFirstAchievement(flags));
    for (auto flags : {0u, 0x1u, 0x80u, 0x400u}) CHECK(!IsRealmFirstAchievement(flags));
    auto achievement = MakeRealmAchievement(1, 4576, "Realm First! Fall of the Lich King", 81, 0x200, true);
    CHECK_EQ(achievement.size(), 7u); CHECK_EQ(achievement.at("type"), "realm_achievement");
    CHECK_EQ(achievement.at("realm_first"), true); CHECK_EQ(achievement.at("category"), 81u);
    // Source placement only: a quiet grant never reaches the publication.
    auto const guild = RealmSource("RealmGuild.cpp");
    auto const hook = RealmSection(guild, "void OnPlayerAchievementComplete(", "void RegisterRealmGuildHandlers()");
    CHECK(hook.find("LeeroyQuietScope::Active()") < hook.find("Publish(MakeRealmAchievement("));
    CHECK(hook.find("!RealmModule::Instance().On()") < hook.find("Publish(MakeRealmAchievement("));
    CHECK(hook.find("RBAC_PERM_CANNOT_EARN_REALM_FIRST_ACHIEVEMENTS") != std::string::npos);
    auto const grant = RealmSection(guild, "LeeroyCommandResult Grant(", "class RealmAchievementScript");
    CHECK(grant.find("LeeroyQuietScope quiet;") < grant.find("player->CompletedAchievement(achievement);"));
}

LEEROY_TEST(Realm_DuelCityUsesTheCoreZoneFlag)
{
    // AreaTable.dbc 3.3.5a flags: Orgrimmar 0x200138, Undercity 0x4200138,
    // Dalaran 0x20004d28 and Shattrath 0x4d28 carry AREA_FLAG_CAPITAL; the
    // "City" channel zone (0x200), Stranglethorn (0x40) and Wintergrasp do not.
    for (auto flags : {0x200138u, 0x4200138u, 0x20004d28u, 0x4d28u}) CHECK(IsCityZone(flags));
    for (auto flags : {0x200u, 0x40u, 0x9004441u, 0u}) CHECK(!IsCityZone(flags));
    auto duel = MakeDuelEnd(1, 2, "fled", 1, 1637, true);
    CHECK_EQ(duel.size(), 7u); CHECK_EQ(duel.at("map"), 1u); CHECK_EQ(duel.at("zone"), 1637u);
    CHECK_EQ(duel.at("city"), true);
    // Source placement only: the zone flag is the core's, for either duelist.
    auto const duelHook = RealmSection(RealmSource("RealmBodies.cpp"), "void OnPlayerDuelEnd(", "void OnPlayerPVPFlagChange(");
    CHECK(duelHook.find("sAreaTableStore.LookupEntry(player->GetZoneId())") < duelHook.find("IsCityZone(zone->flags)"));
    CHECK(duelHook.find("inCity(winner) || inCity(loser)") != std::string::npos);
}

LEEROY_TEST(Realm_AnnouncementText)
{
    CHECK_EQ(FormatAnnouncement("<Testguild>","Lord Marrowgar",25,false,1,251,true),
        "<Testguild> killed Lord Marrowgar (25) for the first time, on the first attempt, in 4:11.");
    CHECK_EQ(FormatAnnouncement("<Testguild>","Lord Marrowgar",25,false,0,0,false),
        "<Testguild> killed Lord Marrowgar (25) this week.");
    CHECK_EQ(FormatAnnouncement("<Testguild>","Lord Marrowgar",10,true,3,60,false),
        "<Testguild> killed Lord Marrowgar (10 heroic) this week, 3 attempts in, in 1:00.");
    CHECK_EQ(FormatAnnouncement("<Testguild>","Lord Marrowgar",10,false,0,5,true),
        "<Testguild> killed Lord Marrowgar (10) for the first time, in 0:05.");
}
