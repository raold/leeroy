// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGuildCommands.h"
#include "LeeroyProtocol.h"
#include "TestHarness.h"
#include <limits>

using namespace Leeroy::Guild;
using json = nlohmann::json;

namespace
{
// Use wire JSON independently of the golden fixture; positive wire integers have
// unsigned storage, unlike signed C++ integer literals inserted into a json value.
json Raid()
{
    return json::parse(R"({"leader_guid":1,"members":[1101,1102],"size":10,"map_id":631,"main_tank_guid":1101})");
}

json Offline()
{
    return json::parse(R"({"map_id":631,"size":25,"leader_guid":1101,"members":[1102],"loot_master_guid":1101})");
}

template <typename Parser>
void Reject(Parser parser, json const& input, char const* field)
{
    std::string error = "stale error";
    CHECK(!parser(input, error).has_value());
    CHECK_EQ(error, std::string("bad field: ") + field);
}
}

LEEROY_TEST(Guild_Review_FormRaidReportsFirstInvalidFieldAcrossDependencies)
{
    auto input = Raid();
    input["members"] = json::parse("[1101,1101]");
    input["size"] = false;
    input["heroic"] = "false";
    input["leader_guid"] = json::parse("4294967296");
    Reject(ParseFormRaid, input, "leader_guid");
    input["leader_guid"] = 1u;
    Reject(ParseFormRaid, input, "members");
    input["members"] = json::parse("[1101]");
    Reject(ParseFormRaid, input, "size");
    input["size"] = 10u;
    Reject(ParseFormRaid, input, "heroic");
    input["heroic"] = false;
    std::string error = "stale error";
    CHECK(ParseFormRaid(input, error).has_value());
    CHECK(error.empty());
}

LEEROY_TEST(Guild_Review_MembersCapacityWinsBeforeLaterInvalidFields)
{
    auto input = Raid();
    input["members"] = json::array();
    for (std::uint32_t guid = 1101; guid <= 1110; ++guid)
        input["members"].push_back(guid);
    input["heroic"] = nullptr;
    input["map_id"] = "631";
    Reject(ParseFormRaid, input, "members");
    input["size"] = 25u;
    Reject(ParseFormRaid, input, "heroic");
}

LEEROY_TEST(Guild_Review_OfflineUsesDeclaredOrderRatherThanJsonKeyOrder)
{
    auto input = Offline();
    input["assistants"] = json::parse("[999]");
    input["heroic"] = 0u;
    input["map_id"] = true;
    Reject(ParseOfflineRaid, input, "map_id");
    input["map_id"] = 631u;
    Reject(ParseOfflineRaid, input, "heroic");
    input["heroic"] = false;
    Reject(ParseOfflineRaid, input, "assistants");
}

LEEROY_TEST(Guild_Review_EnvelopePreservesUnsignedOverflowForFieldRejection)
{
    RegisterGuildCommandTypes();
    auto decoded = Leeroy::DecodeCommand(
        R"({"v":1,"type":"bot_do","ts":0,"id":"overflow","guids":[4294967297],"action":"do","param":"buff"})");
    CHECK(decoded.command.has_value());
    if (!decoded.command)
        return;
    CHECK(decoded.command->type == Leeroy::CommandType::Extension);
    CHECK_EQ(decoded.command->raw.at("guids").at(0).get<std::uint64_t>(), 4294967297ULL);
    Reject(ParseBotDo, decoded.command->raw, "guids");

    auto input = json::parse(R"({"guids":[4294967295],"action":"cast","param":"rebirth","target_guid":4294967295})");
    std::string error;
    auto accepted = ParseBotDo(input, error);
    CHECK(accepted.has_value());
    if (accepted)
    {
        CHECK_EQ(accepted->guids.at(0), std::numeric_limits<std::uint32_t>::max());
        CHECK_EQ(accepted->targetGuid, std::numeric_limits<std::uint32_t>::max());
    }
    input["target_guid"] = json::parse("18446744073709551615");
    Reject(ParseBotDo, input, "target_guid");
}

LEEROY_TEST(Guild_Review_BoolsFloatsAndNegativeZeroAreNotUnsignedIntegers)
{
    for (auto text : {R"({"guid":true})", R"({"guid":1.0})", R"({"guid":1e0})",
             R"({"guid":-0})", R"({"guid":-1})"})
        Reject(ParseRemoveFriend, json::parse(text), "guid");
    for (auto text : {R"({"map_id":631,"guids":[true]})",
             R"({"map_id":631,"guids":[1101.0]})"})
        Reject(ParseSummon, json::parse(text), "guids");
    Reject(ParseClearLockout, json::parse(R"({"map_id":631,"guids":[1101],"difficulties":[false]})"), "difficulties");
    Reject(ParseClearLockout, json::parse(R"({"map_id":631,"guids":[1101],"difficulties":[0.0]})"), "difficulties");
}

LEEROY_TEST(Guild_Review_MixedStrategyListCannotHideSurvivalRemoval)
{
    for (auto action : {"co", "nc"})
    {
        auto input = json::parse(R"({"guids":[1101],"action":"co","param":"+behind,-boost,+ss healer"})");
        input["action"] = action;
        std::string error;
        CHECK(ParseBotDo(input, error).has_value());
        for (auto param : {"+behind,-avoid aoe,+boost", "-avoid aoe,+behind",
                 "+behind,-avoid aoe", "+behind,-tank face,+boost",
                 "+behind,-mark rti,+ss healer", "+behind,+attack anything,+boost"})
        {
            input["param"] = param;
            Reject(ParseBotDo, input, "param");
        }
    }
}

LEEROY_TEST(Guild_Review_WhitelistRequiresWholeTokenIncludingHiddenSuffix)
{
    auto input = json::parse(R"({"guids":[1101],"action":"use","param":"43015"})");
    input["param"] = std::string("43015\0", 6);
    Reject(ParseBotDo, input, "param");
    input["action"] = "co";
    input["param"] = std::string("+behind\0,-avoid aoe", 19);
    Reject(ParseBotDo, input, "param");
    input["param"] = "+behind,\r\n-avoid aoe";
    Reject(ParseBotDo, input, "param");
    input["param"] = "+behind,";
    Reject(ParseBotDo, input, "param");
}

LEEROY_TEST(Guild_Review_OfflineLootMustBeLeaderOrRequestedMember)
{
    auto input = Offline();
    std::string error;
    CHECK(ParseOfflineRaid(input, error).has_value());
    input["loot_master_guid"] = 1102u;
    CHECK(ParseOfflineRaid(input, error).has_value());
    input["loot_master_guid"] = 1103u;
    Reject(ParseOfflineRaid, input, "loot_master_guid");
    input["assistants"] = json::parse("[1103]");
    Reject(ParseOfflineRaid, input, "assistants");
    input["assistants"] = json::array();
    input["loot_master_guid"] = 0u;
    Reject(ParseOfflineRaid, input, "loot_master_guid");
}

LEEROY_TEST(Guild_Review_OfflineEmptyOptionalListIsNotMissingRequiredList)
{
    auto input = Offline();
    input["assistants"] = json::array();
    std::string error = "old failure";
    auto accepted = ParseOfflineRaid(input, error);
    CHECK(accepted.has_value());
    CHECK(error.empty());
    if (accepted)
    {
        CHECK(accepted->assistants.empty());
        CHECK(!accepted->heroic);
        CHECK_EQ(accepted->minMembers, 2);
        CHECK_EQ(accepted->maxWipes, 6);
        CHECK_EQ(accepted->maxWipesPerBoss, 3);
        CHECK_EQ(accepted->timeoutMin, 180);
    }
    input["assistants"] = nullptr;
    Reject(ParseOfflineRaid, input, "assistants");
    input.erase("assistants");
    input["members"] = json::array();
    Reject(ParseOfflineRaid, input, "members");
    input.erase("members");
    Reject(ParseOfflineRaid, input, "members");
}

LEEROY_TEST(Guild_Review_OptionalDefaultsDoNotReplaceExplicitInvalidValues)
{
    auto input = json::parse(R"({"guid":1101,"items":[43015]})");
    std::string error;
    auto stock = ParseStockItems(input, error);
    CHECK(stock.has_value());
    if (stock) CHECK_EQ(stock->count, 5);
    input["count"] = nullptr;
    Reject(ParseStockItems, input, "count");
    input["count"] = 0u;
    Reject(ParseStockItems, input, "count");
    input = json::parse(R"({"member_guid":1,"action":"disband"})");
    auto control = ParseRaidControl(input, error);
    CHECK(control.has_value());
    if (control) CHECK_EQ(control->leaderGuid, 0);
    input["leader_guid"] = nullptr;
    Reject(ParseRaidControl, input, "leader_guid");
}

LEEROY_TEST(Guild_Review_JobSerializationKeepsAllFieldsAndEscapesWireText)
{
    JobEvent event;
    event.id = "review\"\njob";
    event.kind = "offline_raid";
    event.state = "failed";
    event.reason = "setup:starting:bad\r\nreason";
    event.stage = "starting";
    event.guids = {std::numeric_limits<std::uint32_t>::max()};
    event.skipped = {{1102, "offline\tmember"}};
    event.kills = 7;
    event.wipes = 9;
    event.instanceId = std::numeric_limits<std::uint32_t>::max();
    auto body = MakeJobEvent(event);
    CHECK_EQ(body.size(), 14u);
    CHECK(!body.contains("v") && !body.contains("ts"));
    CHECK_EQ(body.at("guid"), 0);
    CHECK_EQ(body.at("level"), 0);
    CHECK_EQ(body.at("avg_ilvl"), 0);
    CHECK(body.at("instance_id").is_number_unsigned());
    auto line = Leeroy::EncodeEvent(1700000000000LL, body);
    CHECK(line.find('\n') == std::string::npos && line.find('\r') == std::string::npos);
    auto decoded = json::parse(line);
    CHECK_EQ(decoded.at("id"), event.id);
    CHECK_EQ(decoded.at("reason"), event.reason);
    CHECK_EQ(decoded.at("skipped").at(0).at("reason"), event.skipped.at(0).second);
    CHECK_EQ(decoded.at("v"), 1);
    CHECK_EQ(decoded.at("ts"), 1700000000000LL);
    decoded.erase("v");
    decoded.erase("ts");
    CHECK_EQ(decoded, body);
}
