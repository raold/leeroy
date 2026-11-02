// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmPolicy.h"
#include "TestHarness.h"

#include <array>
#include <limits>

using namespace Leeroy::Realm;

namespace
{
BodyState FreeBody()
{
    BodyState body;
    body.online = true;
    return body;
}

struct BusyField
{
    bool BodyState::*field;
    char const* reason;
};

std::array<BusyField, 9> const BusyFields{{
    {&BodyState::dead, "dead"}, {&BodyState::combat, "combat"}, {&BodyState::flight, "flight"},
    {&BodyState::raidGroup, "raid_group"}, {&BodyState::humanGroup, "human_group"},
    {&BodyState::instance, "instance"}, {&BodyState::battleground, "battleground"},
    {&BodyState::arena, "arena"}, {&BodyState::lfg, "lfg"}
}};

Kill ObservedKill(std::uint64_t at = 1000)
{
    return {101, 500, 3, at, true, 25, true};
}
}

LEEROY_TEST(RealmPolicy_BusyRulesEachFlagAndOrderedPrecedence)
{
    for (Purpose purpose : {Purpose::Move, Purpose::Status, Purpose::WgJoin, Purpose::Duel,
                            Purpose::Craft, Purpose::Equip})
    {
        CHECK(BusyReason(FreeBody(), purpose).empty());
        for (auto const& flag : BusyFields)
        {
            auto body = FreeBody();
            body.*flag.field = true;
            CHECK_EQ(BusyReason(body, purpose), flag.reason);
        }
        auto body = FreeBody();
        body.pvpOrLfgGroup = true;
        CHECK(BusyReason(body, purpose).empty()); // A group kind is not itself a busy state.
        for (auto const& flag : BusyFields)
            body.*flag.field = true;
        body.online = false;
        CHECK_EQ(BusyReason(body, purpose), "offline");
        body.online = true;
        for (auto const& flag : BusyFields)
        {
            CHECK_EQ(BusyReason(body, purpose), flag.reason);
            body.*flag.field = false;
        }
        CHECK(BusyReason(body, purpose).empty());
    }
}

LEEROY_TEST(RealmPolicy_LeavePvpWaivesOnlyItsGameplayObstacles)
{
    auto body = FreeBody();
    for (auto const& flag : BusyFields)
        body.*flag.field = true;
    body.pvpOrLfgGroup = true;
    CHECK_EQ(BusyReason(body, Purpose::LeavePvp), "human_group");
    body.humanGroup = false;
    CHECK(BusyReason(body, Purpose::LeavePvp).empty());
    body.pvpOrLfgGroup = false;
    CHECK_EQ(BusyReason(body, Purpose::LeavePvp), "raid_group");
    body.humanGroup = true;
    CHECK_EQ(BusyReason(body, Purpose::LeavePvp), "raid_group");
    body.raidGroup = false;
    CHECK_EQ(BusyReason(body, Purpose::LeavePvp), "human_group");
    body.online = false;
    CHECK_EQ(BusyReason(body, Purpose::LeavePvp), "offline");
}

LEEROY_TEST(RealmPolicy_RearmStillRequiresOnlineAliveAndOutOfCombat)
{
    auto body = FreeBody();
    for (auto const& flag : BusyFields)
        body.*flag.field = true;
    CHECK_EQ(BusyReason(body, Purpose::Rearm), "dead");
    body.dead = false;
    CHECK_EQ(BusyReason(body, Purpose::Rearm), "combat");
    body.combat = false;
    CHECK(BusyReason(body, Purpose::Rearm).empty());
    body.online = false;
    CHECK_EQ(BusyReason(body, Purpose::Rearm), "offline");
}

LEEROY_TEST(RealmPolicy_MailAndBankOnlyWaiveGroupMembership)
{
    for (Purpose purpose : {Purpose::Mail, Purpose::Bank})
    {
        auto body = FreeBody();
        body.raidGroup = body.humanGroup = true;
        CHECK(BusyReason(body, purpose).empty());
        for (auto const& flag : BusyFields)
        {
            if (flag.field == &BodyState::raidGroup || flag.field == &BodyState::humanGroup)
                continue;
            body.*flag.field = true;
            CHECK_EQ(BusyReason(body, purpose), flag.reason);
            body.*flag.field = false;
        }
        body.online = false;
        CHECK_EQ(BusyReason(body, purpose), "offline");
    }
}

LEEROY_TEST(RealmPolicy_DefaultBodyNeverImpliesOnline)
{
    for (Purpose purpose : {Purpose::Move, Purpose::Status, Purpose::LeavePvp, Purpose::Rearm,
                            Purpose::WgJoin, Purpose::Duel, Purpose::Craft, Purpose::Bank,
                            Purpose::Equip, Purpose::Mail})
        CHECK_EQ(BusyReason({}, purpose), "offline");
}

LEEROY_TEST(RealmPolicy_LfgExitIncludesDungeonBootAndFinishedStates)
{
    for (LfgPhase phase : {LfgPhase::None, LfgPhase::Queued, LfgPhase::RoleCheck, LfgPhase::Proposal,
                           LfgPhase::RaidBrowser, LfgPhase::Dungeon, LfgPhase::FinishedDungeon, LfgPhase::Boot})
    {
        CHECK(DecideLfgExit(phase, true) == LfgExit::LeaveGroup);
        CHECK(DecideLfgExit(phase, false) == (phase == LfgPhase::None ? LfgExit::None : LfgExit::LeaveQueue));
    }
}

LEEROY_TEST(RealmPolicy_FlaggedIsNarrowAndLocationAloneIsNotConsent)
{
    HumanPvp human;
    human.online = true;
    human.zone = 44;
    CHECK(!Flagged(human));
    CHECK(!GankerMoveAllowed(human, 44));
    CHECK(GankerMoveAllowed(human, 45));
    for (auto field : {&HumanPvp::manualFlag, &HumanPvp::battleground, &HumanPvp::arena, &HumanPvp::wgWar})
    {
        human.*field = true;
        CHECK(Flagged(human));
        CHECK(GankerMoveAllowed(human, 44));
        human.*field = false;
    }
    human.online = false;
    CHECK(!Flagged(human));
    CHECK(GankerMoveAllowed(human, 44));
    human.manualFlag = true;
    CHECK(Flagged(human)); // Online is a separate input to movement admission.
}

LEEROY_TEST(RealmPolicy_AnnounceNeedsMatchingObservedGuildKill)
{
    KillRing ring;
    CHECK(!ring.Match(101, 500, 3, 1000));
    auto kill = ObservedKill();
    kill.guildKill = false;
    ring.Add(kill);
    CHECK(!ring.Match(101, 500, 3, 1000));
    kill.guildKill = true;
    ring.Add(kill);
    CHECK(!ring.Match(102, 500, 3, 1000));
    CHECK(!ring.Match(101, 501, 3, 1000));
    CHECK(!ring.Match(101, 500, 4, 1000));
    auto found = ring.Match(101, 500, 3, 1000);
    CHECK(found.has_value());
    if (found)
    {
        CHECK_EQ(found->size, 25u);
        CHECK(found->heroic);
        CHECK_EQ(found->atMs, 1000u);
    }
}

LEEROY_TEST(RealmPolicy_KillFreshnessIncludesBoundaryWithoutUnsignedUnderflow)
{
    KillRing ring;
    ring.Add(ObservedKill(1000));
    CHECK(ring.Match(101, 500, 3, 601000).has_value());
    CHECK(!ring.Match(101, 500, 3, 601001));
    CHECK(ring.Match(101, 500, 3, 1000, 0).has_value());
    CHECK(!ring.Match(101, 500, 3, 1001, 0));
    CHECK(!ring.Match(101, 500, 3, 999));
    CHECK(!ring.Match(101, 500, 3, 0, std::numeric_limits<std::uint64_t>::max()));
    KillRing nearLimit;
    auto const maximum = std::numeric_limits<std::uint64_t>::max();
    nearLimit.Add(ObservedKill(maximum - 5));
    CHECK(nearLimit.Match(101, 500, 3, maximum, 5).has_value());
    CHECK(!nearLimit.Match(101, 500, 3, maximum, 4));
    CHECK(!nearLimit.Match(101, 500, 3, 3, maximum));
}

LEEROY_TEST(RealmPolicy_KillMatchUsesNewestEligibleObservation)
{
    KillRing ring;
    auto newer = ObservedKill(2000);
    newer.size = 10;
    newer.heroic = false;
    ring.Add(newer);
    ring.Add(ObservedKill(1000)); // Out-of-order old receipt cannot replace newer facts.
    ring.Add(ObservedKill(3001)); // Future receipt cannot mask a valid earlier one.
    auto otherGuild = ObservedKill(3000);
    otherGuild.guildKill = false;
    ring.Add(otherGuild);
    auto found = ring.Match(101, 500, 3, 3000);
    CHECK(found.has_value());
    if (found)
    {
        CHECK_EQ(found->atMs, 2000u);
        CHECK_EQ(found->size, 10u);
        CHECK(!found->heroic);
    }
}

LEEROY_TEST(RealmPolicy_KillRingEvictsByInsertionAtFixedCapacity)
{
    KillRing ring;
    for (std::uint32_t boss = 1; boss <= 32; ++boss)
        ring.Add({101, 500, boss, boss, true, 10, false});
    CHECK(ring.Match(101, 500, 1, 32).has_value());
    ring.Add({101, 500, 33, 33, true, 10, false});
    CHECK(!ring.Match(101, 500, 1, 33));
    CHECK(ring.Match(101, 500, 2, 33).has_value());
    for (std::uint32_t boss = 34; boss <= 96; ++boss)
        ring.Add({101, 500, boss, boss, true, 10, false});
    CHECK(!ring.Match(101, 500, 64, 96));
    CHECK(ring.Match(101, 500, 65, 96).has_value());
    CHECK(ring.Match(101, 500, 96, 96).has_value());
}

LEEROY_TEST(RealmPolicy_TickBudgetCannotUnderflowAndResetsExactly)
{
    TickBudget budget(5);
    for (std::uint32_t left = 5; left > 0; --left)
    {
        CHECK_EQ(budget.Left(), left);
        CHECK(budget.Take());
    }
    CHECK_EQ(budget.Left(), 0u);
    CHECK(!budget.Take());
    CHECK(!budget.Take());
    CHECK_EQ(budget.Left(), 0u);
    budget.Reset();
    CHECK_EQ(budget.Left(), 5u);
    CHECK(budget.Take());
    budget.Reset();
    CHECK_EQ(budget.Left(), 5u);
}

LEEROY_TEST(RealmPolicy_TickBudgetHandlesZeroAndMaximumCap)
{
    TickBudget zero(0);
    CHECK(!zero.Take());
    zero.Reset();
    CHECK(!zero.Take());
    CHECK_EQ(zero.Left(), 0u);
    auto const maximum = std::numeric_limits<std::uint32_t>::max();
    TickBudget large(maximum);
    CHECK(large.Take());
    CHECK_EQ(large.Left(), maximum - 1);
    large.Reset();
    CHECK_EQ(large.Left(), maximum);
}

LEEROY_TEST(RealmPolicy_RearmRecordsOnlyRealMainWeapons)
{
    CHECK(!ShouldRecord({}));
    CHECK(!ShouldRecord({0, 9002, 9003, false}));
    CHECK(!ShouldRecord({8001, 0, 0, true}));
    CHECK(ShouldRecord({9001, 0, 0, false}));
}

LEEROY_TEST(RealmPolicy_RearmPlansPresentItemsInSlotOrder)
{
    WeaponRecord const record{9001, 9002, 9003};
    std::set<std::uint32_t> const bags{9001, 9002, 9003};
    CHECK((PlanRearm(record, {8001, 0, 0, true}, bags) == std::vector<RearmStep>{
        {WeaponSlot::Main, 9001}, {WeaponSlot::Off, 9002}, {WeaponSlot::Ranged, 9003}}));
    CHECK((PlanRearm(record, {}, bags) == std::vector<RearmStep>{
        {WeaponSlot::Main, 9001}, {WeaponSlot::Off, 9002}, {WeaponSlot::Ranged, 9003}}));
    CHECK((PlanRearm(record, {9004, 0, 0, false}, bags) == std::vector<RearmStep>{
        {WeaponSlot::Off, 9002}, {WeaponSlot::Ranged, 9003}}));
}

LEEROY_TEST(RealmPolicy_RearmNeverOverwritesOccupiedRealWeapons)
{
    WeaponRecord const record{9001, 9002, 9003};
    std::set<std::uint32_t> const bags{9001, 9002, 9003};
    CHECK(PlanRearm(record, {9011, 9012, 9013, false}, bags).empty());
    CHECK((PlanRearm(record, {8001, 9012, 9013, true}, bags) == std::vector<RearmStep>{{WeaponSlot::Main, 9001}}));
    CHECK(PlanRearm(record, {9001, 9002, 9003, false}, bags).empty());
}

LEEROY_TEST(RealmPolicy_RearmNeverCreatesMissingOrZeroItems)
{
    WeaponRecord const record{9001, 9002, 9003};
    CHECK(PlanRearm(record, {8001, 0, 0, true}, {}).empty());
    CHECK(PlanRearm({}, {}, {0, 9001}).empty());
    CHECK((PlanRearm(record, {}, {0, 9002}) == std::vector<RearmStep>{{WeaponSlot::Off, 9002}}));
    CHECK((PlanRearm(record, {}, {9003}) == std::vector<RearmStep>{{WeaponSlot::Ranged, 9003}}));
}

LEEROY_TEST(RealmPolicy_RearmRejectsAmbiguousRecordedItemIdentity)
{
    std::set<std::uint32_t> const bags{9001, 9002};
    CHECK((PlanRearm({9001, 9001, 9002}, {}, bags) == std::vector<RearmStep>{{WeaponSlot::Ranged, 9002}}));
    CHECK((PlanRearm({9001, 9002, 9001}, {}, bags) == std::vector<RearmStep>{{WeaponSlot::Off, 9002}}));
    CHECK((PlanRearm({9002, 9001, 9001}, {}, bags) == std::vector<RearmStep>{{WeaponSlot::Main, 9002}}));
    CHECK(PlanRearm({9001, 9001, 9001}, {}, bags).empty());
}

LEEROY_TEST(RealmPolicy_RearmNeverStealsAnEquippedItemFromAnotherSlot)
{
    WeaponRecord const record{9001, 9002, 9003};
    // An inconsistent bag snapshot may claim an equipped GUID. Preserve the
    // actual occupied slots instead of planning an implicit cross-slot move.
    CHECK((PlanRearm(record, {0, 9001, 0, false}, {9001, 9002, 9003})
        == std::vector<RearmStep>{{WeaponSlot::Ranged, 9003}}));
    CHECK((PlanRearm(record, {9002, 0, 0, false}, {9001, 9002, 9003})
        == std::vector<RearmStep>{{WeaponSlot::Ranged, 9003}}));
    CHECK((PlanRearm(record, {0, 0, 9001, false}, {9001, 9002, 9003})
        == std::vector<RearmStep>{{WeaponSlot::Off, 9002}}));
    CHECK(PlanRearm({9001, 0, 0}, {9001, 0, 0, true}, {9001}).empty());
}
