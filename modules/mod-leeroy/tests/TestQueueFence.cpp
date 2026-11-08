// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyQueueFencePolicy.h"
#include "TestHarness.h"

using namespace Leeroy;
namespace
{
constexpr QueueKind Kinds[] = {QueueKind::Battleground, QueueKind::Arena, QueueKind::DungeonFinder,
    QueueKind::RaidBrowser, QueueKind::Wintergrasp};
QueueFenceState Cast() { QueueFenceState state; state.cast = true; return state; }
}

LEEROY_TEST(QueueFence_FreeCastCanUseEveryQueue)
{
    for (auto kind : Kinds) CHECK(QueueFenceReason(Cast(), kind).empty());
}

LEEROY_TEST(QueueFence_EachNativeHoldRefusesEveryQueue)
{
    for (auto reason : {"busy:realm_owner", "busy:training_owner", "busy:training_cleanup",
        "busy:training_state", "busy:raid_owner"})
        for (auto kind : Kinds)
        {
            auto state = Cast(); state.reason = reason;
            CHECK_EQ(QueueFenceReason(state, kind), reason);
        }
}

LEEROY_TEST(QueueFence_HumanInstanceGroupNeedsNoOtherHold)
{
    for (auto kind : Kinds)
    {
        auto state = Cast(); state.humanGroupInInstance = true;
        CHECK_EQ(QueueFenceReason(state, kind), "busy:human_instance");
    }
}

LEEROY_TEST(QueueFence_NeverTargetsHumanOrNonCast)
{
    for (auto kind : Kinds)
    {
        auto state = Cast(); state.reason = "busy:training_owner"; state.humanGroupInInstance = true;
        state.human = true;
        CHECK(QueueFenceReason(state, kind).empty());
        state.human = false; state.cast = false;
        CHECK(QueueFenceReason(state, kind).empty());
    }
}

LEEROY_TEST(QueueFence_OffLeavesEveryQueueAlone)
{
    for (auto kind : Kinds)
    {
        auto state = Cast(); state.reason = "busy:realm_owner";
        state.humanGroupInInstance = true; state.enabled = false;
        CHECK(QueueFenceReason(state, kind).empty());
    }
}

LEEROY_TEST(QueueFence_OnlyExactNativeArenaCallHasAuthority)
{
    CHECK(QueueFenceNativeCallMatches(QueueKind::Arena, false, 101, 101, 11, 11, "operation-a", "operation-a"));
    for (auto kind : {QueueKind::Battleground, QueueKind::DungeonFinder, QueueKind::RaidBrowser, QueueKind::Wintergrasp})
        CHECK(!QueueFenceNativeCallMatches(kind, false, 101, 101, 11, 11, "operation-a", "operation-a"));
    CHECK(!QueueFenceNativeCallMatches(QueueKind::Arena, false, 101, 102, 11, 11, "operation-a", "operation-a"));
    CHECK(!QueueFenceNativeCallMatches(QueueKind::Arena, false, 101, 101, 12, 11, "operation-a", "operation-a"));
    CHECK(!QueueFenceNativeCallMatches(QueueKind::Arena, false, 101, 101, 11, 11, "operation-b", "operation-a"));
    CHECK(!QueueFenceNativeCallMatches(QueueKind::Arena, false, 101, 101, 11, 11, "", ""));
    CHECK(!QueueFenceNativeCallMatches(QueueKind::Arena, false, 0, 0, 11, 11, "operation-a", "operation-a"));
    CHECK(!QueueFenceNativeCallMatches(QueueKind::Arena, false, 101, 101, 0, 0, "operation-a", "operation-a"));
}

LEEROY_TEST(QueueFence_WintergraspAuthorityCannotBeUsedForArena)
{
    CHECK(QueueFenceNativeCallMatches(QueueKind::Wintergrasp, true, 101, 101, 11, 11, "operation-a", "operation-a"));
    CHECK(!QueueFenceNativeCallMatches(QueueKind::Arena, true, 101, 101, 11, 11, "operation-a", "operation-a"));
}

LEEROY_TEST(QueueFence_LogOnlyOnceAcrossRepeatedAttemptsAndTicks)
{
    QueueFenceLog gate;
    gate.Observe(101, "busy:realm_owner:operation-a");
    CHECK(gate.Refuse(101, "busy:realm_owner:operation-a"));
    for (int attempt = 0; attempt != 100; ++attempt)
    {
        gate.Observe(101, "busy:realm_owner:operation-a");
        CHECK(!gate.Refuse(101, "busy:realm_owner:operation-a"));
    }
    CHECK(gate.Refuse(102, "busy:realm_owner:operation-a"));
}

LEEROY_TEST(QueueFence_ReleaseOrNewOwnerAllowsOneNewLog)
{
    QueueFenceLog gate;
    CHECK(gate.Refuse(101, "busy:realm_owner:operation-a"));
    CHECK(gate.Refuse(101, "busy:realm_owner:operation-b"));
    CHECK(!gate.Refuse(101, "busy:realm_owner:operation-b"));
    gate.Observe(101, {});
    CHECK(gate.Refuse(101, "busy:realm_owner:operation-b"));
    gate.Clear();
    CHECK(gate.Refuse(101, "busy:realm_owner:operation-b"));
    CHECK(!gate.Refuse(101, {}));
}

LEEROY_TEST(QueueFence_OwnerRequestIgnoresRaidAndInstanceHolds)
{
    for (auto kind : Kinds)
    {
        auto state = Cast(); state.ownerRequest = true; state.ownerGroup = true;
        state.reason = "busy:raid_owner"; state.humanGroupInInstance = true;
        CHECK(QueueFenceReason(state, kind).empty());
        for (auto reason : {"busy:realm_owner", "busy:training_owner", "busy:training_cleanup", "busy:training_state"})
        {
            state.reason = reason;
            CHECK_EQ(QueueFenceReason(state, kind), reason);
        }
    }
}

LEEROY_TEST(QueueFence_OwnerGroupOutsideInstanceIsNotRaidHeld)
{
    for (auto kind : Kinds)
    {
        auto state = Cast(); state.ownerGroup = true; state.reason = "busy:raid_owner";
        CHECK(QueueFenceReason(state, kind).empty());
        state.reason = "busy:realm_owner";
        CHECK_EQ(QueueFenceReason(state, kind), "busy:realm_owner");
        state.reason.clear(); state.humanGroupInInstance = true;
        CHECK_EQ(QueueFenceReason(state, kind), "busy:human_instance");
    }
}

LEEROY_TEST(QueueFence_NonBotAndUnreachableBodyAreNotHolds)
{
    for (auto kind : Kinds)
        for (auto reason : {"not_bot", "busy:body_unavailable"})
        {
            auto state = Cast(); state.reason = reason;
            CHECK(QueueFenceReason(state, kind).empty());
        }
}
