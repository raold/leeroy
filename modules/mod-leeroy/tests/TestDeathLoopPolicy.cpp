// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyDeathLoopPolicy.h"
#include "TestHarness.h"
#include <array>
using namespace Leeroy::Guild;
namespace
{
DeathLoopState Ready() { DeathLoopState s; s.cast = s.pinned = s.online = true; return s; }
void Death(DeathLoopHistory& h, std::uint64_t now)
{
    auto s = Ready();
    ObserveDeathLoop(h, s, now);
    s.dead = true;
    ObserveDeathLoop(h, s, now);
}
DeathLoopHistory Three()
{
    DeathLoopHistory h;
    Death(h, 0); Death(h, 100); Death(h, 200);
    return h;
}
}
LEEROY_TEST(DeathLoop_CountsEdgesNotDeadTicksAndWaitsForAliveOutOfCombat)
{
    DeathLoopHistory h;
    auto s = Ready();
    s.dead = true;
    for (unsigned i = 0; i < 100; ++i) ObserveDeathLoop(h, s, i);
    CHECK_EQ(h.deaths.size(), 1u);
    Death(h, 100); Death(h, 200);
    unsigned attempts = 0;
    CHECK_EQ(ClaimDeathLoopRescue(h, s, 201, attempts), 0u);
    s.dead = false; s.combat = true;
    CHECK_EQ(ClaimDeathLoopRescue(h, s, 202, attempts), 0u);
    s.combat = false;
    CHECK_EQ(ClaimDeathLoopRescue(h, s, 203, attempts), 3u);
    CHECK_EQ(attempts, 1u);
}
LEEROY_TEST(DeathLoop_FifteenMinuteWindowIncludesBoundaryAndExpiresAfter)
{
    auto h = Three(); unsigned attempts = 0;
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), kDeathWindowMs, attempts), 3u);
    h = Three(); attempts = 0;
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), kDeathWindowMs + 1, attempts), 0u);
    CHECK_EQ(h.deaths.size(), 2u);
}
LEEROY_TEST(DeathLoop_AllFencesDiscardObservationsAndPreventRescue)
{
    for (auto field : {&DeathLoopState::cast, &DeathLoopState::pinned, &DeathLoopState::online,
        &DeathLoopState::grouped, &DeathLoopState::instance, &DeathLoopState::battleground,
        &DeathLoopState::owned, &DeathLoopState::transit, &DeathLoopState::trading, &DeathLoopState::duel})
    {
        auto h = Three(); auto s = Ready(); unsigned attempts = 0;
        s.*field = !(s.*field);
        CHECK_EQ(ClaimDeathLoopRescue(h, s, 300, attempts), 0u);
        s.dead = true;
        ObserveDeathLoop(h, s, 300);
        CHECK(h.deaths.empty());
        s = Ready(); s.dead = true;
        ObserveDeathLoop(h, s, 301);
        CHECK(h.deaths.empty()); // Same corpse, now unblocked, is not another death.
    }
}
LEEROY_TEST(DeathLoop_CooldownBoundsFailuresAndHandlesClockRegression)
{
    auto h = Three(); unsigned attempts = 0;
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), 200, attempts), 3u);
    Death(h, kDeathRescueCooldownMs - 100);
    Death(h, kDeathRescueCooldownMs);
    Death(h, kDeathRescueCooldownMs + 100);
    attempts = 0;
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), kDeathRescueCooldownMs + 199, attempts), 0u);
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), kDeathRescueCooldownMs + 200, attempts), 3u);
    h = Three(); h.lastAttempt = 500;
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), 300, attempts), 0u);
}
LEEROY_TEST(DeathLoop_FivePerPassThenNextPassCanRescueTheRemainingMember)
{
    std::array<DeathLoopHistory, 6> histories;
    unsigned attempted = 0;
    for (auto& h : histories) { h = Three(); ClaimDeathLoopRescue(h, Ready(), 300, attempted); }
    CHECK_EQ(attempted, 5u);
    CHECK_EQ(histories.back().deaths.size(), 3u);
    attempted = 0;
    CHECK_EQ(ClaimDeathLoopRescue(histories.back(), Ready(), 301, attempted), 3u);
}
