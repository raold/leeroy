// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCleanup.h"
#include "LeeroyRealmIdentity.h"
#include "LeeroyRealmPolicy.h"
#include "TestHarness.h"
#include <optional>

using namespace Leeroy::Realm;
namespace
{
std::string Valid(std::uint32_t) { return ""; }
GroupCleanupReadback Restored(std::uint32_t guid)
{
    return {guid, true, true, true, true, true, false, false, true};
}
QueueCleanupReadback Removed(std::uint32_t guid) { return {guid, true, true, true, true, true}; }
}

LEEROY_TEST(RealmCleanup_FullActualRatedRosterMustAlreadyBeAdmitted)
{
    std::vector<std::uint32_t> const actual{301, 302, 303};
    std::vector<std::uint32_t> validated, charged;
    auto const why = PrepareCleanupMutation(actual, {304, 303, 301, 302}, [&](auto guid)
    {
        validated.push_back(guid); return std::string{};
    }, [&](auto const& members) { charged = members; return std::string{}; });
    CHECK(why.empty()); CHECK(validated == actual); CHECK(charged == actual);
    charged.clear();
    CHECK_EQ(PrepareCleanupMutation(actual, {301, 302}, Valid, [&](auto const& members)
        { charged = members; return std::string{}; }), std::string("cleanup_member_not_admitted"));
    CHECK(charged.empty());
}

LEEROY_TEST(RealmCleanup_MalformedActualRosterNeverReachesBudgetOrMutation)
{
    unsigned calls = 0;
    for (auto const& actual : std::vector<std::vector<std::uint32_t>>{{}, {0}, {301, 301}})
        CHECK_EQ(PrepareCleanupMutation(actual, {301, 302}, Valid,
            [&](auto const&) { ++calls; return std::string{}; }), std::string("cleanup_manifest_unavailable"));
    std::vector<std::uint32_t> oversized;
    for (std::uint32_t guid = 1; guid <= 151; ++guid) oversized.push_back(guid);
    CHECK_EQ(PrepareCleanupMutation(oversized, oversized, Valid,
        [&](auto const&) { ++calls; return std::string{}; }), std::string("cleanup_manifest_unavailable"));
    CHECK_EQ(calls, 0u);
}

LEEROY_TEST(RealmCleanup_ForeignHolderCannotBeAdoptedByACompleteRoster)
{
    bool charged = false;
    CHECK_EQ(PrepareCleanupMutation({301, 302}, {301, 302}, [](auto guid)
        { return guid == 302 ? std::string("busy:realm_owner") : std::string{}; },
        [&](auto const&) { charged = true; return std::string{}; }), std::string("busy:realm_owner"));
    CHECK(!charged);
}

LEEROY_TEST(RealmCleanup_SameAddressWithNewLifetimeOrGenerationBlocksWholeClosure)
{
    BodyIdentity saved; saved.account = 42; saved.player = reinterpret_cast<Player*>(std::uintptr_t{0x1000});
    saved.session = reinterpret_cast<WorldSession*>(std::uintptr_t{0x2000});
    for (unsigned change = 0; change < 4; ++change)
    {
        std::optional<BodyIdentity> current = saved;
        std::uint64_t generation = 7;
        if (change == 0) current->session = reinterpret_cast<WorldSession*>(std::uintptr_t{0x3000});
        if (change == 1) current->account = 43;
        if (change == 2) ++generation;
        if (change == 3) current.reset();
        bool charged = false;
        CHECK_EQ(PrepareCleanupMutation({301, 302}, {301, 302}, [&](auto guid)
        {
            return guid == 302 && CheckBodyLease(saved, 7, current, generation, {}) != LeaseCheck::Exact
                ? std::string("busy:body_identity_changed") : std::string{};
        }, [&](auto const&) { charged = true; return std::string{}; }), std::string("busy:body_identity_changed"));
        CHECK(!charged);
    }
}

LEEROY_TEST(RealmCleanup_FullRecursiveCostWaitsAtomicallyAndCannotExceedWholeTickCap)
{
    std::vector<std::uint32_t> const actual{301, 302, 303};
    TickBudget budget(3);
    CHECK(budget.Take());
    bool mutated = false;
    auto charge = [&](auto const& members)
    {
        if (members.size() > budget.Cap()) return std::string("action_cost_exceeds_cap");
        return budget.Take(static_cast<std::uint32_t>(members.size())) ? std::string{} : std::string("busy:tick_budget");
    };
    auto attempt = [&]
    {
        auto why = PrepareCleanupMutation(actual, actual, Valid, charge);
        if (why.empty()) mutated = true;
        return why;
    };
    CHECK_EQ(attempt(), std::string("busy:tick_budget"));
    CHECK_EQ(budget.Left(), 2u); CHECK(!mutated);
    budget.Reset(); CHECK(attempt().empty()); CHECK(mutated); CHECK_EQ(budget.Left(), 0u);
    TickBudget smaller(2);
    CHECK_EQ(PrepareCleanupMutation(actual, actual, Valid, [&](auto const& members)
    {
        if (members.size() > smaller.Cap()) return std::string("action_cost_exceeds_cap");
        CHECK(smaller.Take(static_cast<std::uint32_t>(members.size()))); return std::string{};
    }), std::string("action_cost_exceeds_cap"));
    CHECK_EQ(smaller.Left(), 2u);
}

LEEROY_TEST(RealmCleanup_LeaderLeaveChargesFullGroupAndUsesNativeRosterOrder)
{
    auto plan = PlanOnlineLfgRemoval({303, 301, 302}, {302, 303, 301}, 303, 303, true);
    CHECK(plan.error.empty());
    CHECK((plan.affected == std::vector<std::uint32_t>{303, 301, 302}));
    CHECK((plan.remaining == std::vector<std::uint32_t>{301, 302}));
    CHECK_EQ(plan.leader, 301u); CHECK(!plan.disbands);
}

LEEROY_TEST(RealmCleanup_PenultimateLeaveReservesSurvivorWhenNativeWillDisband)
{
    auto outside = PlanOnlineLfgRemoval({301, 302}, {301, 302}, 301, 302, false);
    CHECK(outside.error.empty()); CHECK(outside.disbands); CHECK(outside.remaining.empty());
    CHECK((outside.affected == std::vector<std::uint32_t>{301, 302}));
    CHECK_EQ(outside.leader, 0u);
    auto dungeon = PlanOnlineLfgRemoval({301, 302}, {301, 302}, 301, 302, true);
    CHECK(dungeon.error.empty()); CHECK(!dungeon.disbands);
    CHECK((dungeon.remaining == std::vector<std::uint32_t>{301}));
    auto last = PlanOnlineLfgRemoval(dungeon.remaining, {301}, 301, 301, true);
    CHECK(last.error.empty()); CHECK(last.disbands); CHECK(last.remaining.empty());
}

LEEROY_TEST(RealmCleanup_LinkedCountCannotSubstituteForExactRoster)
{
    CHECK(!PlanOnlineLfgRemoval({301, 302, 303}, {301}, 301, 302, true).error.empty());
    CHECK(!PlanOnlineLfgRemoval({301, 302}, {301, 303}, 301, 302, true).error.empty());
    CHECK(!PlanOnlineLfgRemoval({301, 302}, {301, 301, 302}, 301, 302, true).error.empty());
    CHECK(!PlanOnlineLfgRemoval({301, 302}, {301, 302}, 303, 301, true).error.empty());
}

LEEROY_TEST(RealmCleanup_GroupAbsenceAloneDoesNotProvePerMemberLfgHandback)
{
    for (unsigned missing = 0; missing < 5; ++missing)
    {
        auto first = Restored(301), survivor = Restored(302);
        if (missing == 0) survivor.sameLife = false;
        if (missing == 1) survivor.lfgStateNone = false;
        if (missing == 2) survivor.lfgGroupAbsent = false;
        if (missing == 3) survivor.originalGroupAbsent = false;
        if (missing == 4) survivor.arrived = false;
        CHECK(ReadGroupCleanup({301, 302}, {first, survivor}) == CleanupReadback::Unknown);
    }
}

LEEROY_TEST(RealmCleanup_OnlyExactPendingWorldportCanRetainProgress)
{
    auto survivor = Restored(302);
    survivor.teleporting = true; survivor.expectedTeleport = true; survivor.arrived = false;
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301), survivor}) == CleanupReadback::Pending);
    survivor.expectedTeleport = false;
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301), survivor}) == CleanupReadback::Unknown);
    survivor = Restored(302);
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301), survivor}) == CleanupReadback::Ready);
}

LEEROY_TEST(RealmCleanup_DelayedNativeEntryRestorationMustFinishBeforeRelease)
{
    auto survivor = Restored(302);
    survivor.delayedExitPending = true; survivor.expectedTeleport = true;
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301), survivor}) == CleanupReadback::Pending);
    survivor.expectedTeleport = false;
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301), survivor}) == CleanupReadback::Unknown);
    survivor.delayedExitPending = false;
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301), survivor}) == CleanupReadback::Ready);
}

LEEROY_TEST(RealmCleanup_ReadbackRequiresEveryExactMemberOnce)
{
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301)}) == CleanupReadback::Unknown);
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301), Restored(301)}) == CleanupReadback::Unknown);
    CHECK(ReadGroupCleanup({301, 302}, {Restored(301), Restored(303)}) == CleanupReadback::Unknown);
    CHECK(!ReadQueueCleanup({301, 302}, {Removed(301)}));
    CHECK(!ReadQueueCleanup({301, 302}, {Removed(301), Removed(301)}));
}

LEEROY_TEST(RealmCleanup_RecursiveQueueReadbackChecksRowsSlotsInvitationsAndOtherQueues)
{
    CHECK(ReadQueueCleanup({301, 302, 303}, {Removed(301), Removed(302), Removed(303)}));
    for (unsigned missing = 0; missing < 5; ++missing)
    {
        auto member = Removed(303);
        if (missing == 0) member.sameLife = false;
        if (missing == 1) member.rowAbsent = false;
        if (missing == 2) member.slotAbsent = false;
        if (missing == 3) member.invitationAbsent = false;
        if (missing == 4) member.unrelatedSlotsPreserved = false;
        CHECK(!ReadQueueCleanup({301, 302, 303}, {Removed(301), Removed(302), member}));
    }
}

LEEROY_TEST(RealmCleanup_AnyWintergraspStateRefusesTheLeaveWhole)
{
    CHECK_EQ(WintergraspPrivateReason({}), std::string{});
    char const* const reason = "wg_private_state_unavailable";
    for (int field = 0; field < 6; ++field)
    {
        WintergraspMemberFacts facts;
        bool* flags[] = {&facts.foreignHistory, &facts.inZone, &facts.inBattlefieldGroup,
            &facts.queued, &facts.invited, &facts.inWar};
        *flags[field] = true;
        CHECK_EQ(WintergraspPrivateReason(facts), std::string(reason));
    }
}

LEEROY_TEST(RealmCleanup_LeavePvpAckNamesTheWintergraspMembers)
{
    WintergraspMemberFacts clean, tainted;
    tainted.foreignHistory = true;
    CHECK(LeavePvpAckError({}).empty());
    CHECK(LeavePvpAckError({{301, clean}, {302, clean}}).empty());
    CHECK_EQ(LeavePvpAckError({{301, clean}, {302, tainted}, {303, clean}, {304, tainted}}),
        std::string("wg_private_state_unavailable:302,304"));
    std::vector<LeavePvpMemberFacts> many;
    for (std::uint32_t guid = 1000; guid < 1100; ++guid) many.push_back({guid, tainted});
    auto const error = LeavePvpAckError(many);
    CHECK(error.size() <= 256 && error.size() > 200);
    CHECK(error.rfind("wg_private_state_unavailable:1000,1001,", 0) == 0);
    // Ten-digit guids: still at most 256 characters, and never fewer than one name.
    many.clear();
    for (std::uint32_t guid = 4000000000u; guid < 4000000100u; ++guid) many.push_back({guid, tainted});
    auto const wide = LeavePvpAckError(many);
    CHECK(wide.size() <= 256 && wide.rfind("wg_private_state_unavailable:4000000000,4000000001,", 0) == 0);
    CHECK(wide.back() != ',');
}

LEEROY_TEST(RealmCleanup_LeaveAckClosureInsideManifestAdmits)
{
    CHECK(LeavePvpAckError({{301, {}, {301, 302}}, {302, {}, {302, 301}}, {303, {}, {303}}}).empty());
    // An offline manifest member still counts as admitted; its body fence is checked separately.
    CHECK(LeavePvpAckError({{301, {}, {301, 302}}, {302, {}}}).empty());
}

LEEROY_TEST(RealmCleanup_LeaveAckNamesMembersWhoseClosuresReachOutside)
{
    CHECK_EQ(LeavePvpAckError({{301, {}, {301, 999}}, {302, {}, {302}}, {303, {}, {303, 999, 998}}}),
        std::string("cleanup_member_not_admitted:301,303"));
    // Removing a named member can expose another closure on the next leave.
    CHECK(LeavePvpAckError({{301, {}, {301, 302}}, {302, {}, {302}}}).empty());
    CHECK_EQ(LeavePvpAckError({{301, {}, {301, 302}}}), std::string("cleanup_member_not_admitted:301"));
}

LEEROY_TEST(RealmCleanup_LeaveAckClosureNamesAreBoundedWholeGuids)
{
    std::vector<LeavePvpMemberFacts> many;
    for (std::uint32_t guid = 4000000000u; guid < 4000000150u; ++guid) many.push_back({guid, {}, {guid, 999}});
    auto const error = LeavePvpAckError(many);
    CHECK(error.size() <= 256 && error.size() > 240);
    CHECK(error.rfind("cleanup_member_not_admitted:4000000000,4000000001,", 0) == 0);
    CHECK(error.back() != ',');
    CHECK_EQ(LeavePvpAckError({many.front()}), std::string("cleanup_member_not_admitted:4000000000"));
}

LEEROY_TEST(RealmCleanup_LeaveAckWintergraspPrecedesExternalClosures)
{
    WintergraspMemberFacts tainted;
    tainted.inZone = true;
    CHECK_EQ(LeavePvpAckError({{301, {}, {301, 999}}, {302, tainted, {302, 999}}}),
        std::string("wg_private_state_unavailable:302"));
}

LEEROY_TEST(RealmCleanup_GroupDepartureReadsTheStepsGroupRules)
{
    GroupDepartureFacts facts{true, false, false, 10, false, 10};
    CHECK_EQ(GroupDepartureReason(facts), std::string{});
    facts.leader = true;
    CHECK_EQ(GroupDepartureReason(facts), std::string("shared_group_leader"));
    facts.listed = false; // A group that no longer lists the member is not its departure.
    CHECK_EQ(GroupDepartureReason(facts), std::string{});
    facts = {true, false, false, 1, true, 1}; // Alone: leading nobody, disbanding nothing shared.
    CHECK_EQ(GroupDepartureReason(facts), std::string{});
    facts = {true, false, false, 10, false, 1}; // RemoveMember disbands on the linked count.
    CHECK_EQ(GroupDepartureReason(facts), std::string("shared_group_cleanup"));
    facts = {true, true, false, 2, false, 2};
    CHECK_EQ(GroupDepartureReason(facts), std::string("shared_lfg_cleanup"));
    facts = {true, true, true, 1, false, 1}; // A shared LFG queue comes first, even alone.
    CHECK_EQ(GroupDepartureReason(facts), std::string("shared_lfg_queue"));
    facts = {true, true, false, 5, true, 1}; // Leadership precedes the other cleanups.
    CHECK_EQ(GroupDepartureReason(facts), std::string("shared_group_leader"));
    for (auto reason : {"shared_lfg_queue", "shared_group_leader", "shared_lfg_cleanup", "shared_group_cleanup"})
        CHECK(IsGroupDepartureReason(reason));
    for (auto reason : {"", "shared_lfg_proposal", "wg_private_state_unavailable", "shared_group_leader:1306"})
        CHECK(!IsGroupDepartureReason(reason));
}

LEEROY_TEST(RealmCleanup_LeaveAckNamesTheBattlegroundLeaders)
{
    // 2026-11-10: guid 1306 led a battleground raid; the leave moved others and then stranded all.
    CHECK_EQ(LeavePvpAckError({{301, {}}, {1306, {}, {1306}, "shared_group_leader"}, {303, {}},
        {304, {}, {304}, "shared_group_leader"}}), std::string("shared_group_leader:1306,304"));
    CHECK(LeavePvpAckError({{301, {}, {301}}, {302, {}, {302}, ""}}).empty());
}

LEEROY_TEST(RealmCleanup_LeaveAckPrecedenceIsWintergraspClosureThenGroups)
{
    WintergraspMemberFacts tainted;
    tainted.inWar = true;
    LeavePvpMemberFacts const leader{301, {}, {301}, "shared_group_leader"};
    LeavePvpMemberFacts const outside{302, {}, {302, 999}};
    LeavePvpMemberFacts const war{303, tainted, {303}};
    LeavePvpMemberFacts const lastLinked{304, {}, {304}, "shared_group_cleanup"};
    CHECK_EQ(LeavePvpAckError({leader, outside, war, lastLinked}), std::string("wg_private_state_unavailable:303"));
    CHECK_EQ(LeavePvpAckError({leader, outside, lastLinked}), std::string("cleanup_member_not_admitted:302"));
    CHECK_EQ(LeavePvpAckError({lastLinked, leader}), std::string("shared_group_leader:301"));
    CHECK_EQ(LeavePvpAckError({lastLinked}), std::string("shared_group_cleanup:304"));
    // One reason per error, in GroupDepartureReason's order; the retry without them meets the next.
    CHECK_EQ(LeavePvpAckError({{305, {}, {305}, "shared_lfg_cleanup"}, lastLinked, {306, {}, {306}, "shared_lfg_queue"}}),
        std::string("shared_lfg_queue:306"));
}

LEEROY_TEST(RealmCleanup_LeaveAckGroupNamesAreBoundedWholeGuids)
{
    std::vector<LeavePvpMemberFacts> many;
    for (std::uint32_t guid = 4000000000u; guid < 4000000150u; ++guid)
        many.push_back({guid, {}, {guid}, "shared_group_leader"});
    auto const error = LeavePvpAckError(many);
    CHECK(error.size() <= 256 && error.size() > 240);
    CHECK(error.rfind("shared_group_leader:4000000000,4000000001,", 0) == 0);
    CHECK(error.back() != ',');
    CHECK_EQ(LeavePvpAckError({many.front()}), std::string("shared_group_leader:4000000000"));
    many.clear();
    for (std::uint32_t guid = 1000; guid < 1100; ++guid) many.push_back({guid, {}, {guid}, "shared_group_cleanup"});
    auto const short_ = LeavePvpAckError(many);
    CHECK(short_.size() <= 256 && short_.size() > 200 && short_.rfind("shared_group_cleanup:1000,1001,", 0) == 0);
}

LEEROY_TEST(RealmCleanup_StepSkipsOnlyAMemberNothingInTheJobTouched)
{
    // #230's group departures, and the review's other reasons of the member's own: a battleground
    // that ended under it (its lease's scope), its original raid group, its entry point, its
    // battleground group changing, an LFG proposal, Wintergrasp, a closure it cannot take along.
    for (auto reason : {"shared_lfg_queue", "shared_group_leader", "shared_lfg_cleanup", "shared_group_cleanup",
        "busy:body_scope_changed", "busy:body_identity_changed", "body_identity_changed", "busy:raid_owner",
        "entry_point_unavailable", "battleground_group_changed", "battleground_receipt_unavailable",
        "shared_lfg_proposal", "shared_lfg_participants_unavailable", "wg_private_state_unavailable",
        "cleanup_member_not_admitted", "cleanup_member_offline", "leave_cleanup_unproved", "busy:teleport",
        "busy:training_state", "not_actor", "queue_lineage_changed", "action_cost_exceeds_cap"})
    {
        CHECK(SkipsLeaveMember(reason, false, false));
        CHECK(!SkipsLeaveMember(reason, true, false));  // Its own step already left a queue or a map.
        CHECK(!SkipsLeaveMember(reason, false, true));  // An earlier member's shared cleanup changed it.
        CHECK(!SkipsLeaveMember(reason, true, true));
    }
    // A reason about the job itself keeps the old rule: refused before any change, else unproved.
    // busy:training_cleanup and lfg_cleanup_disabled hold for every member at once.
    for (auto reason : {"realm_off", "busy:operation_identity", "busy:realm_owner", "unsupported", "",
        "busy:training_cleanup", "lfg_cleanup_disabled", "busy:tick_budget", "bad field: affected_bodies"})
        CHECK(!SkipsLeaveMember(reason, false, false));
}

LEEROY_TEST(RealmCleanup_AStepRechecksOnlyTheMembersTheJobReachedSoFar)
{
    // 2026-11-10's case, after the fix: 1306's battleground ended while 301 was still worldporting.
    // 301's step no longer stops for 1306 (later, untouched): 1306's own step checks it and skips it.
    std::vector<std::uint32_t> const manifest{301, 302, 1306, 304};
    auto none = [](std::uint32_t) { return false; };
    CHECK((RecheckedMembers(manifest, 0, {}, 0, none) == std::vector<std::uint32_t>{301}));
    CHECK((RecheckedMembers(manifest, 1, {}, 0, none) == std::vector<std::uint32_t>{301, 302}));
    CHECK(RecheckedMembers(manifest, 3, {}, 0, none) == manifest); // The last step reads back everyone held.
    // A later member an earlier member's shared cleanup touched (a queue group, an LFG roster) is
    // rechecked from then on; it can no longer be skipped.
    auto touched = [](std::uint32_t guid) { return guid == 1306; };
    CHECK((RecheckedMembers(manifest, 0, {}, 0, touched) == std::vector<std::uint32_t>{301, 1306}));
    // Never the skipped, nor the member whose skip is being decided.
    CHECK((RecheckedMembers(manifest, 3, {302}, 0, none) == std::vector<std::uint32_t>{301, 1306, 304}));
    CHECK((RecheckedMembers(manifest, 3, {302}, 304, none) == std::vector<std::uint32_t>{301, 1306}));
    CHECK(RecheckedMembers({1306}, 0, {}, 1306, none).empty());
}

LEEROY_TEST(RealmCleanup_ASkipLeavesTheOthersOutcomeNormal)
{
    // 301 left, 1306 (a battleground leader) was skipped, 303 left: the job holds and reads back
    // 301 and 303 alone, and a later closure that reaches the skipped body is no longer admitted.
    std::vector<std::uint32_t> const manifest{301, 1306, 303};
    CHECK(LeaveMembers(manifest, {}) == manifest);
    CHECK((LeaveMembers(manifest, {1306}) == std::vector<std::uint32_t>{301, 303}));
    CHECK((LeaveMembers(manifest, {}, 303) == std::vector<std::uint32_t>{301, 1306}));
    CHECK((LeaveMembers(manifest, {1306}, 303) == std::vector<std::uint32_t>{301}));
    CHECK(LeaveMembers({1306}, {1306}).empty());
    CHECK(ValidateCleanupClosure({303}, LeaveMembers(manifest, {1306}), Valid).empty());
    CHECK_EQ(ValidateCleanupClosure({303, 1306}, LeaveMembers(manifest, {1306}), Valid),
        std::string("cleanup_member_not_admitted"));
}

LEEROY_TEST(RealmCleanup_OnlyAnOrdinaryLeaveIsFencedAtItsAck)
{
    CHECK(FencesLeavePvpAck("leave_pvp", ""));
    CHECK(!FencesLeavePvpAck("leave_pvp", "token"));
    CHECK(!FencesLeavePvpAck("rearm", ""));
}
