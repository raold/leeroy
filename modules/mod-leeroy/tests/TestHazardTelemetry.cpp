// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TestHarness.h"
#include <algorithm>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <unordered_map>
#include <vector>

namespace
{
using uint32 = std::uint32_t;
using MapKey = std::pair<uint32, uint32>;
enum { UNITHOOK_ON_DAMAGE = 1, CHEAT_GOD = 2 };
struct Guid
{
    uint32 value;
    uint32 GetCounter() const { return value; }
};
struct Unit
{
    uint32 entry = 0, guid = 17, map = 533, instance = 1;
    bool creature = false, player = false, god = false;
    bool IsCreature() const { return creature; }
    bool IsPlayer() const { return player; }
    uint32 GetEntry() const { return entry; }
    Guid GetGUID() const { return {guid}; }
    uint32 GetMapId() const { return map; }
    uint32 GetInstanceId() const { return instance; }
    Unit* ToPlayer() { return player ? this : nullptr; }
    bool GetCommandStatus(int flag) const { CHECK_EQ(flag, CHEAT_GOD); return god; }
};
struct UnitScript
{
    UnitScript(char const*, bool active, std::vector<int> hooks)
    {
        CHECK(active);
        CHECK(hooks == std::vector<int>{UNITHOOK_ON_DAMAGE});
    }
    virtual void OnDamage(Unit*, Unit*, uint32&) { }
};
struct Snapshot { std::unordered_map<uint32, MapKey> aware; };
std::shared_ptr<Snapshot const> snapshot;
std::mutex snapshotLock;

#include "HazardTelemetry.inc"

void ResetPoison()
{
    poisonWindows.clear();
    poisonOverflow = 0;
    auto current = std::make_shared<Snapshot>();
    current->aware.emplace(17, MapKey{533, 1});
    snapshot = current;
}
}

LEEROY_TEST(HazardTelemetry_CountsOnlyPositivePoisonEventsForTheObservedBotInstance)
{
    ResetPoison();
    HazardDamage hook;
    Unit poison; poison.creature = true; poison.entry = 16027;
    Unit bot; bot.player = true;
    uint32 damage = 100;
    hook.OnDamage(&poison, &bot, damage);
    CHECK_EQ(poisonWindows.at({533, 1}).hitEvents, 1u);
    CHECK_EQ(damage, 100u); // observation never modifies damage
    damage = 0;
    hook.OnDamage(&poison, &bot, damage);
    damage = 100;
    hook.OnDamage(nullptr, &bot, damage);
    hook.OnDamage(&poison, nullptr, damage);
    poison.entry = 1; hook.OnDamage(&poison, &bot, damage); poison.entry = 16027;
    bot.player = false; hook.OnDamage(&poison, &bot, damage); bot.player = true;
    bot.god = true; hook.OnDamage(&poison, &bot, damage); bot.god = false;
    bot.guid = 18; hook.OnDamage(&poison, &bot, damage); bot.guid = 17;
    bot.instance = 2; hook.OnDamage(&poison, &bot, damage); bot.instance = 1;
    bot.map = 603; hook.OnDamage(&poison, &bot, damage); bot.map = 533;
    snapshot.reset(); hook.OnDamage(&poison, &bot, damage);
    CHECK_EQ(poisonWindows.size(), 1u);
    CHECK_EQ(poisonWindows.at({533, 1}).hitEvents, 1u);
}

LEEROY_TEST(HazardTelemetry_BoundsInstancesWithoutSilentlyDroppingUpdates)
{
    ResetPoison();
    for (uint32 instance = 1; instance <= kPoisonWindows; ++instance)
        CHECK(PoisonCounter({533, instance}) != nullptr);
    CHECK(PoisonCounter({533, 65}) == nullptr);
    CHECK_EQ(poisonOverflow, 1u);
    CHECK_EQ(poisonWindows.size(), kPoisonWindows);
    CHECK(PoisonCounter({533, 1}) != nullptr); // existing counters still work at capacity
    CHECK_EQ(poisonOverflow, 1u);
}

LEEROY_TEST(HazardTelemetry_AggregatesRouteFailuresWithoutCountingOtherMapsOrEmptyCalls)
{
    ResetPoison();
    NotePoisonRoutes({533, 1}, {});
    NotePoisonRoutes({603, 1}, {1, 1});
    CHECK(poisonWindows.empty());
    NotePoisonRoutes({533, 1}, {6, 1, 2, 1, 3, 1, 8, 500});
    NotePoisonRoutes({533, 1}, {2, 1, 0, 1, 0, 0, 4, 250});
    auto const& total = poisonWindows.at({533, 1});
    CHECK_EQ(total.hitEvents, 0u);
    CHECK_EQ(total.routes.attempts, 8u);
    CHECK_EQ(total.routes.pathRejected, 2u);
    CHECK_EQ(total.routes.endRejected, 2u);
    CHECK_EQ(total.routes.collisionRejected, 2u);
    CHECK_EQ(total.routes.budgetRejected, 3u);
    CHECK_EQ(total.routes.waits, 1u);
    CHECK_EQ(total.routes.rejectedTypes, 12u);
    CHECK_EQ(total.routes.maxHeightErrorMm, 500u);
}
