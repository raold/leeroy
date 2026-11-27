// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMechanicPolicy.h"
#include "TestHarness.h"
#include <fstream>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <utility>

namespace
{
using namespace Leeroy::Guild;
using uint32 = std::uint32_t;
using uint8 = std::uint8_t;
using TableKey = std::pair<std::uint32_t, std::uint8_t>;
using nlohmann::json;
std::map<TableKey, std::shared_ptr<MechanicTable const>> tables;
std::uint64_t tablesSerial = 0;
struct LeeroyCommandResult { bool ok; std::string error; };
std::function<LeeroyCommandResult(json const&)> handler;
struct TestBridge
{
    std::uint64_t serial = 1;
    std::uint64_t ConnectionSerial() const { return serial; }
};
struct LeeroyModule
{
    TestBridge bridge;
    static LeeroyModule& Instance() { static LeeroyModule module; return module; }
    TestBridge& Bridge() { return bridge; }
};
void RegisterCommand(std::string const& kind, std::function<LeeroyCommandResult(json const&)> command)
{
    CHECK(kind == "raid_mechanics");
    handler = std::move(command);
}
template <class... Args> void LogInfo(Args const&...) { }
#define LOG_INFO(...) LogInfo(__VA_ARGS__)
#include "MechanicTables.inc"
#undef LOG_INFO

json Table(std::uint32_t map, std::uint8_t difficulty)
{
    return {{"v", 1}, {"type", "raid_mechanics"}, {"id", "m1"},
        {"map", map}, {"difficulty", difficulty}, {"mechanics", json::array({
        {{"id", "test.avoid"}, {"kind", "avoid_ground"}, {"area", "dest"},
         {"roles", json::array({"ranged"})}, {"spells", json::array({72110})},
         {"radius", 8}, {"range", 0}, {"angle", 0}, {"duration", 10}}
    })}};
}

void Reset()
{
    tables.clear();
    tablesSerial = 0;
    LeeroyModule::Instance().bridge.serial = 1;
    RegisterMechanicsHandler();
}
}

LEEROY_TEST(MechanicTables_TheRealHelloFeedKeepsBothRubySanctumHeroicTables)
{
    Reset();
    std::ifstream input(std::string(LEEROY_TEST_FIXTURES) + "/mechanic_table_keys.json");
    CHECK(input.good());
    auto const keys = json::parse(input);
    CHECK(keys.size() > 16); // regression: the old capacity passed small per-table tests
    for (auto const& key : keys)
        CHECK(handler(Table(key[0].get<std::uint32_t>(), key[1].get<std::uint8_t>())).ok);
    CHECK_EQ(tables.size(), keys.size());
    CHECK(tables.contains({724, 2}));
    CHECK(tables.contains({724, 3}));
    CHECK_EQ(tables.at({724, 3})->rules.size(), 1u);
}

LEEROY_TEST(MechanicTables_StillBoundedReplaceableAndResetOnANewConnection)
{
    Reset();
    for (std::size_t i = 0; i < kMechanicMaxTables; ++i)
        CHECK(handler(Table(1000 + static_cast<std::uint32_t>(i), 0)).ok);
    CHECK_EQ(tables.size(), kMechanicMaxTables);
    auto const overflow = handler(Table(2000, 0));
    CHECK(!overflow.ok && overflow.error == "too_many_tables");
    CHECK(!tables.contains({2000, 0}));
    CHECK(handler(Table(1000, 0)).ok); // an existing table can still be replaced at capacity
    auto invalid = Table(1000, 0);
    invalid["difficulty"] = 9;
    CHECK(!handler(invalid).ok);
    CHECK_EQ(tables.size(), kMechanicMaxTables);
    ++LeeroyModule::Instance().bridge.serial;
    CHECK(handler(Table(724, 3)).ok);
    CHECK_EQ(tables.size(), 1u);
    CHECK(tables.contains({724, 3}));
}
