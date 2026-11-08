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
    std::vector<std::pair<std::uint32_t, WintergraspMemberFacts>> many;
    for (std::uint32_t guid = 1000; guid < 1100; ++guid) many.emplace_back(guid, tainted);
    auto const error = LeavePvpAckError(many);
    CHECK(error.size() <= 256 && error.size() > 200);
    CHECK(error.rfind("wg_private_state_unavailable:1000,1001,", 0) == 0);
    // Ten-digit guids: still at most 256 characters, and never fewer than one name.
    many.clear();
    for (std::uint32_t guid = 4000000000u; guid < 4000000100u; ++guid) many.emplace_back(guid, tainted);
    auto const wide = LeavePvpAckError(many);
    CHECK(wide.size() <= 256 && wide.rfind("wg_private_state_unavailable:4000000000,4000000001,", 0) == 0);
    CHECK(wide.back() != ',');
}

LEEROY_TEST(RealmCleanup_OnlyAnOrdinaryLeaveIsFencedAtItsAck)
{
    CHECK(FencesLeavePvpAck("leave_pvp", ""));
    CHECK(!FencesLeavePvpAck("leave_pvp", "token"));
    CHECK(!FencesLeavePvpAck("rearm", ""));
}
