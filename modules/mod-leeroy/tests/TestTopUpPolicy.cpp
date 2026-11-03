// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyTopUpPolicy.h"
#include "TestHarness.h"
#include <limits>

using namespace Leeroy::Guild;

namespace
{
constexpr std::uint64_t Hour = 3600000;
TopUpState Ready()
{
    TopUpState s;
    s.isCast = s.online = true;
    s.nowMs = 10 * Hour;
    return s;
}
}

LEEROY_TEST(Guild_TopUpFirstPassAndExactCooldownBoundary)
{
    auto s = Ready();
    CHECK(TopUpAllowed(s, Hour));
    s.nowMs = 15 * 60000; // First pass is allowed even on a freshly booted machine.
    CHECK(TopUpAllowed(s, Hour));
    s.nowMs = 10 * Hour;
    s.lastTopUpMs = s.nowMs - Hour + 1;
    CHECK(!TopUpAllowed(s, Hour));
    --s.lastTopUpMs;
    CHECK(TopUpAllowed(s, Hour));
}

LEEROY_TEST(Guild_TopUpNeverUnderflowsClockOrStockCounts)
{
    auto s = Ready();
    s.lastTopUpMs = s.nowMs + 1;
    CHECK(!TopUpAllowed(s, Hour));
    CHECK_EQ(TopUpMissing(0, 20), 20u);
    CHECK_EQ(TopUpMissing(19, 20), 1u);
    CHECK_EQ(TopUpMissing(20, 20), 0u);
    CHECK_EQ(TopUpMissing(std::numeric_limits<std::uint32_t>::max(), 20), 0u);
}

LEEROY_TEST(Guild_TopUpRefusesEveryUnsafeState)
{
    auto s = Ready();
    s.isCast = false;
    CHECK(!TopUpAllowed(s, Hour));
    s = Ready();
    s.online = false;
    CHECK(!TopUpAllowed(s, Hour));
    for (auto field : {&TopUpState::inCombat, &TopUpState::dead, &TopUpState::grouped,
        &TopUpState::onInstanceMap, &TopUpState::inBattleground, &TopUpState::teleporting,
        &TopUpState::inFlight, &TopUpState::trading})
    {
        s = Ready();
        s.*field = true;
        CHECK(!TopUpAllowed(s, Hour));
    }
}

LEEROY_TEST(Guild_TopUpRoundRobinNormalizesInputAndStartsAfterLastHandled)
{
    CHECK(TopUpOrder({}, 10).empty());
    CHECK(TopUpOrder({0, 0}, 0).empty());
    CHECK(TopUpOrder({30, 10, 20, 10, 0}, 10) == std::vector<std::uint32_t>({20, 30, 10}));
    CHECK(TopUpOrder({30, 10, 20}, 25) == std::vector<std::uint32_t>({30, 10, 20}));
    CHECK(TopUpOrder({30, 10, 20}, 30) == std::vector<std::uint32_t>({10, 20, 30}));
}

LEEROY_TEST(Guild_TopUpRoundRobinCoversMoreThanOneFiveMemberBatch)
{
    std::vector<std::uint32_t> members = {101, 102, 103, 104, 105, 106, 107};
    auto first = TopUpOrder(members, 0);
    auto second = TopUpOrder(members, first[4]);
    CHECK_EQ(second[0], 106u);
    CHECK_EQ(second[1], 107u);
    CHECK_EQ(second[2], 101u);
    auto next = TopUpOrder({101, 102, 103, 104, 108}, 107); // Roster changed since the last pass.
    CHECK_EQ(next[0], 108u);
}
