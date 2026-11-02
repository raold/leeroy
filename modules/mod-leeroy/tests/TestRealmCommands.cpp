// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyRealmCommands.h"
#include "LeeroyProtocol.h"
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
    if (type == "realm_emote") return ParseEmote(j, error).has_value();
    if (type == "realm_move") return ParseMove(j, error).has_value();
    if (type == "rpg_status") return ParseRpgStatus(j, error).has_value();
    if (type == "leave_pvp") return ParseLeavePvp(j, error).has_value();
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
    if (type == "realm_job") return MakeRealmJob({"realm-test", "rearm", true, "", 11001, 2});
    if (type == "channel_chat") return MakeChannelChat("trade", 11001, "Test Member", 1637, "Ready for a raid.", true);
    if (type == "pvp_kill") return MakePvpKill(11001, 11002, 571, 4197);
    if (type == "duel_end") return MakeDuelEnd(11001, 11002, "won");
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
            CHECK_EQ(json::parse(Leeroy::EncodeEvent(0, Event(type))), msg);
            continue;
        }
        auto decoded = Leeroy::DecodeCommand(msg.dump());
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
    CHECK_EQ(full.size(), kCommandTypes.size()); CHECK_EQ(events.size(), 12u);
    for (auto type : kCommandTypes) CHECK(full.contains(std::string(type)));
}

LEEROY_TEST(Realm_RegisterTypes)
{
    RegisterRealmCommandTypes(); RegisterRealmCommandTypes();
    for (auto type : kCommandTypes) CHECK(Leeroy::IsExtensionCommand(type));
    CHECK(!Leeroy::RegisterExtensionCommand("rearm"));
}

LEEROY_TEST(Realm_QueuedAndValue)
{
    std::set<std::string_view> queued = {"realm_move", "rpg_status", "leave_pvp", "rearm", "wg_join", "arena_queue", "duel",
        "craft", "equip", "set_profession", "transmog_set", "calendar_signup", "mail_send", "bank_deposit",
        "bank_withdraw", "bank_stock", "guild_grow"};
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
    CHECK_EQ(ParseBankDeposit(Message("bank_deposit_defaults"), error)->count, 0u);
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
            if (key != "dungeon_id")
            { auto changed = msg; changed[key] = json::number_integer_t(1); Reject(changed, key); }
            if (key != "event_id")
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
    std::string error; CHECK(ParseEquip(Message("equip_ok"), error)); CHECK_EQ(kGuildTabard, 5976u);
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

LEEROY_TEST(Realm_HeaderOnlyDistributionsParse)
{
    for (auto const& [file, places] : std::vector<std::pair<std::string, bool>>{
            {"leeroy_places.csv.dist", true}, {"leeroy_items.csv.dist", false}})
    {
        std::ifstream input(std::string(LEEROY_TEST_FIXTURES) + "/../../conf/" + file);
        CHECK(input.is_open());
        std::string csv((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
        std::string error;
        if (places) { auto rows = ParsePlaces(csv, error); CHECK(rows && rows->empty()); }
        else { auto rows = ParseItems(csv, error); CHECK(rows && rows->empty()); }
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
