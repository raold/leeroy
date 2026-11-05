// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingProtocol.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyProtocol.h"
#include "TestHarness.h"
#include <fstream>
#include <limits>
#include <set>
#include <stdexcept>

using namespace Leeroy::Training;
using json = nlohmann::json;

namespace
{
std::vector<json> Fixtures()
{
    std::ifstream file(LEEROY_TEST_FIXTURES "/training_protocol.jsonl");
    if (!file)
        throw std::runtime_error("cannot open training protocol fixture");
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
    throw std::runtime_error("missing training fixture row");
}

bool Parse(std::string_view type, json const& j, std::string& error)
{
    if (type == "telemetry") return ParseTelemetry(j, error).has_value();
    if (type == "get_stats") return ParseGetStats(j, error).has_value();
    if (type == "get_training_cleanup") return ParseGetTrainingCleanup(j, error).has_value();
    if (type == "get_vocab") return ParseGetVocab(j, error).has_value();
    if (type == "custom_strategy") return ParseCustomStrategy(j, error).has_value();
    if (type == "dc_setting") return ParseDcSetting(j, error).has_value();
    if (type == "dummy_trial") return ParseDummyTrial(j, error).has_value();
    if (type == "get_rivals") return ParseGetRivals(j, error).has_value();
    if (type == "start_benchmark") return ParseStartBenchmark(j, error).has_value();
    if (type == "bot_do") return Leeroy::Guild::ParseBotDo(j, error).has_value();
    throw std::runtime_error("unhandled training command");
}

void Reject(std::string_view name, char const* field, json value)
{
    auto msg = Message(name);
    msg[field] = std::move(value);
    std::string error;
    CHECK(!Parse(msg.at("type").get<std::string>(), msg, error));
    CHECK_EQ(error, std::string("bad field: ") + field);
}
}

LEEROY_TEST(Training_FixtureCommandsParseAsRecorded)
{
    RegisterTrainingCommandTypes();
    RegisterTrainingCommandTypes();
    Leeroy::Guild::RegisterGuildCommandTypes();
    std::set<std::string> seen;
    for (auto const& row : Fixtures())
    {
        if (row.at("kind") != "command")
            continue;
        auto const& msg = row.at("msg");
        auto decoded = Leeroy::DecodeCommand(msg.dump());
        CHECK(decoded.command.has_value());
        if (!decoded.command)
            continue;
        CHECK(decoded.command->type == Leeroy::CommandType::Extension);
        CHECK(decoded.command->raw == msg);
        seen.insert(decoded.command->extType);
        std::string error = "previous error";
        bool parsed = Parse(decoded.command->extType, msg, error);
        CHECK_EQ(parsed, row.at("valid").get<bool>());
        CHECK_EQ(error, parsed ? std::string() : row.at("error").get<std::string>());
    }
    CHECK_EQ(seen.size(), kTrainingCommandTypes.size() + 1);
    for (auto type : kTrainingCommandTypes)
        CHECK(seen.count(std::string(type)) == 1);
    CHECK(!Leeroy::RegisterExtensionCommand("say"));
}

LEEROY_TEST(Training_Defaults)
{
    std::string error;
    auto telemetry = ParseTelemetry(json::parse(R"({"record":false})"), error);
    CHECK(telemetry && telemetry->watch.empty() && !telemetry->record);
    auto dc = ParseDcSetting(Message("dc_setting_clear"), error);
    CHECK(dc && dc->key.empty() && dc->centi == 0 && dc->op == DcOp::Clear);
    auto rivals = ParseGetRivals(json::object(), error);
    CHECK(rivals && rivals->level == 80 && rivals->max == 200);
    auto custom = Message("custom_strategy_ok");
    custom.erase("probe");
    auto strategy = ParseCustomStrategy(custom, error);
    CHECK(strategy && strategy->probe == 0);
    auto vocab = ParseGetVocab(json::parse(R"({"classes":[6]})"), error);
    CHECK(vocab && vocab->probes.empty() && vocab->triggers.empty());
    auto benchmark = ParseStartBenchmark(json::parse(
        R"({"map_id":631,"size":10,"leader_guid":5001,"members":[5002],"bosses":[0]})"), error);
    CHECK(benchmark.has_value());
    if (benchmark)
    {
        CHECK(!benchmark->heroic);
        CHECK_EQ(benchmark->maxWipes, 6);
        CHECK_EQ(benchmark->maxWipesPerBoss, 3);
        CHECK_EQ(benchmark->timeoutMin, 180);
        CHECK_EQ(benchmark->minMembers, 2);
    }
    Reject("start_benchmark_ok", "min_members", 0u);
}

LEEROY_TEST(Training_CustomLineGrammar)
{
    for (auto text : {"often>frost strike!7", "killing machine>frost strike!9.5", "high mana>arcane blast!5",
        "a>b!5.0", "a>b!9.9"})
        CHECK(CustomLineValid(text));
    for (auto text : {"often>frost strike!4.9", "a>b!10", "a>b!19.9", "a>b", ">b!7", "a>a,b!7",
        "Often>b!7", "a>b!7;", "a>b!", "a>b!7.00", "a>b!+7", "a>b!7\n", "a>b'!7", "a>>b!7"})
        CHECK(!CustomLineValid(text));
    CHECK(!CustomLineValid(std::string(121, 'a')));
    CHECK(!CustomLineValid(std::string(41, 'a') + ">b!7"));
    CHECK(!CustomLineValid("a>" + std::string(41, 'b') + "!7"));
}

LEEROY_TEST(Training_CacheEntryFormatCannotEscapeLiteral)
{
    CHECK_EQ(FormatCacheEntry("tg_x_v1", {"often>a!6", "b>c!7"}),
        "(nullptr, 'tg_x_v1', 'often>a!6'),\n(nullptr, 'tg_x_v1', 'b>c!7');");
    for (auto const& name : {"tg_x'", "tg_x\\", "tg_", "other", "tg_X"})
    {
        bool rejected = false;
        try { FormatCacheEntry(name, {"a>b!6"}); }
        catch (std::invalid_argument const&) { rejected = true; }
        CHECK(rejected);
    }
    bool rejected = false;
    try { FormatCacheEntry("tg_safe", {"a>b'!6"}); }
    catch (std::invalid_argument const&) { rejected = true; }
    CHECK(rejected);
}

LEEROY_TEST(Training_BotDoTierTwoPreservesSurvivalRules)
{
    using Leeroy::Guild::BotDoParamAllowed;
    for (auto text : {"+threat,+dps aoe", "+wait for attack", "+focus heal targets", "-save mana",
        "+custom::tg_dk_frost_v1", "-custom::tg_dk_frost_v1"})
        CHECK(BotDoParamAllowed("co", text));
    for (auto text : {"+save mana", "-threat", "-dps aoe", "+custom::frost", "-avoid aoe"})
        CHECK(!BotDoParamAllowed("co", text));
    for (auto text : {"focus heal targets 1101,1102,1103", "mana save level 2", "formation circle",
        "wait for attack time 0", "wait for attack time 10"})
        CHECK(BotDoParamAllowed("value", text));
    for (auto text : {"focus heal targets 1,2,3,4,5,6", "focus heal targets 1,1", "focus heal targets 0",
        "focus heal targets 4294967296", "focus heal targets 1,01", "mana save level 3", "mana save level 0",
        "formation diamond", "wait for attack time -1", "wait for attack time 10x", "wait for attack time 11"})
        CHECK(!BotDoParamAllowed("value", text));
    for (auto text : {"dps aoe", "custom::tg_dk_frost_v1", "formation", "*", "mana save level"})
        CHECK(BotDoParamAllowed("restore", text));
    CHECK(!BotDoParamAllowed("restore", "boost"));
    CHECK(!BotDoParamAllowed("restore", "avoid aoe"));
    CHECK(!BotDoParamAllowed("do", "value"));
    CHECK(!BotDoParamAllowed("do", "restore"));
    CHECK(!IsTierTwo("co", "+behind"));
    CHECK(IsTierTwo("co", "+behind,+threat"));
    CHECK(IsTierTwo("nc", "-save mana"));
    CHECK(IsTierTwo("value", "mana save level 2"));
    CHECK(IsTierTwo("restore", "threat"));
}

LEEROY_TEST(Training_SwitchOffGateAllowsOnlyScopedUndo)
{
    CHECK(!TierTwoAllowed(false, "co", "+threat"));
    CHECK(!TierTwoAllowed(false, "value", "mana save level 2"));
    CHECK(TierTwoAllowed(false, "restore", "dps aoe"));
    CHECK(TierTwoAllowed(false, "restore", "*"));
    CHECK(TierTwoAllowed(false, "co", "-custom::tg_a_v1,-custom::tg_b_v2"));
    CHECK(!TierTwoAllowed(false, "co", "-custom::tg_a_v1,+threat"));
    CHECK(!TierTwoAllowed(false, "restore", "avoid aoe"));
    CHECK(!TierTwoAllowed(true, "co", "-avoid aoe"));
    CHECK(TierTwoAllowed(true, "co", "+threat"));
}

LEEROY_TEST(Training_StrictNumericFieldsAndBounds)
{
    for (json bad : {json(true), json(1.0), json(-1), json("1"), json(nullptr),
        json(std::numeric_limits<std::uint64_t>::max())})
    {
        Reject("dc_setting_set", "leader_guid", bad);
        Reject("get_rivals_ok", "level", bad);
        Reject("start_benchmark_ok", "min_members", bad);
    }
    Reject("telemetry_on", "watch", json::array({1u, 1u}));
    Reject("get_vocab_ok", "classes", json::array({6u, 6u}));
    Reject("dummy_trial_ok", "guids", json::array({1101u, 1101u}));
    Reject("start_benchmark_ok", "bosses", json::array({0u, 0u}));
    Reject("start_benchmark_ok", "members", json::array({5001u}));
    Reject("start_benchmark_ok", "size", 11u);
    Reject("start_benchmark_ok", "heroic", 1u);
    Reject("dc_setting_set", "centi", std::uint64_t(2147483648));
    Reject("dc_setting_set", "centi", std::int64_t(-2147483649));
    Reject("dc_setting_set", "centi", true);
    Reject("dc_setting_set", "centi", 100.0);
    std::string error;
    auto dc = Message("dc_setting_set");
    dc["leader_guid"] = std::numeric_limits<std::uint32_t>::max();
    dc["centi"] = std::numeric_limits<std::int32_t>::min();
    CHECK(ParseDcSetting(dc, error).has_value());
    for (auto value : {50u, 150u})
    {
        dc = Message("dc_setting_scale");
        dc["centi"] = value;
        CHECK(ParseDcSetting(dc, error).has_value());
    }
}

LEEROY_TEST(Training_ListLimitsAndFirstFailure)
{
    auto watch = json::array();
    for (unsigned i = 1; i <= 33; ++i)
        watch.push_back(i);
    Reject("telemetry_on", "watch", watch);
    auto lines = json::array();
    for (unsigned i = 0; i != 13; ++i)
        lines.push_back("a>b!7");
    Reject("custom_strategy_ok", "lines", lines);
    Reject("get_vocab_ok", "triggers", json::array({std::string(41, 'a')}));
    std::string error;
    auto telemetry = Message("telemetry_on");
    telemetry["watch"] = json::array();
    CHECK(ParseTelemetry(telemetry, error).has_value());
    telemetry["record"] = 1u;
    telemetry["watch"] = json::array({0u});
    CHECK(!ParseTelemetry(telemetry, error));
    CHECK_EQ(error, "bad field: record");
    auto onlyProbe = ParseGetVocab(json::parse(R"({"probes":[4294967295]})"), error);
    CHECK(onlyProbe && onlyProbe->classes.empty());
}
