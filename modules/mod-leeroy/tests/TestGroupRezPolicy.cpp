// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyGroupRezPolicy.h"
#include "TestHarness.h"
#include <string>
using namespace Leeroy::Guild;
namespace
{
constexpr std::uint64_t kRelease = 60000;
GroupRezBody Bot() { GroupRezBody b; b.bot = true; return b; }
GroupRezCandidate Rezzer(std::uint32_t guid, float distance)
{
    GroupRezCandidate c;
    c.guid = guid; c.distance = distance; c.castRange = 30.0f; c.los = distance <= 30.0f;
    c.mana = 20000; c.manaCost = 2781; // Resurrection: 72% of a level 80 priest's base mana
    return c;
}
GroupRezCandidate Dry(std::uint32_t guid, float distance)
{
    auto c = Rezzer(guid, distance);
    c.mana = 1150; // what a fresh Resurrection leaves a rezzed healer
    return c;
}
GroupRezDecision Decide(GroupRezBody const& body, bool fight, std::vector<GroupRezCandidate> const& candidates,
    GroupRezMemory& memory, std::uint64_t now, std::uint64_t release = kRelease)
{
    static GroupRezCasters const none;
    return DecideGroupRez(body, fight, candidates, memory, none, now, release);
}
std::string Code(GroupRezDecision const& d) { return GroupRezBlockCode(d.block); }
}

LEEROY_TEST(GroupRez_RanksAreHighestFirstAndOnlyForRezClasses)
{
    CHECK_EQ(RezRanks(2)->front(), 48950u);
    CHECK_EQ(RezRanks(2)->back(), 7328u);
    CHECK_EQ(RezRanks(5)->front(), 48171u);
    CHECK_EQ(RezRanks(7)->front(), 49277u);
    CHECK_EQ(RezRanks(11)->front(), 50763u); // druid Revive, rank 7
    CHECK_EQ(RezRanks(11)->back(), 50769u);
    CHECK(RezRanks(1) == nullptr);
}

LEEROY_TEST(GroupRez_CastsFromTheNearestRezzerInRangeEvenWhenMounted)
{
    GroupRezMemory m;
    auto mounted = Rezzer(7, 12.0f);
    mounted.mounted = true;
    auto d = Decide(Bot(), false, {Rezzer(9, 25.0f), mounted}, m, 0);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 7u);
    CHECK_EQ(Code(d), std::string("none"));
}

LEEROY_TEST(GroupRez_WalksTheNearestRezzerInWhenNoneIsInRangeOrSight)
{
    // Before: a walker 30-57 yards out was left to playerbots, and one past 57 yards was never used.
    GroupRezMemory m;
    auto d = Decide(Bot(), false, {Rezzer(8, 80.0f), Rezzer(6, 45.0f)}, m, 0);
    CHECK(d.step == GroupRezStep::Approach);
    CHECK_EQ(d.caster, 6u);
    CHECK_EQ(Code(d), std::string("none"));

    GroupRezMemory mounted;
    auto rider = Rezzer(7, 45.0f);
    rider.mounted = true;
    d = Decide(Bot(), false, {rider}, mounted, 0);
    CHECK(d.step == GroupRezStep::Approach); // rides in; the cast puts it down
    CHECK_EQ(d.caster, 7u);

    GroupRezMemory blind;
    auto behind = Rezzer(5, 10.0f);
    behind.los = false;
    d = Decide(Bot(), false, {behind}, blind, 0);
    CHECK(d.step == GroupRezStep::Approach);
    CHECK_EQ(d.caster, 5u);
}

LEEROY_TEST(GroupRez_TheWalkerKeepsItsBodyAndCastsOnArrival)
{
    GroupRezMemory m;
    CHECK_EQ(Decide(Bot(), false, {Rezzer(8, 60.0f), Rezzer(9, 70.0f)}, m, 0).caster, 8u);
    // 9 is now nearer, but 8 is on its way.
    auto d = Decide(Bot(), false, {Rezzer(8, 50.0f), Rezzer(9, 40.0f)}, m, 1000);
    CHECK(d.step == GroupRezStep::Approach);
    CHECK_EQ(d.caster, 8u);
    d = Decide(Bot(), false, {Rezzer(8, 28.0f), Rezzer(9, 40.0f)}, m, 6000);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 8u);
    CHECK(!m.approachCaster.has_value());
}

LEEROY_TEST(GroupRez_ARezzerAlreadyInRangeCastsInsteadOfWaitingForTheWalker)
{
    GroupRezMemory m;
    CHECK_EQ(Decide(Bot(), false, {Rezzer(8, 60.0f)}, m, 0).caster, 8u);
    auto d = Decide(Bot(), false, {Rezzer(8, 50.0f), Rezzer(9, 20.0f)}, m, 1000);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 9u);
}

LEEROY_TEST(GroupRez_AWalkInThatNeverArrivesGivesThatRezzerUpForTheBody)
{
    GroupRezMemory m;
    CHECK(Decide(Bot(), false, {Rezzer(8, 60.0f)}, m, 0).step == GroupRezStep::Approach);
    CHECK(Decide(Bot(), false, {Rezzer(8, 60.0f)}, m, kGroupRezApproachMs - 1).step == GroupRezStep::Approach);
    CHECK(Decide(Bot(), false, {Rezzer(8, 60.0f)}, m, kGroupRezApproachMs).step == GroupRezStep::None);
    auto d = Decide(Bot(), false, {Rezzer(8, 60.0f)}, m, kGroupRezApproachMs + 1000);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("unreachable"));
    d = Decide(Bot(), false, {Rezzer(8, 60.0f), Rezzer(9, 70.0f)}, m, kGroupRezApproachMs + 2000);
    CHECK(d.step == GroupRezStep::Approach);
    CHECK_EQ(d.caster, 9u);
    GroupRezMemory later;
    later.unreachableAt[8] = 0;
    d = Decide(Bot(), false, {Rezzer(8, 60.0f)}, later, kGroupRezUnreachableMs);
    CHECK(d.step == GroupRezStep::Approach);
    CHECK_EQ(d.caster, 8u);
}

LEEROY_TEST(GroupRez_ARezzerWithNoPathIsGivenUpAtOnce)
{
    GroupRezMemory m;
    CHECK_EQ(Decide(Bot(), false, {Rezzer(8, 60.0f), Rezzer(9, 70.0f)}, m, 0).caster, 8u);
    NoteGroupRezUnreachable(m, 8, 0);
    CHECK(!m.approachCaster.has_value());
    auto d = Decide(Bot(), false, {Rezzer(8, 60.0f), Rezzer(9, 70.0f)}, m, 1000);
    CHECK(d.step == GroupRezStep::Approach);
    CHECK_EQ(d.caster, 9u);
    NoteGroupRezUnreachable(m, 9, 1000);
    CHECK_EQ(Code(Decide(Bot(), false, {Rezzer(8, 60.0f), Rezzer(9, 70.0f)}, m, 2000)), std::string("unreachable"));
}

LEEROY_TEST(GroupRez_NeverUsesAnAirborneMountedRezzerOrOneOutOfReach)
{
    GroupRezMemory m;
    auto airborne = Rezzer(7, 5.0f);
    airborne.mounted = true; airborne.grounded = false;
    auto distant = Rezzer(9, kGroupRezReachYards + 1.0f);
    auto d = Decide(Bot(), false, {airborne, distant}, m, 0);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("casters_busy"));
    d = Decide(Bot(), false, {airborne, Rezzer(10, 20.0f)}, m, 1);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 10u);

    GroupRezMemory outside;
    d = Decide(Bot(), false, {distant}, outside, 0);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("no_caster_in_range"));
    CHECK_EQ(Code(Decide(Bot(), false, {}, outside, 0)), std::string("no_caster"));
}

LEEROY_TEST(GroupRez_PrefersARezzerWithTheManaForItsRez)
{
    // 06:32 on 2026-11-21: the nearest rezzer had no mana and was refused NO_POWER on three bodies.
    GroupRezMemory m;
    auto d = Decide(Bot(), false, {Dry(3363, 5.0f), Rezzer(2708, 20.0f)}, m, 0);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 2708u);
    GroupRezMemory walk;
    d = Decide(Bot(), false, {Dry(3363, 5.0f), Rezzer(2708, 70.0f)}, walk, 0);
    CHECK(d.step == GroupRezStep::Approach);
    CHECK_EQ(d.caster, 2708u);
    auto exact = Rezzer(4, 5.0f);
    exact.mana = exact.manaCost;
    GroupRezMemory enough;
    CHECK(Decide(Bot(), false, {exact}, enough, 0).step == GroupRezStep::Cast);
}

LEEROY_TEST(GroupRez_ADryRezzerIsAskedToDrinkOnceForEveryBodyThenCastsWhenItHasTheMana)
{
    GroupRezCasters casters;
    GroupRezMemory a, b;
    auto d = DecideGroupRez(Bot(), false, {Dry(3363, 5.0f)}, a, casters, 0, kRelease);
    CHECK(d.step == GroupRezStep::Drink);
    CHECK_EQ(d.caster, 3363u);
    CHECK_EQ(Code(d), std::string("all_oom"));
    NoteGroupRezDrinkAsked(casters, 3363, 0);
    d = DecideGroupRez(Bot(), false, {Dry(3363, 8.0f)}, b, casters, 0, kRelease);
    CHECK(d.step == GroupRezStep::None); // a second body does not ask again
    CHECK_EQ(Code(d), std::string("all_oom"));
    d = DecideGroupRez(Bot(), false, {Dry(3363, 5.0f), Dry(1686, 9.0f)}, a, casters, 1000, kRelease);
    CHECK(d.step == GroupRezStep::Drink); // the next dry one is asked too
    CHECK_EQ(d.caster, 1686u);
    CHECK(DecideGroupRez(Bot(), false, {Dry(3363, 5.0f)}, a, casters, kGroupRezDrinkRetryMs - 1, kRelease).step
        == GroupRezStep::None);
    CHECK(DecideGroupRez(Bot(), false, {Dry(3363, 5.0f)}, a, casters, kGroupRezDrinkRetryMs, kRelease).step
        == GroupRezStep::Drink);
    d = DecideGroupRez(Bot(), false, {Rezzer(3363, 5.0f)}, a, casters, kGroupRezDrinkRetryMs + 1000, kRelease);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 3363u);
}

LEEROY_TEST(GroupRez_ARefusedRezzerIsPassedOverForEveryBodySoAnotherCanTry)
{
    GroupRezCasters casters;
    GroupRezMemory a, b;
    CHECK_EQ(DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f), Rezzer(9, 20.0f)}, a, casters, 0, kRelease).caster, 7u);
    NoteGroupRezRefused(casters, 7, 0, false);
    auto d = DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f), Rezzer(9, 20.0f)}, b, casters, 0, kRelease);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 9u);
    d = DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f)}, a, casters, kGroupRezRetryMs * 2, kRelease);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("casters_busy"));
    CHECK_EQ(DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f)}, a, casters, kGroupRezRefusedMs, kRelease).caster, 7u);
}

LEEROY_TEST(GroupRez_AGlobalCooldownRefusalIsRetriedSoon)
{
    // 05:57:00 2346 was refused NOT_READY and cast on another body a second later.
    GroupRezCasters casters;
    GroupRezMemory a, b;
    NoteGroupRezRefused(casters, 2346, 0, true);
    CHECK(DecideGroupRez(Bot(), false, {Rezzer(2346, 5.0f)}, b, casters, kGroupRezRetryMs - 1, kRelease).step
        == GroupRezStep::None);
    CHECK(DecideGroupRez(Bot(), false, {Rezzer(2346, 5.0f)}, b, casters, kGroupRezRetryMs, kRelease).step
        == GroupRezStep::Cast);
}

LEEROY_TEST(GroupRez_RetriesACastOnlyAfterTheGap)
{
    GroupRezMemory m;
    CHECK(Decide(Bot(), false, {Rezzer(7, 5.0f)}, m, 1000).step == GroupRezStep::Cast);
    auto d = Decide(Bot(), false, {Rezzer(7, 5.0f)}, m, 1000 + kGroupRezRetryMs - 1);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("none")); // a wait, not a block
    CHECK(Decide(Bot(), false, {Rezzer(7, 5.0f)}, m, 1000 + kGroupRezRetryMs).step == GroupRezStep::Cast);
}

LEEROY_TEST(GroupRez_ARezzerStillInCombatOrBusyIsNotUsedAndSaysSo)
{
    GroupRezMemory m;
    auto fighting = Rezzer(7, 5.0f);
    fighting.combat = true;
    auto casting = Rezzer(8, 6.0f);
    casting.busy = true;
    auto owned = Rezzer(9, 7.0f);
    owned.fenced = true;
    auto d = Decide(Bot(), false, {fighting, casting, owned}, m, 0);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("caster_combat"));
    CHECK_EQ(Code(Decide(Bot(), false, {casting, owned}, m, 0)), std::string("casters_busy"));
    // A dry rezzer is the more useful reason: it is being asked to drink.
    CHECK_EQ(Code(Decide(Bot(), false, {fighting, Dry(3, 9.0f)}, m, 0)), std::string("all_oom"));
    d = Decide(Bot(), false, {fighting, casting, owned, Rezzer(10, 25.0f)}, m, 0);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 10u);
}

LEEROY_TEST(GroupRez_AFightNearTheBodyWaitsAndRestartsTheReleaseClock)
{
    GroupRezMemory m;
    CHECK(Decide(Bot(), false, {}, m, 0).step == GroupRezStep::None);
    auto d = Decide(Bot(), true, {Rezzer(7, 5.0f)}, m, 50000);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("combat"));
    CHECK(!m.quietSince.has_value());
    CHECK(Decide(Bot(), false, {}, m, 60000).step == GroupRezStep::None);
    CHECK(Decide(Bot(), false, {}, m, 60000 + kRelease - 1).step == GroupRezStep::None);
    CHECK(Decide(Bot(), false, {}, m, 60000 + kRelease).step == GroupRezStep::Release);
}

LEEROY_TEST(GroupRez_AFightOrAFenceStopsAWalkIn)
{
    GroupRezMemory m;
    CHECK(Decide(Bot(), false, {Rezzer(8, 60.0f)}, m, 0).step == GroupRezStep::Approach);
    CHECK(Decide(Bot(), true, {Rezzer(8, 60.0f)}, m, 1000).step == GroupRezStep::None);
    CHECK(!m.approachCaster.has_value());
}

LEEROY_TEST(GroupRez_ReleasesABotAfterAQuietMinuteEvenWithAFailingRezzer)
{
    GroupRezMemory m;
    CHECK(Decide(Bot(), false, {Rezzer(7, 5.0f)}, m, 0).step == GroupRezStep::Cast);
    auto d = Decide(Bot(), false, {Rezzer(7, 5.0f)}, m, kRelease);
    CHECK(d.step == GroupRezStep::Release);
    CHECK_EQ(d.caster, 0u);
}

LEEROY_TEST(GroupRez_ARezUnderWayOrAwaitingAnAnswerHoldsTheReleaseWithoutRestartingIt)
{
    GroupRezMemory m;
    auto body = Bot();
    CHECK(Decide(body, false, {}, m, 0).step == GroupRezStep::None);
    body.rezzing = true;
    auto d = Decide(body, false, {Rezzer(7, 5.0f)}, m, kRelease + 5000);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("none"));
    body.rezzing = false; body.requested = true;
    d = Decide(body, false, {Rezzer(7, 5.0f)}, m, kRelease + 9000);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("awaiting_answer"));
    body.requested = false;
    CHECK(Decide(body, false, {}, m, kRelease + 10000).step == GroupRezStep::Release);
}

LEEROY_TEST(GroupRez_NeverReleasesTheOwnerOrInsideAnInstanceOrWhenReleaseIsOff)
{
    GroupRezBody owner;
    GroupRezMemory a;
    Decide(owner, false, {}, a, 0);
    CHECK(Decide(owner, false, {}, a, 10 * kRelease).step == GroupRezStep::None);
    auto d = Decide(owner, false, {Rezzer(7, 5.0f)}, a, 10 * kRelease + 1);
    CHECK(d.step == GroupRezStep::Cast); // he still gets a rez offer

    auto raid = Bot();
    raid.instance = true;
    GroupRezMemory b;
    Decide(raid, false, {}, b, 0);
    CHECK(Decide(raid, false, {}, b, 10 * kRelease).step == GroupRezStep::None);
    CHECK(Decide(raid, false, {Rezzer(7, 90.0f)}, b, 10 * kRelease).step == GroupRezStep::Approach);

    GroupRezMemory c;
    Decide(Bot(), false, {}, c, 0, 0);
    CHECK(Decide(Bot(), false, {}, c, 10 * kRelease, 0).step == GroupRezStep::None);
}

LEEROY_TEST(GroupRez_SelfResWaitsForTheBotButStillReleasesWhenItDoesNotUseIt)
{
    GroupRezMemory m;
    auto body = Bot();
    body.selfRes = true;
    auto d = Decide(body, false, {Rezzer(7, 5.0f)}, m, 0);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("self_res"));
    CHECK(Decide(body, false, {Rezzer(7, 5.0f)}, m, kRelease).step == GroupRezStep::Release);
}

LEEROY_TEST(GroupRez_AFencedBodyIsLeftAlone)
{
    GroupRezMemory m;
    auto body = Bot();
    body.fenced = true;
    auto d = Decide(body, false, {Rezzer(7, 5.0f)}, m, 0);
    CHECK(d.step == GroupRezStep::None);
    CHECK_EQ(Code(d), std::string("fenced"));
    CHECK(Decide(body, true, {Rezzer(7, 5.0f)}, m, 10 * kRelease).step == GroupRezStep::None);
    CHECK_EQ(Code(Decide(body, true, {Rezzer(7, 5.0f)}, m, 10 * kRelease)), std::string("fenced"));
    CHECK(!m.quietSince.has_value());
}

LEEROY_TEST(GroupRez_TiesGoToTheLowestGuid)
{
    GroupRezMemory m;
    CHECK_EQ(Decide(Bot(), false, {Rezzer(9, 10.0f), Rezzer(4, 10.0f)}, m, 0).caster, 4u);
    GroupRezMemory walk;
    CHECK_EQ(Decide(Bot(), false, {Rezzer(9, 50.0f), Rezzer(4, 50.0f)}, walk, 0).caster, 4u);
    GroupRezCasters casters;
    GroupRezMemory drink;
    CHECK_EQ(DecideGroupRez(Bot(), false, {Dry(9, 10.0f), Dry(4, 10.0f)}, drink, casters, 0, kRelease).caster, 4u);
}

LEEROY_TEST(GroupRez_ABlockIsLoggedOncePerReasonAMinute)
{
    std::map<GroupRezBlock, std::uint64_t> logged;
    CHECK(GroupRezBlockLogDue(logged, GroupRezBlock::AllOom, 1000));
    CHECK(!GroupRezBlockLogDue(logged, GroupRezBlock::AllOom, 1000 + kGroupRezBlockLogMs - 1));
    CHECK(GroupRezBlockLogDue(logged, GroupRezBlock::Combat, 2000));
    CHECK(GroupRezBlockLogDue(logged, GroupRezBlock::AllOom, 1000 + kGroupRezBlockLogMs));
    CHECK(!GroupRezBlockLogDue(logged, GroupRezBlock::None, 5000));
}

LEEROY_TEST(GroupRez_BlockCodesAreStable)
{
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::Combat)), std::string("combat"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::Fenced)), std::string("fenced"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::AwaitingAnswer)), std::string("awaiting_answer"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::SelfRes)), std::string("self_res"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::NoCaster)), std::string("no_caster"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::NoCasterInRange)), std::string("no_caster_in_range"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::AllOom)), std::string("all_oom"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::Unreachable)), std::string("unreachable"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::CasterCombat)), std::string("caster_combat"));
    CHECK_EQ(std::string(GroupRezBlockCode(GroupRezBlock::CastersBusy)), std::string("casters_busy"));
}

LEEROY_TEST(GroupRez_CasterMemoryIsPrunedOnceNothingIsLeft)
{
    GroupRezCasters casters;
    NoteGroupRezRefused(casters, 7, 0, false);
    NoteGroupRezDrinkAsked(casters, 8, 0);
    PruneGroupRezCasters(casters, kGroupRezDrinkRetryMs);
    CHECK(casters.contains(7));
    CHECK(!casters.contains(8));
    PruneGroupRezCasters(casters, kGroupRezRefusedMs);
    CHECK(casters.empty());
}
