// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyOfflineRaidPolicy.h"
#include "TestHarness.h"

#include <limits>

using namespace Leeroy::Guild;

namespace
{
OfflineRaidLimits const limits{6, 3, 180 * 60000, 8, 8};
OfflineRaidState Base() { return {0, false, true, 10, 0, 0, 0, 12, true, 0, 0}; }
}

LEEROY_TEST(OfflineRaid_HumanOnlineBeatsEverything)
{
    auto state = Base();
    state.humanOnline = true;
    state.elapsedMs = 1000000000;
    state.wipes = 99;
    state.leaderPresent = false;
    CHECK(Decide(state, limits).verdict == Verdict::Done);
    CHECK_EQ(Decide(state, limits).reason, "human_login");
}

LEEROY_TEST(OfflineRaid_Order)
{
    auto state = Base();
    state.elapsedMs = limits.timeoutMs - 1;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.elapsedMs = limits.timeoutMs;
    CHECK_EQ(Decide(state, limits).reason, "timeout");
    state = Base();
    state.leaderPresent = false;
    state.membersPresent = 7;
    CHECK_EQ(Decide(state, limits).reason, "leader_lost");
    state.leaderPresent = true;
    state.wipes = 6;
    state.maxWipesOnOneBoss = 3;
    CHECK_EQ(Decide(state, limits).reason, "too_few");
    state.membersPresent = 8;
    CHECK_EQ(Decide(state, limits).reason, "boss_wipe_cap");
    state.maxWipesOnOneBoss = 2;
    CHECK_EQ(Decide(state, limits).reason, "wipe_cap");
    state.wipes = 5;
    state.bossesDone = 12;
    CHECK_EQ(Decide(state, limits).reason, "cleared");
}

LEEROY_TEST(OfflineRaid_EmptyOrInconsistentBossRosterNeverClaimsCleared)
{
    auto state = Base();
    state.bossesTotal = 0;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.bossesDone = 1;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.bossesTotal = 1;
    state.bossesDone = 2;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
}

LEEROY_TEST(OfflineRaid_DcStopped)
{
    auto state = Base();
    state.dcEnabled = false;
    state.dcOffMs = 29999;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.dcOffMs = 30000;
    CHECK(Decide(state, limits).verdict == Verdict::Restart);
    CHECK_EQ(Decide(state, limits).reason, "dc_stopped");
    state.restarts = 8;
    CHECK(Decide(state, limits).verdict == Verdict::Done);
    CHECK_EQ(Decide(state, limits).reason, "dc_stalled");
    state.dcEnabled = true;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
}

LEEROY_TEST(OfflineRaid_ClearedWinsOverRestartButNeverOverWipeCap)
{
    auto state = Base();
    state.bossesDone = state.bossesTotal;
    state.dcEnabled = false;
    state.dcOffMs = 30000;
    state.restarts = 8;
    CHECK_EQ(Decide(state, limits).reason, "cleared");
    state.wipes = 6;
    CHECK_EQ(Decide(state, limits).reason, "wipe_cap");
}

LEEROY_TEST(OfflineRaid_IsWipe)
{
    CHECK(IsWipe(1, 0));
    CHECK(IsWipe(1, 2));
    CHECK(!IsWipe(1, 3));
    CHECK(!IsWipe(0, 0));
    CHECK(!IsWipe(3, 0));
    CHECK(!IsWipe(2, 0));
    CHECK(!IsWipe(1, 1));
    CHECK(!IsWipe(5, 2));
}

LEEROY_TEST(OfflineRaid_FreshCopyAcceptsBossesWhoseGridsHaveNotLoaded)
{
    // NOT_STARTED 0, IN_PROGRESS 1, FAIL 2, DONE 3, SPECIAL 4, TO_BE_DECIDED 5. A new ICC copy entered at
    // the door reads Marrowgar as 0 and every later boss as 5 until its grid loads.
    CHECK(FreshEncounters({0, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5}, 0));
    CHECK(FreshEncounters({5, 5, 5, 5, 5}, 0));
    CHECK(FreshEncounters({0, 0, 0}, 0));
    CHECK(!FreshEncounters({0, 3, 5}, 0));
    CHECK(!FreshEncounters({5, 5, 3}, 0));
    CHECK(!FreshEncounters({0, 5, 5}, 1u << 2));  // A saved copy's credit counts even before its boss loads.
    CHECK(!FreshEncounters({}, 0));
}

LEEROY_TEST(OfflineRaid_TimersSaturateInsteadOfExtendingTheRunAfterOverflow)
{
    auto const maximum = std::numeric_limits<std::uint32_t>::max();
    CHECK_EQ(AdvanceElapsed(100, 20), 120u);
    CHECK_EQ(AdvanceElapsed(maximum - 5, 5), maximum);
    CHECK_EQ(AdvanceElapsed(maximum - 5, 6), maximum);
    CHECK_EQ(AdvanceElapsed(maximum, maximum), maximum);
    auto state = Base();
    state.elapsedMs = AdvanceElapsed(maximum - 5, 6);
    CHECK_EQ(Decide(state, limits).reason, "timeout");
}

LEEROY_TEST(OfflineRaid_RuntimeDisableAndDriverLossStopSetupAndRestarts)
{
    CHECK(OfflineRaidStopReason(false, true, true, true, 10, 100).empty());
    CHECK_EQ(OfflineRaidStopReason(false, false, true, true, 10, 100), "module_disabled");
    CHECK_EQ(OfflineRaidStopReason(false, true, false, true, 10, 100), "keep_cast_off");
    CHECK_EQ(OfflineRaidStopReason(false, true, true, false, 10, 100), "driver_lost");
    CHECK_EQ(OfflineRaidStopReason(false, false, false, false, 100, 100), "timeout");
    CHECK_EQ(OfflineRaidStopReason(true, false, false, false, 100, 100), "human_login");
    CHECK_EQ(OfflineRaidStopReason(false, true, true, true, 10, 100, false), "instance_strategies_off");
    CHECK_EQ(OfflineRaidStopReason(true, true, true, true, 10, 100, false), "human_login");
}
