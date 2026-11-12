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
    DeathRescueBudget attempts;
    CHECK_EQ(ClaimDeathLoopRescue(h, s, 201, attempts), 0u);
    s.dead = false; s.combat = true;
    CHECK_EQ(ClaimDeathLoopRescue(h, s, 202, attempts), 0u);
    s.combat = false;
    CHECK_EQ(ClaimDeathLoopRescue(h, s, 203, attempts), 3u);
    CHECK_EQ(attempts.attempts.size(), 1u);
}
LEEROY_TEST(DeathLoop_FifteenMinuteWindowIncludesBoundaryAndExpiresAfter)
{
    auto h = Three(); DeathRescueBudget attempts;
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), kDeathWindowMs, attempts), 3u);
    h = Three(); attempts = {};
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), kDeathWindowMs + 1, attempts), 0u);
    CHECK_EQ(h.deaths.size(), 2u);
}
LEEROY_TEST(DeathLoop_CooldownBoundsFailuresAndHandlesClockRegression)
{
    auto h = Three(); DeathRescueBudget attempts;
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), 200, attempts), 3u);
    Death(h, kDeathRescueCooldownMs - 100);
    Death(h, kDeathRescueCooldownMs);
    Death(h, kDeathRescueCooldownMs + 100);
    attempts = {};
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), kDeathRescueCooldownMs + 199, attempts), 0u);
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), kDeathRescueCooldownMs + 200, attempts), 3u);
    h = Three(); h.lastAttempt = 500;
    CHECK_EQ(ClaimDeathLoopRescue(h, Ready(), 300, attempts), 0u);
}

LEEROY_TEST(DeathLoop_TemporaryFencesKeepDeathsWhileBlockingRescue)
{
    // owned includes pending bot defaults and the global training cleanup fence.
    for (auto field : {&DeathLoopState::owned, &DeathLoopState::transit, &DeathLoopState::trading,
                      &DeathLoopState::duel, &DeathLoopState::invited, &DeathLoopState::queued})
    {
        auto h = Three(); auto s = Ready(); DeathRescueBudget budget;
        s.*field = true;
        ObserveDeathLoop(h,s,300);
        CHECK_EQ(h.deaths.size(),3u);
        CHECK_EQ(ClaimDeathLoopRescue(h,s,300,budget),0u);
        s.*field = false;
        CHECK_EQ(ClaimDeathLoopRescue(h,s,301,budget),3u);
    }
}

LEEROY_TEST(DeathLoop_DeathDuringTemporaryFenceStillCounts)
{
    auto s = Ready(); s.queued = true;
    DeathLoopHistory h;
    ObserveDeathLoop(h,s,0); s.dead = true;
    ObserveDeathLoop(h,s,1); s.queued = false;
    ObserveDeathLoop(h,s,2);
    CHECK_EQ(h.deaths.size(),1u); // The same corpse does not become another death.
}

LEEROY_TEST(DeathLoop_AnUnavailableBodyDoesNotInventAResurrection)
{
    auto h=Three(); auto s=Ready(); s.online=false;
    ObserveDeathLoop(h,s,300);
    s=Ready(); s.dead=true;
    ObserveDeathLoop(h,s,301);
    CHECK_EQ(h.deaths.size(),3u);
}

LEEROY_TEST(DeathLoop_RealOwnershipFencesNeverPermitRescue)
{
    for (auto field : {&DeathLoopState::cast,&DeathLoopState::pinned,&DeathLoopState::online,
                      &DeathLoopState::grouped,&DeathLoopState::instance,&DeathLoopState::battleground})
    {
        auto h=Three(); auto s=Ready(); DeathRescueBudget budget;
        s.*field=!(s.*field);
        ObserveDeathLoop(h,s,300);
        CHECK_EQ(ClaimDeathLoopRescue(h,s,300,budget),0u);
        CHECK_EQ(h.deaths.size(),3u); // Age/real movement, not a momentary flag, ends evidence.
    }
}

LEEROY_TEST(DeathLoop_AliveOutsideDeathAreaForOneMinuteEndsTheLoop)
{
    auto h=Three(); auto s=Ready();
    s.x=kDeathAreaRadius+1;
    ObserveDeathLoop(h,s,1000);
    ObserveDeathLoop(h,s,1000+kDeathAreaClearMs-1);
    CHECK_EQ(h.deaths.size(),3u);
    s.x=0; ObserveDeathLoop(h,s,1000+kDeathAreaClearMs); // Returning interrupts the proof.
    s.x=kDeathAreaRadius+1; ObserveDeathLoop(h,s,70000);
    ObserveDeathLoop(h,s,70000+kDeathAreaClearMs);
    CHECK(h.deaths.empty());
    h=Three(); s=Ready(); s.map=1;
    ObserveDeathLoop(h,s,1000); ObserveDeathLoop(h,s,1000+kDeathAreaClearMs);
    CHECK(h.deaths.empty());
}

LEEROY_TEST(DeathLoop_DeadOrTeleportingOutsideDoesNotClearTheLoop)
{
    for (auto field : {&DeathLoopState::dead,&DeathLoopState::transit})
    {
        auto h=Three(); auto s=Ready(); s.x=1000; s.*field=true;
        ObserveDeathLoop(h,s,1000); ObserveDeathLoop(h,s,1000+kDeathAreaClearMs);
        CHECK_EQ(h.deaths.size(),3u);
    }
}

LEEROY_TEST(DeathLoop_FiveRescuesPerRollingFifteenMinutesAcrossTicks)
{
    DeathRescueBudget budget;
    for (unsigned i=0;i<5;++i)
    {
        auto h=Three();
        CHECK_EQ(ClaimDeathLoopRescue(h,Ready(),300+i*1000,budget),3u);
    }
    auto h=Three();
    CHECK_EQ(ClaimDeathLoopRescue(h,Ready(),6000,budget),0u);
    CHECK_EQ(h.deaths.size(),3u);
    CHECK(!budget.Claim(300+kDeathWindowMs-1));
    CHECK(budget.Claim(300+kDeathWindowMs));
    CHECK(!budget.Claim(301+kDeathWindowMs));
    CHECK(!budget.Claim(0)); // Clock regression cannot refill the window.
}
