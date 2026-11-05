// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingProtocol.h"
#include "TestHarness.h"
#include <limits>

using namespace Leeroy::Training;

LEEROY_TEST(benchmark_rival_requires_each_independent_body_safety_gate)
{
    RivalState base{true, true, true, false, false, false, false, false, false, false, false, 80, 1};
    CHECK(RivalUsable(base, 80));
    for (auto flag : {&RivalState::online, &RivalState::isBot, &RivalState::isRandomBot})
    {
        auto state = base;
        state.*flag = false;
        CHECK(!RivalUsable(state, 80));
    }
    for (auto flag : {&RivalState::isCast, &RivalState::isHuman, &RivalState::grouped,
        &RivalState::inInstance, &RivalState::inBattleground, &RivalState::inLfg,
        &RivalState::dead, &RivalState::inFlight})
    {
        auto state = base;
        state.*flag = true;
        CHECK(!RivalUsable(state, 80));
    }
    base.level = 79;
    CHECK(!RivalUsable(base, 80));
    base.level = 80;
    base.team = 0;
    CHECK(RivalUsable(base, 80)); // Faction agreement is a roster admission check, not a Horde-only filter.
    CHECK(!RivalUsable(base, 256)); // Never narrow the requested level before comparison.
}

LEEROY_TEST(benchmark_frontier_only_observes_unapproved_active_fights)
{
    CHECK(FrontierReached({3, 3, 1}, {0, 1}));
    CHECK(!FrontierReached({3, 1}, {0, 1}));
    CHECK(!FrontierReached({0, 0, 0}, {0}));
    CHECK(!FrontierReached({3, 3, 2}, {0, 1}));
    CHECK(!FrontierReached({3, 3, 3}, {0, 1}));
    CHECK(FrontierReached({1}, {}));
    CHECK(!FrontierReached({}, {}));
    CHECK(FrontierReached({1}, {std::numeric_limits<std::uint32_t>::max()}));
    CHECK(!FrontierReached({1, 0, 1}, {2, 0, 2}));
}
