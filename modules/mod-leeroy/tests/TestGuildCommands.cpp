// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGuildCommands.h"
#include "LeeroyProtocol.h"
#include "TestHarness.h"
#include <fstream>
#include <limits>
#include <set>
#include <stdexcept>
#include <tuple>

using namespace Leeroy::Guild;
using json = nlohmann::json;

namespace
{
std::vector<json> Fixtures()
{
    std::ifstream file(LEEROY_TEST_FIXTURES "/p1d_protocol.jsonl");
    if (!file)
        throw std::runtime_error("cannot open P1d protocol fixture");
    std::vector<json> rows;
    for (std::string line; std::getline(file, line);)
        if (!line.empty())
            rows.push_back(json::parse(line));
    return rows;
}

json Message(std::string_view name)
{
    for (auto const& row : Fixtures())
        if (row.at("name").get<std::string>() == name)
            return row.at("msg");
    throw std::runtime_error("missing P1d protocol fixture row");
}

bool Parse(std::string_view type, json const& j, std::string& error)
{
    if (type == "form_raid") return ParseFormRaid(j, error).has_value();
    if (type == "summon") return ParseSummon(j, error).has_value();
    if (type == "clear_lockout") return ParseClearLockout(j, error).has_value();
    if (type == "set_loot_master") return ParseSetLootMaster(j, error).has_value();
    if (type == "give_loot") return ParseGiveLoot(j, error).has_value();
    if (type == "start_offline_raid") return ParseOfflineRaid(j, error).has_value();
    if (type == "gear_up") return ParseGearUp(j, error).has_value();
    if (type == "raid_control") return ParseRaidControl(j, error).has_value();
    if (type == "remove_friend") return ParseRemoveFriend(j, error).has_value();
    if (type == "set_spec") return ParseSetSpec(j, error).has_value();
    if (type == "bot_do") return ParseBotDo(j, error).has_value();
    if (type == "stock_items") return ParseStockItems(j, error).has_value();
    throw std::runtime_error("unhandled P1d command type in test");
}

void Reject(json const& msg, char const* field)
{
    std::string error;
    CHECK(!Parse(msg.at("type").get<std::string>(), msg, error));
    CHECK_EQ(error, std::string("bad field: ") + field);
}

void RejectField(std::string_view fixture, char const* field, json bad)
{
    auto msg = Message(fixture);
    msg[field] = std::move(bad);
    Reject(msg, field);
}
}

LEEROY_TEST(Guild_FixtureCommandsParseAsRecorded)
{
    RegisterGuildCommandTypes();
    RegisterGuildCommandTypes();
    std::set<std::string> seen;
    for (auto const& row : Fixtures())
    {
        if (row.at("kind") != "command")
            continue;
        auto msg = row.at("msg");
        auto decoded = Leeroy::DecodeCommand(msg.dump());
        if (!decoded.command)
        {
            CHECK(!row.at("valid").get<bool>());
            CHECK_EQ(decoded.error, row.at("error").get<std::string>());
            continue;
        }
        CHECK(decoded.command->type == Leeroy::CommandType::Extension);
        CHECK_EQ(decoded.command->raw, msg);
        seen.insert(decoded.command->extType);
        std::string error = "previous error";
        bool parsed = Parse(decoded.command->extType, decoded.command->raw, error);
        CHECK_EQ(parsed, row.at("valid").get<bool>());
        CHECK_EQ(error, parsed ? std::string() : row.at("error").get<std::string>());
    }
    CHECK_EQ(seen.size(), kCommandTypes.size());
    for (auto type : kCommandTypes)
        CHECK(seen.count(std::string(type)) == 1);
}

LEEROY_TEST(Guild_RaidDifficulty)
{
    CHECK_EQ(RaidDifficulty(10, false), 0);
    CHECK_EQ(RaidDifficulty(25, false), 1);
    CHECK_EQ(RaidDifficulty(10, true), 2);
    CHECK_EQ(RaidDifficulty(25, true), 3);
}

LEEROY_TEST(Guild_DefaultsFilled)
{
    std::string error;
    auto lockout = ParseClearLockout(Message("clear_lockout_defaults"), error);
    CHECK(lockout.has_value());
    if (lockout) CHECK(lockout->difficulties == std::vector<std::uint8_t>({0, 1, 2, 3}));
    auto gear = ParseGearUp(Message("gear_up_defaults"), error);
    CHECK(gear.has_value());
    if (gear) { CHECK_EQ(gear->ilvl, 264); CHECK_EQ(gear->levelTo, 80); }
    auto raid = ParseOfflineRaid(Message("start_offline_raid_defaults"), error);
    CHECK(raid.has_value());
    if (raid)
    {
        CHECK(!raid->heroic); CHECK(raid->assistants.empty());
        CHECK_EQ(raid->maxWipes, 6); CHECK_EQ(raid->maxWipesPerBoss, 3);
        CHECK_EQ(raid->timeoutMin, 180); CHECK_EQ(raid->minMembers, 2);
    }
    auto control = ParseRaidControl(Message("raid_control_disband"), error);
    CHECK(control.has_value());
    if (control) { CHECK_EQ(control->leaderGuid, 0); CHECK(control->param.empty()); }
    auto summon = ParseSummon(json::parse(R"({"map_id":631,"guids":[1101]})"), error);
    CHECK(summon.has_value());
    if (summon)
    {
        CHECK_EQ(summon->where, "entrance"); CHECK(!summon->summonHuman); CHECK_EQ(summon->summonerGuid, 0);
    }
    auto bot = ParseBotDo(Message("bot_do_cast_target"), error);
    CHECK(bot.has_value());
    if (bot) CHECK_EQ(bot->targetGuid, 1104);
    bot = ParseBotDo(json::parse(R"({"guids":[1101],"action":"do","param":"buff"})"), error);
    CHECK(bot.has_value());
    if (bot) CHECK_EQ(bot->targetGuid, 0);
    auto spec = ParseSetSpec(json::parse(R"({"guid":1101,"spec_no":0})"), error);
    CHECK(spec.has_value());
    if (spec) CHECK(spec->glyphs);
    auto stock = ParseStockItems(json::parse(R"({"guid":1101,"items":[43015]})"), error);
    CHECK(stock.has_value());
    if (stock) CHECK_EQ(stock->count, 5);
    auto loot = ParseSetLootMaster(json::parse(R"({"member_guid":1,"looter_guid":1101})"), error);
    CHECK(loot.has_value());
    if (loot) CHECK_EQ(loot->threshold, 4);
}

LEEROY_TEST(Guild_BotDoWhitelist)
{
    for (auto action : {"co", "nc"})
    {
        for (auto name : kBotStrategyAdd)
            CHECK(BotDoParamAllowed(action, "+" + std::string(name)));
        for (auto name : kBotStrategyRemove)
            CHECK(BotDoParamAllowed(action, "-" + std::string(name)));
        CHECK(BotDoParamAllowed(action, "+behind,-boost,+ss tank"));
        for (auto param : {"", "+", "behind", "+behind,", ",+behind", "+behind,,+boost",
                 "+behind, -boost", "+Behind", "+behind\n", "-tank face", "-mark rti",
                 "-avoid aoe", "+attack anything", "+behind;+boost"})
            CHECK(!BotDoParamAllowed(action, param));
    }
    for (auto param : kRtiIcons) CHECK(BotDoParamAllowed("rti", param));
    for (auto param : kFormations) CHECK(BotDoParamAllowed("formation", param));
    for (auto param : kBotSpells) CHECK(BotDoParamAllowed("cast", param));
    for (auto param : kBotActions) CHECK(BotDoParamAllowed("do", param));
    for (auto item : kStockItems) CHECK(BotDoParamAllowed("use", std::to_string(item)));
    CHECK(!BotDoParamAllowed("cast", "fireball"));
    CHECK(!BotDoParamAllowed("do", "attack"));
    CHECK(!BotDoParamAllowed("rti", "Skull"));
    CHECK(!BotDoParamAllowed("formation", "follow"));
    CHECK(!BotDoParamAllowed("unknown", "buff"));
    for (auto param : {"49623", "043015", "+43015", "43015 ", "43015,46377", "43015\n"})
        CHECK(!BotDoParamAllowed("use", param));
    CHECK(!BotDoParamAllowed("co", std::string("+behind\0,+boost", 15)));
}

LEEROY_TEST(Guild_MakeJobEventMatchesFixture)
{
    JobEvent event{"t1", "form_raid", "done", "", "", {1101, 1102}, {{1103, "in_bg"}}, 0, 0, 0, 0, 0, 0};
    auto full = Message("job_form_raid_done");
    auto body = full;
    body.erase("v"); body.erase("ts");
    CHECK_EQ(MakeJobEvent(event), body);
    CHECK_EQ(json::parse(Leeroy::EncodeEvent(0, MakeJobEvent(event))), full);
    event = {"t11", "bot_do", "done", "", "", {1101}, {{1102, "offline"}}, 0, 0, 0, 0, 0, 0};
    CHECK_EQ(json::parse(Leeroy::EncodeEvent(0, MakeJobEvent(event))), Message("job_bot_do_done"));
    event = {"job", "gear_up", "progress", "", "", {}, {}, 2, 3, 7, 1101, 80, 264};
    auto progress = MakeJobEvent(event);
    CHECK(progress.at("guids").is_array() && progress.at("guids").empty());
    CHECK(progress.at("skipped").is_array() && progress.at("skipped").empty());
    CHECK_EQ(progress.at("kills"), 2); CHECK_EQ(progress.at("wipes"), 3);
    CHECK_EQ(progress.at("instance_id"), 7); CHECK_EQ(progress.at("guid"), 1101);
    CHECK_EQ(progress.at("level"), 80); CHECK_EQ(progress.at("avg_ilvl"), 264);
    CHECK(!progress.contains("v") && !progress.contains("ts"));
}

LEEROY_TEST(Guild_NumericFieldsRequireUnsignedJsonAndDoNotNarrowOverflow)
{
    std::vector<std::pair<std::string_view, char const*>> fields = {
        {"form_raid_ok", "leader_guid"}, {"form_raid_ok", "size"}, {"form_raid_ok", "map_id"},
        {"form_raid_ok", "main_tank_guid"}, {"summon_ok", "map_id"}, {"summon_ok", "summoner_guid"},
        {"clear_lockout_ok", "map_id"}, {"set_loot_master_ok", "member_guid"},
        {"set_loot_master_ok", "looter_guid"}, {"set_loot_master_ok", "threshold"},
        {"give_loot_ok", "map"}, {"give_loot_ok", "instance_id"}, {"give_loot_ok", "source_entry"},
        {"give_loot_ok", "index"}, {"give_loot_ok", "item_id"}, {"give_loot_ok", "to_guid"},
        {"start_offline_raid_ok", "map_id"}, {"start_offline_raid_ok", "size"},
        {"start_offline_raid_ok", "leader_guid"}, {"start_offline_raid_ok", "loot_master_guid"},
        {"start_offline_raid_ok", "max_wipes"}, {"start_offline_raid_ok", "max_wipes_per_boss"},
        {"start_offline_raid_ok", "timeout_min"}, {"start_offline_raid_ok", "min_members"},
        {"gear_up_ok", "ilvl"}, {"gear_up_ok", "level_to"}, {"raid_control_dc_on", "member_guid"},
        {"raid_control_dc_on", "leader_guid"}, {"remove_friend_ok", "guid"},
        {"set_spec_ok", "guid"}, {"set_spec_ok", "spec_no"}, {"bot_do_ok", "target_guid"},
        {"stock_items_ok", "guid"}, {"stock_items_ok", "count"}
    };
    for (auto const& [fixture, field] : fields)
        for (auto const& bad : {json(true), json(false), json(nullptr), json("1"), json(-1), json(1.0),
                 json(json::number_integer_t(1)), json(std::uint64_t(4294967296ULL)),
                 json(std::numeric_limits<std::uint64_t>::max())})
            RejectField(fixture, field, bad);
    std::string error;
    auto largest = ParseRemoveFriend(json::parse(R"({"guid":4294967295})"), error);
    CHECK(largest.has_value());
    if (largest) CHECK_EQ(largest->guid, std::numeric_limits<std::uint32_t>::max());
}

LEEROY_TEST(Guild_ListShapesElementsAndCrossFieldsAreValidated)
{
    for (auto const& [fixture, field] : std::vector<std::pair<std::string_view, char const*>>{
             {"form_raid_ok", "members"}, {"form_raid_ok", "assistants"}, {"summon_ok", "guids"},
             {"clear_lockout_ok", "guids"}, {"clear_lockout_ok", "difficulties"},
             {"start_offline_raid_ok", "members"}, {"start_offline_raid_ok", "assistants"},
             {"gear_up_ok", "guids"}, {"bot_do_ok", "guids"}, {"stock_items_ok", "items"}})
    {
        for (auto const& bad : {json(nullptr), json(true), json("1"), json::object()})
            RejectField(fixture, field, bad);
        for (auto const& bad : {json(true), json(1.0), json(-1), json(nullptr), json("1"),
                 json(std::uint64_t(4294967296ULL))})
            RejectField(fixture, field, json::array({bad}));
        auto duplicate = Message(fixture).at(field).at(0);
        RejectField(fixture, field, json::array({duplicate, duplicate}));
    }
    RejectField("form_raid_ok", "members", json::parse("[1,1101]"));
    RejectField("form_raid_ok", "members", json::array());
    RejectField("form_raid_ok", "assistants", json::parse("[1]"));
    RejectField("form_raid_ok", "assistants", json::parse("[999]"));
    RejectField("start_offline_raid_ok", "members", json::parse("[1101,1102]"));
    RejectField("start_offline_raid_ok", "assistants", json::parse("[1101]"));
    RejectField("start_offline_raid_ok", "min_members", json::parse("11"));
    RejectField("clear_lockout_ok", "difficulties", json::array());
    RejectField("stock_items_ok", "items", json::array());
    for (auto fixture : {"summon_ok", "clear_lockout_ok", "gear_up_ok", "bot_do_ok"})
    {
        RejectField(fixture, "guids", json::array());
        RejectField(fixture, "guids", json::parse("[0]"));
    }
    json many = json::array();
    for (std::uint32_t guid = 100; guid < 141; ++guid) many.push_back(guid);
    RejectField("gear_up_ok", "guids", many);
    RejectField("bot_do_ok", "guids", many);
    many.erase(many.end() - 1);
    auto accepted = Message("gear_up_ok"); accepted["guids"] = many;
    std::string error;
    CHECK(ParseGearUp(accepted, error).has_value());
}

LEEROY_TEST(Guild_BoundsAndRequiredFields)
{
    for (auto const& row : Fixtures())
    {
        if (row.at("kind") != "command" || !row.at("valid").get<bool>() || !row.value("full", false))
            continue;
        auto msg = row.at("msg");
        for (auto const& [field, value] : msg.items())
        {
            if (!field.ends_with("guid") || field == "target_guid" || field == "summoner_guid") continue;
            auto zero = msg; zero[field] = std::uint32_t(0);
            Reject(zero, field.c_str());
        }
    }
    for (auto const& [fixture, field, low, high] : std::vector<std::tuple<std::string_view, char const*, std::uint32_t, std::uint32_t>>{
             {"set_loot_master_ok", "threshold", 2, 5}, {"give_loot_ok", "index", 0, 255},
             {"start_offline_raid_ok", "max_wipes", 1, 20}, {"start_offline_raid_ok", "max_wipes_per_boss", 1, 10},
             {"start_offline_raid_ok", "timeout_min", 10, 360}, {"start_offline_raid_ok", "min_members", 1, 10},
             {"gear_up_ok", "ilvl", 1, 400}, {"gear_up_ok", "level_to", 1, 80},
             {"set_spec_ok", "spec_no", 0, 9}, {"stock_items_ok", "count", 1, 20}})
    {
        for (auto good : {low, high})
        {
            auto msg = Message(fixture); msg[field] = good;
            std::string error;
            CHECK(Parse(msg.at("type").get<std::string>(), msg, error));
        }
        if (low) RejectField(fixture, field, low - 1);
        RejectField(fixture, field, high + 1);
    }
    auto bad = Message("raid_control_dc_on"); bad.erase("leader_guid"); Reject(bad, "leader_guid");
    bad = Message("bot_do_ok"); bad.erase("param"); Reject(bad, "param");
    bad = Message("set_spec_ok"); bad.erase("spec_no"); Reject(bad, "spec_no");
    bad = Message("give_loot_ok"); bad.erase("index"); Reject(bad, "index");
    RejectField("give_loot_ok", "source_entry", 0u);
    RejectField("give_loot_ok", "item_id", 0u);
    // Zero is a real map/instance identifier; world-map legality is the core handler's job.
    auto world = Message("give_loot_ok"); world["map"] = 0u; world["instance_id"] = 0u;
    std::string error;
    CHECK(ParseGiveLoot(world, error).has_value());
}

LEEROY_TEST(Guild_BooleanStringTypesAndFirstErrorOrder)
{
    for (auto const& [fixture, field] : std::vector<std::pair<std::string_view, char const*>>{
             {"form_raid_ok", "heroic"}, {"start_offline_raid_ok", "heroic"},
             {"summon_ok", "summon_human"}, {"set_spec_ok", "glyphs"}})
        for (auto const& bad : {json(nullptr), json("false"), json(std::uint32_t(0)), json(1.0)})
            RejectField(fixture, field, bad);
    for (auto const& [fixture, field] : std::vector<std::pair<std::string_view, char const*>>{
             {"summon_ok", "where"}, {"raid_control_dc_on", "action"}, {"raid_control_dc_on", "param"},
             {"bot_do_ok", "action"}, {"bot_do_ok", "param"}})
        for (auto const& bad : {json(nullptr), json(true), json::array(), json(std::uint32_t(1))})
            RejectField(fixture, field, bad);
    auto msg = Message("form_raid_ok");
    msg["members"] = json::parse("[1101,1101]"); msg["size"] = 20u; msg["heroic"] = "false";
    Reject(msg, "members");
    msg = Message("form_raid_ok"); msg["size"] = 20u; msg["heroic"] = "false";
    Reject(msg, "size");
    msg = Message("bot_do_ok"); msg["action"] = "leave"; msg["param"] = false;
    Reject(msg, "action");
    std::string error;
    CHECK(!ParseGearUp(json::array(), error)); CHECK_EQ(error, "bad field: guids");
    CHECK(!ParseFormRaid(json(nullptr), error)); CHECK_EQ(error, "bad field: leader_guid");
}
