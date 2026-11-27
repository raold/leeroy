// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyHazardPolicy.h"
#include "TestHarness.h"

#include <algorithm>
#include <cmath>
#include <optional>
#include <random>
#include <vector>

using namespace Leeroy::Guild;

namespace
{
constexpr float kStockCap = 15.0f;
constexpr float kReach = 0.5f;

HazardMemberFacts Member()
{
    HazardMemberFacts f;
    f.enabled = f.cast = f.bot = f.ownerGroup = f.ownerMaster = true;
    return f;
}

HazardHold Hold(bool journaled, bool present, bool sameLife = true)
{
    HazardHold h;
    h.journaled = journaled;
    h.present = present;
    h.sameLife = sameLife;
    return h;
}

float Dist(HazardPoint a, HazardPoint b) { return std::hypot(a.x - b.x, a.y - b.y); }

HazardSpellIds Ids(std::uint32_t spell, std::uint32_t base = 0, std::uint32_t auraBaseTrigger = 0)
{
    return {spell, base, auraBaseTrigger};
}
}

LEEROY_TEST(Hazard_StockSizedAreasKeepTheirRadiusUpToTheCap)
{
    CHECK_EQ(HazardAvoidRadius(Ids(28794), 10.0f, 0.0f, kStockCap, false), 10.0f); // Faerlina's Rain of Fire
    CHECK_EQ(HazardAvoidRadius(Ids(1), 15.0f, 0.0f, kStockCap, false), 15.0f);
    CHECK_EQ(HazardAvoidRadius(Ids(1), 15.5f, 0.0f, kStockCap, false), 0.0f);
    CHECK_EQ(HazardAvoidRadius(Ids(1), 0.0f, 0.0f, kStockCap, false), 0.0f); // no radius, as stock
    CHECK_EQ(HazardAvoidRadius(Ids(1), 8.0f, 0.0f, 5.0f, false), 0.0f);      // a lower configured cap still holds
}

LEEROY_TEST(Hazard_TheStockWhitelistIsNeverAvoided)
{
    CHECK_EQ(HazardAvoidRadius(Ids(57491), 7.0f, 0.0f, kStockCap, true), 0.0f); // Flame Tsunami
    CHECK_EQ(HazardAvoidRadius(Ids(69789), 25.0f, 0.0f, kStockCap, true), 0.0f);
}

LEEROY_TEST(Hazard_OnlyTheMeasuredEscapableLargeHazardsPassTheCap)
{
    CHECK(LargeHazardRule(69789) == LargeHazardRadius::Spell);
    CHECK(LargeHazardRule(71588) == LargeHazardRadius::Spell);
    CHECK(LargeHazardRule(28241) == LargeHazardRadius::CasterReach);
    CHECK_EQ(HazardAvoidRadius(Ids(69789), 25.0f, 1.5f, kStockCap, false), 25.0f); // Ooze Flood
    // Out of the band: boss-centred, a vehicle phase, a jet line, a sweeping orb.
    for (std::uint32_t spell : {30122u, 61886u, 61878u, 62553u, 64168u, 57429u, 70461u, 62017u, 62392u})
    {
        CHECK(!LargeHazardRule(spell));
        CHECK_EQ(HazardAvoidRadius(Ids(spell), 25.0f, 1.0f, kStockCap, false), 0.0f);
    }
}

LEEROY_TEST(Hazard_RaidWideEffectsAreNeverFledEvenWhenAllowListed)
{
    CHECK_EQ(HazardAvoidRadius(Ids(68983), 45.0f, 0.0f, kStockCap, false), 0.0f); // Remorseless Winter
    CHECK_EQ(HazardAvoidRadius(Ids(72754), 200.0f, 0.0f, kStockCap, false), 0.0f); // Defile, scripted size
    CHECK_EQ(HazardAvoidRadius(Ids(69789), 36.0f, 0.0f, kStockCap, false), 0.0f);
    CHECK_EQ(HazardAvoidRadius(Ids(69789), 35.0f, 0.0f, kStockCap, false), 35.0f);
}

LEEROY_TEST(Hazard_GrobbulusCloudUsesItsGrowingReachNotTheSpellRadius)
{
    CHECK_EQ(HazardAvoidRadius(Ids(28241), 30.0f, 2.0f, kStockCap, false), 2.0f);  // a fresh cloud
    CHECK_EQ(HazardAvoidRadius(Ids(28241), 30.0f, 17.0f, kStockCap, false), 17.0f); // a minute later
    CHECK_EQ(HazardAvoidRadius(Ids(28241), 30.0f, 0.0f, kStockCap, false), 0.0f);
}

LEEROY_TEST(Hazard_InsideMeansWithinRadiusPlusReachOnTheSameFloor)
{
    HazardZone const zone{0, 0, 0, 8};
    CHECK(InHazard({8.4f, 0, 0}, zone, kReach));
    CHECK(!InHazard({8.6f, 0, 0}, zone, kReach));
    CHECK(InHazard({0, 0, 7.9f}, zone, kReach));
    CHECK(!InHazard({0, 0, 8.5f}, zone, kReach)); // the floor above
    std::vector<HazardZone> zones{{50, 50, 0, 5}, zone};
    CHECK(HazardAt({1, 1, 0}, zones, kReach) == std::size_t(1));
    CHECK(!HazardAt({20, 0, 0}, zones, kReach));
}

LEEROY_TEST(Hazard_ClearNeedsTheMarginBeyondTheEdge)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8}};
    CHECK(!HazardClear({9.0f, 0, 0}, zones, kReach)); // outside, but inside the margin
    CHECK(HazardClear({10.1f, 0, 0}, zones, kReach));
    CHECK(HazardClear({0, 0, 20}, zones, kReach));
    CHECK(HazardClear({0, 0, 0}, {}, kReach));
}

LEEROY_TEST(Hazard_AClearSpotIsLeftAlone)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8}};
    CHECK(HazardSafeSpots({30, 0, 0}, {30, 0, 0}, zones, kReach, 4).empty());
    CHECK(HazardSafeSpots({0, 0, 0}, {0, 0, 0}, {}, kReach, 4).empty());
    CHECK(HazardSafeSpots({0, 0, 0}, {0, 0, 0}, zones, kReach, 0).empty());
}

LEEROY_TEST(Hazard_ASpotInOneZoneMovesStraightOutPastTheMargin)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8}};
    auto spots = HazardSafeSpots({4, 0, 0}, {4, 0, 0}, zones, kReach, 3);
    CHECK(!spots.empty());
    CHECK(spots.front().x >= 9.99f && spots.front().x <= 10.11f); // radius + reach + margin, straight out
    CHECK(std::fabs(spots.front().y) < 0.01f);
    CHECK(HazardClear(spots.front(), zones, kReach));
    for (std::size_t i = 1; i < spots.size(); ++i) // nearest first
        CHECK(Dist(spots[i - 1], {4, 0, 0}) <= Dist(spots[i], {4, 0, 0}));
}

LEEROY_TEST(Hazard_DeadCentreLeavesTowardTheMaster)
{
    std::vector<HazardZone> zones{{10, 10, 0, 6}};
    auto spots = HazardSafeSpots({10, 10, 0}, {10, -20, 0}, zones, kReach, 1);
    CHECK_EQ(spots.size(), std::size_t(1));
    CHECK(spots.front().y < 10.0f - 6.0f);
    CHECK(std::fabs(spots.front().x - 10.0f) < 0.01f);
}

LEEROY_TEST(Hazard_OverlappingZonesFindASpotClearOfBoth)
{
    // Two touching pools with the spot between them: neither straight exit is clear of the other.
    std::vector<HazardZone> zones{{-5, 0, 0, 6}, {5, 0, 0, 6}};
    auto spots = HazardSafeSpots({0, 0, 0}, {0, -30, 0}, zones, kReach, 4);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
    {
        CHECK(HazardClear(spot, zones, kReach));
        CHECK(Dist(spot, {0, 0, 0}) <= kHazardMaxShiftYards);
    }
    CHECK(spots.front().y < 0.0f); // equal distances break toward the master
}

LEEROY_TEST(Hazard_TheLargestAllowedZoneStillHasAnExitWithinTheShift)
{
    std::vector<HazardZone> zones{{0, 0, 0, kHazardLargeCapYards}};
    auto spots = HazardSafeSpots({0, 0, 0}, {1, 0, 0}, zones, 1.5f, 1);
    CHECK_EQ(spots.size(), std::size_t(1));
    CHECK(Dist(spots.front(), {0, 0, 0}) <= kHazardMaxShiftYards);
}

LEEROY_TEST(Hazard_NoSpotWhenEverythingNearIsCovered)
{
    std::vector<HazardZone> zones;
    for (int x = -60; x <= 60; x += 10)
        for (int y = -60; y <= 60; y += 10)
            zones.push_back({float(x), float(y), 0, 8});
    CHECK(HazardSafeSpots({0, 0, 0}, {0, 0, 0}, zones, kReach, 4).empty());
}

LEEROY_TEST(Hazard_OnlyHisGroupsCastBotsWithHimAsMasterQualify)
{
    CHECK(HazardEligible(Member()));
    CHECK(HazardAware(Member()));
    for (auto field : {&HazardMemberFacts::enabled, &HazardMemberFacts::cast, &HazardMemberFacts::bot,
             &HazardMemberFacts::ownerGroup, &HazardMemberFacts::ownerMaster})
    {
        auto f = Member();
        f.*field = false;
        CHECK(!HazardEligible(f));
        CHECK(!HazardAware(f));
    }
    auto fenced = Member();
    fenced.fenced = true;
    CHECK(HazardEligible(fenced));
    CHECK(!HazardAware(fenced));
}

LEEROY_TEST(Hazard_AddsOnlyWhereAbsentAndNeverTakesOverAnotherOwnersStrategy)
{
    CHECK(DecideHazardStrategy(Member(), Hold(false, false)) == HazardStep::Add);
    CHECK(DecideHazardStrategy(Member(), Hold(false, true)) == HazardStep::None); // someone else added it
    CHECK(DecideHazardStrategy(Member(), Hold(true, true)) == HazardStep::None);
    auto off = Member();
    off.enabled = false;
    CHECK(DecideHazardStrategy(off, Hold(false, true)) == HazardStep::None); // not ours to remove
    CHECK(DecideHazardStrategy(off, Hold(false, false)) == HazardStep::None);
}

LEEROY_TEST(Hazard_RemovesItsOwnAdditionWhenTheMemberNoLongerQualifies)
{
    for (auto field : {&HazardMemberFacts::enabled, &HazardMemberFacts::cast, &HazardMemberFacts::bot,
             &HazardMemberFacts::ownerGroup, &HazardMemberFacts::ownerMaster})
    {
        auto f = Member();
        f.*field = false;
        CHECK(DecideHazardStrategy(f, Hold(true, true)) == HazardStep::Remove);
        CHECK(DecideHazardStrategy(f, Hold(true, false)) == HazardStep::Forget);
    }
}

LEEROY_TEST(Hazard_AResetThatDroppedItIsReaddedWhileHeStillQualifies)
{
    CHECK(DecideHazardStrategy(Member(), Hold(true, false)) == HazardStep::Add);
}

LEEROY_TEST(Hazard_AFencedBodyIsLeftToItsOwner)
{
    auto fenced = Member();
    fenced.fenced = true;
    CHECK(DecideHazardStrategy(fenced, Hold(false, false)) == HazardStep::None);
    CHECK(DecideHazardStrategy(fenced, Hold(true, true)) == HazardStep::None);
    fenced.ownerGroup = false;
    CHECK(DecideHazardStrategy(fenced, Hold(true, true)) == HazardStep::None);
}

LEEROY_TEST(Hazard_ANewLifeIsForgottenNeverWrittenTo)
{
    CHECK(DecideHazardStrategy(Member(), Hold(true, true, false)) == HazardStep::Forget);
    auto fenced = Member();
    fenced.fenced = true;
    CHECK(DecideHazardStrategy(fenced, Hold(true, false, false)) == HazardStep::Forget);
}

LEEROY_TEST(Hazard_OffReasonNamesTheFirstMissingCondition)
{
    auto f = Member();
    f.ownerMaster = false;
    CHECK_EQ(HazardOffReason(f), std::string_view("master"));
    f.ownerGroup = false;
    CHECK_EQ(HazardOffReason(f), std::string_view("not_in_group"));
    f.bot = false;
    CHECK_EQ(HazardOffReason(f), std::string_view("not_bot"));
    f.cast = false;
    CHECK_EQ(HazardOffReason(f), std::string_view("not_cast"));
    f.enabled = false;
    CHECK_EQ(HazardOffReason(f), std::string_view("off"));
}

LEEROY_TEST(Hazard_Grobbulus25ManCloudIsCoveredDirectlyAndThroughItsAura)
{
    CHECK(LargeHazardRule(54363) == LargeHazardRadius::CasterReach);
    CHECK_EQ(HazardAvoidRadius(Ids(54363), 30.0f, 6.0f, kStockCap, false), 6.0f);
    // An unlisted damage spell whose aura's 10-man twin triggers an allowed one.
    CHECK_EQ(HazardAvoidRadius(Ids(999001, 0, 28241), 30.0f, 6.0f, kStockCap, false), 6.0f);
}

LEEROY_TEST(Hazard_DifficultyVersionsFollowTheirFirstVersion)
{
    CHECK(LargeHazardRule(Ids(999002, 69789)) == LargeHazardRadius::Spell);
    CHECK_EQ(HazardAvoidRadius(Ids(999002, 69789), 25.0f, 1.0f, kStockCap, false), 25.0f);
    CHECK(!LargeHazardRule(Ids(999002, 999003, 999004)));
    CHECK(!LargeHazardRule(Ids(0, 0, 0)));
    CHECK_EQ(HazardAvoidRadius(Ids(999002, 69789), 25.0f, 1.0f, kStockCap, true), 0.0f); // whitelisted stays out
}

LEEROY_TEST(Hazard_TriggerZonesAddTheNpcsReachAsStockDoesUnlessThatReachIsTheRadius)
{
    CHECK_EQ(HazardTriggerZoneRadius(Ids(1), 10.0f, 1.5f, kStockCap, false), 11.5f);
    CHECK_EQ(HazardTriggerZoneRadius(Ids(1), 16.0f, 1.5f, kStockCap, false), 0.0f); // the cap is on the spell
    CHECK_EQ(HazardTriggerZoneRadius(Ids(69789), 25.0f, 1.5f, kStockCap, false), 26.5f);
    CHECK_EQ(HazardTriggerZoneRadius(Ids(28241), 30.0f, 9.0f, kStockCap, false), 9.0f);
    CHECK_EQ(HazardTriggerZoneRadius(Ids(1), 10.0f, 1.5f, kStockCap, true), 0.0f);
}

LEEROY_TEST(Hazard_ASegmentIsClearOnlyIfItNeverComesWithinRadiusPlusReach)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8}};
    CHECK(!HazardSegmentClear({-20, 0, 0}, {20, 0, 0}, zones, kReach));
    CHECK(HazardSegmentClear({-20, 9, 0}, {20, 9, 0}, zones, kReach));
    CHECK(!HazardSegmentClear({-20, 8.4f, 0}, {20, 8.4f, 0}, zones, kReach));
    CHECK(HazardSegmentClear({-20, 0, 0}, {-12, 0, 0}, zones, kReach)); // stops short
    CHECK(HazardSegmentClear({-20, 0, 20}, {20, 0, 20}, zones, kReach)); // the floor above
}

namespace
{
// Walks a bot along detour waypoints as follow would, checking every leg; true when it reaches the goal.
bool WalkRound(HazardPoint bot, HazardPoint goal, std::vector<HazardZone> const& zones, float reach, int& legs)
{
    for (legs = 0; legs < 20; ++legs)
    {
        auto const waypoints = HazardDetour(bot, goal, zones, reach);
        HazardPoint const next = waypoints.empty() ? goal : waypoints.front();
        if (!HazardSegmentClear(bot, next, zones, reach) || HazardAt(next, zones, reach))
            return false;
        bot = next;
        if (Dist(bot, goal) < 0.01f)
            return true;
    }
    return false;
}
}

// Review of #296, P2-1: zone at (0,0) radius 8, reach 1.5; he stands at (10,0) and the bot's arrow spot (4,0)
// is in the zone; the bot comes from (-20,0).
LEEROY_TEST(Hazard_TheReviewsCrossingNeverEntersTheZone)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8, 7}};
    float const reach = 1.5f;
    auto spots = HazardSafeSpots({4, 0, 0}, {-20, 0, 0}, zones, reach, 1);
    CHECK_EQ(spots.size(), std::size_t(1));
    HazardPoint const goal = spots.front();
    CHECK(goal.x > 0.0f); // his side of the zone
    auto const first = HazardDetour({-20, 0, 0}, goal, zones, reach);
    CHECK_EQ(first.size(), std::size_t(2)); // round either way
    CHECK(Dist(first.front(), goal) < Dist({-20, 0, 0}, goal));
    int legs = 0;
    CHECK(WalkRound({-20, 0, 0}, goal, zones, reach, legs));
    CHECK(legs >= 2 && legs <= 6);
}

LEEROY_TEST(Hazard_HeAlreadyCrossedAndHisSpotIsClearButTheLineIsNot)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8, 7}};
    HazardPoint const goal{15, 1, 0}; // clear, beyond the zone
    CHECK(HazardClear(goal, zones, 1.5f));
    CHECK(HazardSafeSpots(goal, goal, zones, 1.5f, 1).empty());
    CHECK(!HazardDetour({-20, 0, 0}, goal, zones, 1.5f).empty());
    int legs = 0;
    CHECK(WalkRound({-20, 0, 0}, goal, zones, 1.5f, legs));
}

LEEROY_TEST(Hazard_NoDetourForAClearLineOrABotStandingInAZone)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8, 7}};
    CHECK(HazardDetour({-20, 12, 0}, {20, 12, 0}, zones, 1.5f).empty());
    CHECK(HazardDetour({-5, 0, 0}, {20, 0, 0}, zones, 1.5f).empty()); // stepping out comes first
    CHECK(HazardDetour({-20, 0, 0}, {20, 0, 0}, {}, 1.5f).empty());
}

LEEROY_TEST(Hazard_TheDetourGoesRoundTheFirstZoneAndAvoidsTheOthers)
{
    // A second zone sits right above the first: the way round over the top is shut, the bottom is open.
    std::vector<HazardZone> zones{{0, 0, 0, 6, 1}, {0, 14, 0, 6, 2}, {40, 0, 0, 3, 3}};
    auto const waypoints = HazardDetour({-20, 0, 0}, {20, 0, 0}, zones, 1.0f);
    CHECK(!waypoints.empty());
    for (auto const& w : waypoints)
    {
        CHECK(HazardClear(w, zones, 1.0f));
        CHECK(HazardSegmentClear({-20, 0, 0}, w, zones, 1.0f));
    }
    int legs = 0;
    CHECK(WalkRound({-20, 0, 0}, {20, 0, 0}, zones, 1.0f, legs));
}

LEEROY_TEST(Hazard_JustInsideTheNearEdgeStepsBackAndThenGoesRound)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8, 7}};
    float const reach = 1.5f;
    HazardPoint const goal{11.1f, 0, 0};
    auto const exits = HazardExitSpots({-9.5f, 0, 0}, goal, {10, 0, 0}, zones, reach, 3);
    CHECK(!exits.empty());
    CHECK(exits.front().x < -10.9f); // 1.6 yards back beats 20 yards through
    int legs = 0;
    CHECK(WalkRound(exits.front(), goal, zones, reach, legs)); // follow then never re-enters: no loop
}

LEEROY_TEST(Hazard_PastTheMiddleTheExitFinishesTheCrossingTowardHisSide)
{
    std::vector<HazardZone> zones{{0, 0, 0, 8, 7}};
    float const reach = 1.5f;
    HazardPoint const goal{11.1f, 0, 0};
    // Straight back out is nearer (10.1 vs 12 yards), but the goal is in plain view only from his side.
    auto const exits = HazardExitSpots({-1, 0, 0}, goal, {10, 0, 0}, zones, reach, 3);
    CHECK(!exits.empty());
    CHECK(exits.front().x > 0.0f);
    CHECK(HazardSegmentClear(exits.front(), goal, zones, reach));
    // Without a goal (in combat) the nearest exit wins.
    auto const nearest = HazardExitSpots({-1, 0, 0}, std::nullopt, {10, 0, 0}, zones, reach, 1);
    CHECK(nearest.front().x < 0.0f);
}

LEEROY_TEST(Hazard_AnExitLoopWithAnotherMoverYieldsOnTheThirdEntry)
{
    HazardExitGuard guard;
    std::vector<std::uint64_t> const seven{7};
    CHECK(NoteHazardEntry(guard, seven, 1000) == HazardEntry::Exit);
    CHECK(!HazardExitYielded(guard, seven, 1000));
    CHECK(NoteHazardEntry(guard, seven, 3000) == HazardEntry::Exit);
    CHECK(NoteHazardEntry(guard, seven, 5000) == HazardEntry::YieldStarts); // a mover keeps walking it back in
    CHECK(HazardExitYielded(guard, seven, 5000));
    CHECK(HazardExitYielded(guard, seven, 5000 + kHazardYieldMs - 1));
    CHECK(NoteHazardEntry(guard, seven, 6000) == HazardEntry::Yielded);
    CHECK(!HazardExitYielded(guard, {8}, 6000)); // another hazard is still stepped out of
    CHECK(!HazardExitYielded(guard, {7, 8}, 6000)); // so is a spot where it overlaps a yielded one
    CHECK(!HazardExitYielded(guard, seven, 5000 + kHazardYieldMs));
    CHECK(NoteHazardEntry(guard, seven, 5000 + kHazardYieldMs + 1) == HazardEntry::Exit); // a fresh window
    CHECK(!HazardExitYielded(guard, {}, 0));
}

LEEROY_TEST(Hazard_SlowEntriesNeverYield)
{
    HazardExitGuard guard;
    std::vector<std::uint64_t> const seven{7};
    CHECK(NoteHazardEntry(guard, seven, 0) == HazardEntry::Exit);
    CHECK(NoteHazardEntry(guard, seven, kHazardExitWindowMs - 1) == HazardEntry::Exit);
    CHECK(NoteHazardEntry(guard, seven, 3 * kHazardExitWindowMs) == HazardEntry::Exit); // counting again
    CHECK(NoteHazardEntry(guard, seven, 4 * kHazardExitWindowMs + 1) == HazardEntry::Exit);
    CHECK(!HazardExitYielded(guard, seven, 4 * kHazardExitWindowMs + 2));
}

LEEROY_TEST(Hazard_GrowingCloudOvertakesAreNotRepeatedWalksBackIntoIt)
{
    HazardExitGuard guard, oldGuard;
    HazardZone cloud{0, 0, 0, 2, 7};
    HazardPoint at{5, 0, 0};
    float const reach = 1.0f;
    NoteHazardPosition(guard, {cloud}, at, reach, 0);
    HazardEntry oldAnswer = HazardEntry::Exit;
    for (std::uint64_t now : {8000u, 15000u, 22000u})
    {
        // The core's Grobbulus cloud grows 0.00025 yards/ms. The bot waits at its safe exit;
        // each later contact comes from growth, without walking toward the cloud at all.
        cloud.radius = 2.0f + 0.00025f * now;
        CHECK(InHazard(at, cloud, reach));
        oldAnswer = NoteHazardZoneEntry(oldGuard, {cloud}, now);
        CHECK(NoteHazardPosition(guard, {cloud}, at, reach, now) == HazardEntry::Exit);
        at.x = cloud.radius + reach + kHazardMarginYards + 0.1f;
        CHECK(!InHazard(at, cloud, reach));
        oldGuard.standing.clear();
        NoteHazardPosition(guard, {cloud}, at, reach, now + 1);
    }
    CHECK(oldAnswer == HazardEntry::YieldStarts); // reproduces the old false loop diagnosis
    CHECK(!HazardExitYielded(guard, {7}, 22000));
}

LEEROY_TEST(Hazard_RealReturnsStillYieldEvenWhileTheCloudGrows)
{
    HazardExitGuard guard;
    HazardZone cloud{0, 0, 0, 2, 7};
    for (std::uint64_t now : {1000u, 3000u, 5000u})
    {
        NoteHazardPosition(guard, {cloud}, {20, 0, 0}, 1.0f, now - 1);
        cloud.radius += 0.5f;
        auto const answer = NoteHazardPosition(guard, {cloud}, {0, 0, 0}, 1.0f, now);
        CHECK(answer == (now == 5000 ? HazardEntry::YieldStarts : HazardEntry::Exit));
    }
    CHECK(HazardExitYielded(guard, {7}, 5000));
    CHECK(NoteHazardPosition(guard, {cloud}, {0, 0, 0}, 1.0f, 6000) == HazardEntry::Yielded);
    // A new danger overlapping the yielded cloud must still be escaped.
    CHECK(NoteHazardPosition(guard, {cloud, HazardZone{0, 0, 0, 1, 8}}, {0, 0, 0}, 1.0f, 7000)
        == HazardEntry::Exit);
    NoteHazardPosition(guard, {}, {20, 0, 0}, 1.0f, 8000);
    CHECK(guard.observed.empty() && guard.standing.empty());
}

LEEROY_TEST(Hazard_SourceCodesAndMotionPreserveMeasuredIdentity)
{
    HazardZone zone{0, 0, 0, 2, 7};
    zone.spell = 54363;
    zone.entry = 16363;
    zone.kind = HazardKind::Trigger;
    auto const led = HazardLeadZone(zone, HazardPoint{10, 0, 0}, 2.0f);
    CHECK_EQ(led.spell, zone.spell);
    CHECK_EQ(led.entry, zone.entry);
    CHECK(HazardKindCode(led.kind) == "trigger");
    CHECK(HazardKindCode(HazardKind::Dynamic) == "dynamic");
    CHECK(HazardKindCode(HazardKind::Trap) == "trap");
    CHECK(HazardKindCode(HazardKind::HeldAura) == "held_aura");
    CHECK(HazardKindCode(HazardKind::Proximity) == "proximity");
    CHECK(HazardKindCode(static_cast<HazardKind>(255)) == "unknown");
}

// Re-review of #296, R-P3-1: a route through two overlapping hazards enters them by turns; each is counted on
// its own, so both yield and the bot is let through.
LEEROY_TEST(Hazard_OverlappingHazardsEnteredByTurnsBothYield)
{
    HazardExitGuard guard;
    std::uint64_t now = 1000;
    std::vector<std::vector<std::uint64_t>> const turns{{2}, {1}, {2, 1}, {1}, {2}, {1, 2}};
    std::vector<HazardEntry> seen;
    for (auto const& inside : turns)
        seen.push_back(NoteHazardEntry(guard, inside, now += 1000));
    CHECK(seen[0] == HazardEntry::Exit);
    CHECK(seen[1] == HazardEntry::Exit);
    CHECK(seen[2] == HazardEntry::Exit);
    CHECK(seen[3] == HazardEntry::YieldStarts); // zone 1's third entry
    CHECK(seen[4] == HazardEntry::YieldStarts); // zone 2's third entry
    CHECK(seen[5] == HazardEntry::Yielded);
    CHECK(HazardExitYielded(guard, {1, 2}, now));
}

// Re-review of #296, R-P2-1: a second zone shuts the shorter way round a degree or two from the bot, so the
// farthest clear step there is its own spot. It must be skipped for the other way round.
LEEROY_TEST(Hazard_ADetourNeverStallsOnTheBotsOwnSpot)
{
    std::vector<HazardZone> zones{{-20.4f, 18.0f, 0, 9.3f, 1}, {-29.8f, 18.5f, 0, 10.6f, 2}};
    HazardPoint const from{-22.1f, 30.83f, 0};
    HazardPoint const goal{-21.2f, -5.0f, 0};
    for (float reach : {0.5f, 1.0f, 1.5f})
    {
        for (auto const& w : HazardDetour(from, goal, zones, reach))
            CHECK(Dist(from, w) >= kHazardDetourMinStepYards);
        int legs = 0;
        WalkRound(from, goal, zones, reach, legs);
        CHECK(legs < 20);
    }
}

namespace
{
// A fixed generator, so the layouts are the same on every compiler.
struct Layouts
{
    std::uint32_t state;
    float Next(float low, float high)
    {
        state = state * 1664525u + 1013904223u;
        return low + (high - low) * static_cast<float>(state >> 8) / static_cast<float>(1u << 24);
    }
};

enum class Walk { Reached, Stalled, Wandered };

// Follow as the filter drives it: a waypoint is kept until the bot is within `step` of it, a new one comes from
// HazardDetour, and with none the bot walks straight at its goal; 0.7 yards a tick. Stalled means follow would
// stand still short of the goal: playerbots walks only while the distance from the bot's edge (centre distance
// less its reach) exceeds the 1.5-yard follow distance.
Walk FollowWalk(HazardPoint bot, HazardPoint goal, std::vector<HazardZone> const& zones, float reach, float step,
    int& noProgress)
{
    std::optional<HazardPoint> kept;
    HazardDetourTrack track;
    for (int tick = 0; tick < 4000; ++tick)
    {
        if (Dist(bot, goal) - reach <= 1.5f)
            return Walk::Reached;
        if (kept && (Dist(bot, *kept) <= step || !HazardSegmentClear(bot, *kept, zones, reach)))
            kept.reset();
        if (!kept && !HazardSegmentClear(bot, goal, zones, reach) && !HazardAt(bot, zones, reach)
            && HazardDetourAllowed(track, bot, goal))
        {
            auto const waypoints = HazardDetour(bot, goal, zones, reach, step);
            if (!waypoints.empty())
            {
                kept = waypoints.front();
                if (Dist(bot, *kept) < step - 0.001f)
                    ++noProgress;
            }
        }
        HazardPoint const target = kept.value_or(goal);
        float const left = Dist(bot, target);
        if (left - reach <= 1.5f)
            return Walk::Stalled;
        float const move = std::min(0.7f, left);
        bot.x += (target.x - bot.x) / left * move;
        bot.y += (target.y - bot.y) / left * move;
    }
    return Walk::Wandered;
}
}

LEEROY_TEST(Hazard_RandomFieldsNeverStallAFollower)
{
    Layouts random{20261114u};
    int reached = 0, stalled = 0, wandered = 0, noProgress = 0, runs = 0;
    for (int layout = 0; layout < 4000; ++layout)
    {
        std::vector<HazardZone> zones;
        int const count = 1 + static_cast<int>(random.Next(0.0f, 3.999f));
        for (int i = 0; i < count; ++i)
            zones.push_back({random.Next(-30, 30), random.Next(-30, 30), 0, random.Next(2, 12),
                static_cast<std::uint64_t>(i + 1)});
        float const reach = random.Next(0.4f, 1.5f);
        auto const clearPoint = [&]() -> std::optional<HazardPoint>
        {
            for (int tries = 0; tries < 50; ++tries)
            {
                HazardPoint const p{random.Next(-50, 50), random.Next(-50, 50), 0};
                if (HazardClear(p, zones, reach))
                    return p;
            }
            return std::nullopt;
        };
        auto const start = clearPoint();
        auto const goal = clearPoint();
        if (!start || !goal || Dist(*start, *goal) < 10.0f)
            continue;
        ++runs;
        switch (FollowWalk(*start, *goal, zones, reach, HazardDetourStep(1.5f, reach), noProgress))
        {
            case Walk::Reached: ++reached; break;
            case Walk::Stalled: ++stalled; break;
            case Walk::Wandered: ++wandered; break;
        }
    }
    CHECK(runs > 3000);
    CHECK_EQ(noProgress, 0);
    CHECK_EQ(stalled, 0);
    CHECK_EQ(wandered, 0);
    CHECK_EQ(reached, runs);
}

LEEROY_TEST(Hazard_DetoursGiveUpOnlyWhenTheyStopGainingOnTheGoal)
{
    HazardDetourTrack track;
    HazardPoint const goal{100, 0, 0};
    CHECK(HazardDetourAllowed(track, {0, 0, 0}, goal));
    for (int leg = 1; leg <= 10; ++leg) // steady gains never give up
        CHECK(HazardDetourAllowed(track, {static_cast<float>(leg) * 5.0f, 3, 0}, goal));
    for (std::uint32_t leg = 0; leg < kHazardDetourIdleLegs; ++leg) // circling at the same distance
        CHECK(HazardDetourAllowed(track, {50, leg % 2 ? 4.0f : -4.0f, 0}, goal));
    CHECK(!HazardDetourAllowed(track, {50, 4, 0}, goal));
    CHECK(!HazardDetourAllowed(track, {10, 0, 0}, goal)); // given up for this goal
    CHECK(HazardDetourAllowed(track, {50, 4, 0}, {100, 10, 0})); // he moved: a fresh start
}

// A pocket between four hazards from the random fields: the greedy waypoints alternated for ever.
LEEROY_TEST(Hazard_APocketBetweenHazardsEndsInAStraightWalk)
{
    std::vector<HazardZone> zones{{6.8f, -20.5f, 0, 6.1f, 1}, {-4.4f, 11.7f, 0, 7.1f, 2}, {5.4f, -7.1f, 0, 10.3f, 3},
        {-12.3f, -15.7f, 0, 7.4f, 4}};
    int noProgress = 0;
    CHECK(FollowWalk({-23.7f, 31.1f, 0}, {41.0f, -41.7f, 0}, zones, 1.33f, HazardDetourStep(1.5f, 1.33f),
        noProgress) == Walk::Reached);
    CHECK_EQ(noProgress, 0);
}

LEEROY_TEST(Hazard_DetourStepsClearFollowsOwnStopDistance)
{
    CHECK_EQ(HazardDetourStep(1.5f, 1.5f), 3.5f); // a player: 1.5 follow distance from his 1.5-yard edge
    CHECK_EQ(HazardDetourStep(0.5f, 0.4f), kHazardDetourMinStepYards);
    CHECK_EQ(HazardDetourStep(3.0f, 1.0f), 4.5f);
}

// Re-review 2 of #296: a first waypoint 2.7 yards off cleared the old 2.5-yard step but not follow's stop
// distance (1.5 beyond the bot's 1.5-yard reach), so follow never walked to it.
LEEROY_TEST(Hazard_AWaypointInsideFollowsStopDistanceIsNeverTaken)
{
    std::vector<HazardZone> zones{{3.8f, -13.3f, 0, 10.3f, 1}, {-28.5f, 18.3f, 0, 11.5f, 2},
        {-10.4f, 15.1f, 0, 11.3f, 3}, {-9.6f, 4.4f, 0, 6.2f, 4}};
    HazardPoint const from{-14.5f, 29.1f, 0};
    HazardPoint const goal{2.6f, -20.1f, 0};
    float const reach = 1.5f;
    float const step = HazardDetourStep(1.5f, reach);
    for (auto const& w : HazardDetour(from, goal, zones, reach, step))
        CHECK(Dist(from, w) - reach > 1.5f);
    int noProgress = 0;
    CHECK(FollowWalk(from, goal, zones, reach, step, noProgress) == Walk::Reached);
    CHECK_EQ(noProgress, 0);
}

// Ruby Sanctum, heroic 25-man Halion: a Living Inferno is a selectable add, so stock's trigger npc case never sees
// its Blazing Aura (75885 -> 75886, 75887 on 25-man, 15 yards).
LEEROY_TEST(Hazard_OnlyTheMeasuredAddsBurningAurasAreHeld)
{
    CHECK(HeldAuraHazard(Ids(75886, 75886)));
    CHECK(HeldAuraHazard(Ids(75887, 75886))); // the 25-man tick by its 10-man twin
    CHECK(HeldAuraHazard(Ids(75887)));
    CHECK(!HeldAuraHazard(Ids(74717, 74717))); // a meteor flame: a trigger npc, stock's own case
    CHECK(!HeldAuraHazard(Ids(69789, 69789))); // Ooze Flood: a large hazard, not an add's aura
    CHECK(!HeldAuraHazard(Ids(0)));
    CHECK_EQ(HazardHeldAuraRadius(Ids(75887, 75886), 15.0f, 1.5f, false), 16.5f); // its own reach counts
    CHECK_EQ(HazardHeldAuraRadius(Ids(75887, 75886), 15.0f, 1.5f, true), 0.0f);   // whitelisted: never
    CHECK_EQ(HazardHeldAuraRadius(Ids(1234), 15.0f, 1.5f, false), 0.0f);          // any other add's aura
    CHECK_EQ(HazardHeldAuraRadius(Ids(75886), 0.0f, 1.5f, false), 0.0f);
    CHECK_EQ(HazardHeldAuraRadius(Ids(75886), 36.0f, 1.5f, false), 0.0f); // past the large cap
}

LEEROY_TEST(Hazard_ATankHoldsAnAddInItsOwnFire)
{
    HazardZone fire{0, 0, 0, 4.5f, 1};
    HazardZone inferno{10, 0, 0, 16.5f, 2};
    inferno.held = true;
    std::vector<HazardZone> const zones{fire, inferno};
    CHECK(HazardApplies(inferno, 1, false));
    CHECK(!HazardApplies(inferno, 1, true));
    CHECK(HazardApplies(fire, 1, true)); // a tank still keeps out of ground fire
    auto const mine = HazardZonesFor(zones, 1, true);
    CHECK_EQ(mine.size(), std::size_t(1));
    CHECK_EQ(mine.front().id, std::uint64_t(1));
    CHECK_EQ(HazardZonesFor(zones, 1, false).size(), std::size_t(2));

    // A healer beside the inferno steps out of its fire; its tank stays.
    HazardPoint const at{14, 0, 0};
    auto const exits = HazardExitSpots(at, std::nullopt, at, HazardZonesFor(zones, 1, false), kReach, 3);
    CHECK(!exits.empty());
    for (auto const& spot : exits)
        CHECK(Dist(spot, {10, 0, 0}) >= 16.5f + kReach + kHazardMarginYards);
    CHECK(HazardExitSpots(at, std::nullopt, at, HazardZonesFor(zones, 1, true), kReach, 3).empty());
}

LEEROY_TEST(Hazard_AZoneInAnotherPhaseNeverConcernsABody)
{
    // Halion's realms: the physical realm is phase 1, the twilight realm 32; heroic combustions burn in both (33).
    HazardZone physical{0, 0, 0, 3, 1, 1};
    HazardZone twilight{0, 0, 0, 6, 2, 32};
    HazardZone both{20, 0, 0, 6, 3, 33};
    HazardZone any{40, 0, 0, 3, 4, 0};
    std::vector<HazardZone> const zones{physical, twilight, both, any};
    std::vector<std::uint64_t> ids;
    for (auto const& zone : HazardZonesFor(zones, 32, false))
        ids.push_back(zone.id);
    CHECK(ids == std::vector<std::uint64_t>({2, 3, 4}));
    ids.clear();
    for (auto const& zone : HazardZonesFor(zones, 1, false))
        ids.push_back(zone.id);
    CHECK(ids == std::vector<std::uint64_t>({1, 3, 4}));
    // The crater in the physical realm is no hazard to a twilight body standing on the same spot.
    CHECK(!HazardAt({0, 0, 0}, HazardZonesFor({physical}, 32, false), kReach).has_value());
    CHECK(HazardAt({0, 0, 0}, HazardZonesFor({physical}, 1, false), kReach).has_value());
}

// Review of #392: a follow goal memoised in one realm must not be reused in the other, nor across a tank reading.
LEEROY_TEST(Hazard_AnAnswerIsMemoisedPerPhaseAndTankReading)
{
    std::uint64_t const physical = HazardSelectionKey(7, 1, false);
    CHECK_EQ(physical, HazardSelectionKey(7, 1, false));
    CHECK(physical != HazardSelectionKey(7, 32, false)); // into the twilight realm
    CHECK(physical != HazardSelectionKey(7, 1, true));   // now read as a tank
    CHECK(physical != HazardSelectionKey(8, 1, false));  // the zones changed
    CHECK(HazardSelectionKey(1, 0xFFFFFFFFu, true) != HazardSelectionKey(2, 0xFFFFFFFFu, true));
}

// Npcs that go off when a player comes near (2026-11-21: bots walked into the Living Poison of Naxxramas's
// Construct Quarter hall). Measured from the core's scripts and Spell.dbc; see the README.
LEEROY_TEST(Hazard_OnlyTheMeasuredProximityNpcsAreAllowListed)
{
    for (std::uint32_t entry : {16027u, 34362u, 33292u, 39137u})
    {
        auto const rule = ProximityHazardFor(entry);
        CHECK(rule.has_value());
        CHECK_EQ(rule->entry, entry);
    }
    CHECK(!ProximityHazardFor(0).has_value());
    CHECK(!ProximityHazardFor(40681).has_value()); // the Living Inferno: an add a tank holds (HeldAuraHazard)
    CHECK(!ProximityHazardFor(16290).has_value()); // a Fallout Slime: an add the raid kills in its cloud
    CHECK(!ProximityHazardFor(34628).has_value()); // a Concentrated Darkness orb: the raid collects its colour
}

LEEROY_TEST(Hazard_AProximityNpcsZoneIsItsTriggerOrItsLargerBlast)
{
    auto const poison = *ProximityHazardFor(16027);
    auto const mine = *ProximityHazardFor(34362);
    auto const cloud = *ProximityHazardFor(33292);
    auto const trap = *ProximityHazardFor(39137);
    CHECK_EQ(poison.blast, 0u);
    CHECK_EQ(HazardProximityRadius(poison, 5.0f, 1.5f), 1.5f); // Explode's 5 yards would shut its lanes
    CHECK_EQ(mine.blast, 66351u);
    CHECK_EQ(HazardProximityRadius(mine, 3.0f, 1.0f), 3.0f); // the blast beats its 1.9-yard trigger
    CHECK_EQ(HazardProximityRadius(mine, 0.0f, 1.0f), 1.9f); // no spell to read: the trigger still holds
    CHECK_EQ(HazardProximityRadius(cloud, 0.0f, 1.0f), 7.0f); // 6 yards from its edge
    CHECK_EQ(HazardProximityRadius(cloud, 0.0f, -1.0f), 6.0f);
    CHECK_EQ(trap.blast, 73529u);
    CHECK_EQ(HazardProximityRadius(trap, 10.0f, 1.0f), 10.0f);
    CHECK_EQ(HazardProximityRadius(trap, 40.0f, 1.0f), 3.0f); // a blast past the large cap: its trigger alone
    CHECK_EQ(HazardProximityRadius(ProximityHazardRule{}, 0.0f, 1.0f), 0.0f);
}

LEEROY_TEST(Hazard_NobodyHoldsAProximityNpc)
{
    HazardZone const poison{0, 0, 0, 1.5f, 1};
    CHECK(HazardApplies(poison, 1, true));
    CHECK_EQ(HazardZonesFor({poison}, 1, true).size(), std::size_t(1));
    CHECK(HazardAt({2.5f, 0, 0}, HazardZonesFor({poison}, 1, true), 1.5f).has_value()); // a tank steps out too
}

LEEROY_TEST(Hazard_AMovingHazardLeadsAlongItsPath)
{
    HazardZone const poison{0, 0, 0, 1.5f, 7, 1};
    HazardZone const led = HazardLeadZone(poison, HazardPoint{0, -20, 0}, 2.5f);
    CHECK(std::fabs(led.x) < 0.001f);
    CHECK(std::fabs(led.y + 1.25f) < 0.001f);      // halfway to where it is in a second
    CHECK(std::fabs(led.radius - 2.75f) < 0.001f); // round both
    CHECK_EQ(led.id, poison.id);
    CHECK_EQ(led.phase, poison.phase);
    HazardZone const close = HazardLeadZone(poison, HazardPoint{0.5f, 0, 0}, 2.5f); // never past its path's point
    CHECK(std::fabs(close.x - 0.25f) < 0.001f);
    CHECK(std::fabs(close.radius - 1.75f) < 0.001f);
    CHECK(HazardLeadZone(poison, std::nullopt, 2.5f) == poison);
    CHECK(HazardLeadZone(poison, HazardPoint{0, -20, 0}, 0.0f) == poison);
    CHECK(HazardLeadZone(poison, HazardPoint{0, -20, 0}, std::nanf("")) == poison);
    CHECK(HazardLeadZone(poison, HazardPoint{0, 0, 0}, 2.5f) == poison); // at its point already
    CHECK(led.moving && close.moving && !poison.moving);
}

// A walk near a slime walking south at 2.5 yards a second, judged as both move (the dojo's after-run 4: a bot that
// crossed a lane a yard in front of one set it off, the led circle being clear at the start).
LEEROY_TEST(Hazard_AWalkIsJudgedAgainstWhereAMovingNpcGoes)
{
    HazardZone const slime = HazardLeadZone({0, 0, 0, 1.5f, 1}, HazardPoint{0, -40, 0}, 2.5f);
    float const reach = 1.5f;
    std::vector<HazardZone> const zones{slime};
    // Its led circle (2.75 yards round a point 1.25 ahead) leaves this line clear, but it comes down onto the bot.
    CHECK(Dist({0, -6, 0}, {slime.x, slime.y, 0}) > slime.radius + reach);
    CHECK(!HazardSegmentClear({-10, -6, 0}, {10, -6, 0}, zones, reach));
    CHECK(HazardSegmentClear({-10, 6, 0}, {10, 6, 0}, zones, reach)); // behind it: it has gone
    CHECK(!HazardSegmentClear({-10, -8, 0}, {0, -8, 0}, zones, reach)); // stopping in its way: it arrives
    CHECK(HazardSegmentClear({-10, -8, 0}, {-5, -8, 0}, zones, reach)); // stopping off its way
    HazardZone parked = slime;
    parked.until = 0.0f; // at its path's end: it stays
    CHECK(HazardSegmentClear({-10, -8, 0}, {0, -8, 0}, {parked}, reach));
}

// A bot waits for a gap only where no slime comes within 2 s; beside a lane with one coming it steps aside
// instead of leaving itself to the exit.
LEEROY_TEST(Hazard_ABotWaitsOnlyWhereNoSlimeComes)
{
    float const reach = 1.5f;
    // A lane running south along x = 0 and a slime 3 yards north of the bot's latitude, coming.
    HazardZone const slime = HazardLeadZone({0, 3, 0, 1.5f, 1}, HazardPoint{0, -40, 0}, 2.5f);
    std::vector<HazardZone> const zones{slime};
    HazardPoint const beside{4, 0, 0};
    CHECK(!HazardAt(beside, zones, reach).has_value()); // not in its zone yet
    CHECK(!HazardStandClear(beside, zones, reach));      // but in its way within 2 s
    HazardPoint const goal{-30, 0, 0};                   // across the lane: no walk gets ahead of it
    auto const leg = HazardMovingLeg(beside, goal, std::nullopt, zones, reach, 3.5f);
    CHECK(leg.has_value());
    CHECK(Dist(*leg, beside) > 1.0f);                    // a step, not a wait on the spot
    CHECK(leg->x >= 4.5f);                               // its radius, the reach and the margin from the lane
    CHECK(HazardStandClear(*leg, zones, reach));
    // Well clear of the lane it waits where it stands.
    HazardPoint const back{9, 0, 0};
    CHECK(HazardStandClear(back, zones, reach));
    auto const wait = HazardMovingLeg(back, goal, std::nullopt, zones, reach, 3.5f);
    CHECK(wait.has_value() && Dist(*wait, back) < 0.01f);
}

// A slime that walks onto a bot again and again is no mover walking the bot back in: never counted, never yielded.
LEEROY_TEST(Hazard_AMovingHazardNeverYieldsTheExit)
{
    HazardZone slime{0, 0, 0, 1.5f, 5};
    slime.moving = true;
    HazardZone const pool{0, 0, 0, 4, 6};
    HazardExitGuard guard;
    for (std::uint64_t now = 0; now < 10000; now += 1000)
    {
        guard.standing.clear(); // stood clear, then the slime walked onto it again
        CHECK(NoteHazardZoneEntry(guard, {slime}, now) == HazardEntry::Exit);
    }
    CHECK(!guard.zones.contains(5));
    // Walked back into the pool where the slime is, the pool's entries count as ever, but the bot still steps out.
    for (std::uint64_t now : {11000u, 12000u, 13000u})
    {
        guard.standing.clear();
        CHECK(NoteHazardZoneEntry(guard, {slime, pool}, now) == HazardEntry::Exit);
    }
    CHECK(HazardExitYielded(guard, {6}, 13000));
    CHECK(!HazardExitYielded(guard, {5, 6}, 13000));
    CHECK(NoteHazardZoneEntry(guard, {pool}, 14000) == HazardEntry::Yielded); // the pool alone is let through
}

LEEROY_TEST(Hazard_DistantMotionStillCrossesAndBoundaryTouchesStillBlock)
{
    HazardZone const fast = HazardLeadZone({0, 31, 0, 1.5f, 1}, HazardPoint{0, -30, 0}, 20.0f);
    // The current/led circle misses the entire segment; its later trajectory does not.
    CHECK(fast.y - fast.radius > 1.0f);
    CHECK(!HazardSegmentClear({-7, 0, 0}, {7, 0, 0}, {fast}, 1.0f));
    CHECK(!HazardSegmentClear({7, 0, 0}, {-7, 0, 0}, {fast}, 1.0f));
    CHECK(!HazardSegmentClear({-7, 0, 0}, {7, 0, 0}, {{0, 3, 0, 2, 2}}, 1.0f));
    CHECK(HazardSegmentClear({-7, 0, 0}, {7, 0, 0}, {{0, 3.01f, 0, 2, 2}}, 1.0f));
    CHECK(HazardSegmentClear({-7, 0, 0}, {7, 0, 0}, {{1000, 1000, 0, 2, 3}}, 1.0f));
}

LEEROY_TEST(Hazard_StepAsideTriesReachableSpotsAndNeverReturnsAnUncheckedGoal)
{
    HazardPoint const at{4, 0, 0}, goal{-30, 0, 0};
    HazardZone const slime = HazardLeadZone({0, 3, 0, 1.5f, 1}, HazardPoint{0, -40, 0}, 2.5f);
    auto const blocked = HazardMovingLeg(at, goal, std::nullopt, {slime}, 1.5f, 3.5f,
        [](HazardPoint const&) { return false; });
    CHECK(blocked.has_value());
    CHECK(Dist(*blocked, at) < 0.01f);
    // A wall closes the nearest candidate but leaves the north side reachable.
    auto const around = HazardMovingLeg(at, goal, std::nullopt, {slime}, 1.5f, 3.5f,
        [](HazardPoint const& point) { return point.y > 0.4f; });
    CHECK(around.has_value());
    CHECK(around->y > 0.4f);
    CHECK(HazardStandClear(*around, {slime}, 1.5f));
}

// An exit planned again and again inside the same zones is one entry; a bot another mover walks from one
// overlapping zone into the other and back, never out of both, enters each in turn (review of #424).
LEEROY_TEST(Hazard_OnlyAZoneNewlyEnteredCountsTowardTheYield)
{
    HazardZone const a{0, 0, 0, 4, 1};
    HazardZone const b{6, 0, 0, 4, 2};
    HazardExitGuard guard;
    std::uint64_t now = 1000;
    for (int replan = 0; replan < 10; ++replan)
        CHECK(NoteHazardZoneEntry(guard, {a}, now += 100) == HazardEntry::Exit);
    CHECK_EQ(guard.zones.at(1).exits, 1u);
    std::vector<std::vector<HazardZone>> const turns{{a, b}, {b}, {b, a}, {a}, {a, b}, {b}, {b, a}, {a}, {a, b}};
    std::vector<HazardEntry> seen;
    for (auto const& inside : turns)
        seen.push_back(NoteHazardZoneEntry(guard, inside, now += 1000));
    for (std::size_t i = 0; i < 6; ++i)
        CHECK(seen[i] == HazardEntry::Exit);
    CHECK(seen[6] == HazardEntry::Exit);         // a's third entry yields a, but b is not yielded
    CHECK(seen[7] == HazardEntry::Yielded);      // standing in a alone, a lets it through
    CHECK(seen[8] == HazardEntry::YieldStarts);  // b's third entry
    CHECK(HazardExitYielded(guard, {1, 2}, now));
    // Stood clear, and back in the same zone after the window: a fresh count.
    guard.standing.clear();
    CHECK(NoteHazardZoneEntry(guard, {a}, now + kHazardYieldMs + kHazardExitWindowMs + 1) == HazardEntry::Exit);
}

namespace
{
// instance_naxxramas.cpp LivingPoisonDataList: start, end and despawn; one slime per lane every 5 s, walking at
// 2.5 yards a second (speed_walk 1).
struct PoisonLane
{
    HazardPoint start, end;
    float despawn;
};
PoisonLane const kPoisonLanes[3]{
    {{3128.59f, -3118.81f, 293.346f}, {3130.322f, -3156.51f, 293.324f}, 15.2f},
    {{3154.25f, -3125.7f, 293.43f}, {3144.779f, -3158.416f, 293.324f}, 14.8f},
    {{3175.42f, -3134.86f, 293.34f}, {3158.778f, -3164.201f, 293.312f}, 14.8f},
};
constexpr float kPoisonSpeed = 2.5f;

struct Poison
{
    HazardPoint at;
    std::optional<HazardPoint> next;
    std::uint64_t id;
};

std::vector<Poison> PoisonsAt(float t)
{
    std::vector<Poison> out;
    for (int lane = 0; lane < 3; ++lane)
        for (int wave = 0; static_cast<float>(wave) * 5.0f <= t; ++wave)
        {
            PoisonLane const& l = kPoisonLanes[lane];
            float const age = t - static_cast<float>(wave) * 5.0f;
            if (age >= l.despawn)
                continue;
            float const length = Dist(l.start, l.end);
            float const walked = std::min(length, age * kPoisonSpeed);
            HazardPoint const at{l.start.x + (l.end.x - l.start.x) * walked / length,
                l.start.y + (l.end.y - l.start.y) * walked / length, l.start.z};
            out.push_back({at, walked < length ? std::optional<HazardPoint>(l.end) : std::nullopt,
                static_cast<std::uint64_t>(wave * 3 + lane + 1)});
        }
    return out;
}

// What the scan publishes for them: each slime's zone, led along its lane.
std::vector<HazardZone> PoisonZones(float t)
{
    float const radius = HazardProximityRadius(*ProximityHazardFor(16027), 5.0f, 1.5f);
    std::vector<HazardZone> zones;
    for (Poison const& p : PoisonsAt(t))
        zones.push_back(HazardLeadZone({p.at.x, p.at.y, p.at.z, radius, p.id}, p.next, kPoisonSpeed));
    return zones;
}

struct Crossing
{
    bool reached = false;
    float nearest = 1000.0f; // the closest any slime's centre came to the bot's
    int yields = 0;          // ticks a yield left the bot standing in a zone to follow
};

// A bot following him across the hall at 7 yards a second, 0.1 s a tick, as the filter drives it: the zones of
// the last 500 ms pass; standing in one, the exit (kept while clear, entries counted per slime); else follow,
// a detour waypoint kept until reached, blocked or the zones change. `aware` false walks straight, as stock.
Crossing CrossHall(HazardPoint bot, HazardPoint goal, float start, bool aware)
{
    float const reach = 1.5f;
    float const step = HazardDetourStep(1.5f, reach);
    Crossing out;
    std::vector<HazardZone> zones;
    std::optional<HazardPoint> kept, leave;
    HazardDetourTrack track;
    HazardExitGuard guard;
    for (int tick = 0; tick < 600; ++tick)
    {
        float const t = start + static_cast<float>(tick) * 0.1f;
        auto const now = static_cast<std::uint64_t>(t * 1000.0f);
        for (Poison const& p : PoisonsAt(t))
            out.nearest = std::min(out.nearest, Dist(bot, p.at));
        if (Dist(bot, goal) - reach <= 1.5f)
        {
            out.reached = true;
            return out;
        }
        if (aware && tick % 5 == 0)
        {
            zones = PoisonZones(t);
            kept.reset(); // a new version: the memoised waypoint is asked again
        }
        std::vector<HazardZone> inside;
        for (HazardZone const& zone : zones)
            if (InHazard(bot, zone, reach))
                inside.push_back(zone);
        std::optional<HazardPoint> target;
        if (inside.empty())
        {
            leave.reset();
            guard.standing.clear();
        }
        else if (NoteHazardZoneEntry(guard, inside, now) != HazardEntry::Exit)
            ++out.yields; // left to follow
        else if (leave && HazardClear(*leave, zones, reach))
            target = leave;
        else
        {
            // in a zone with no exit yet, or the one chosen was taken by a slime: planned again
            auto const spots = HazardExitSpots(bot, goal, goal, zones, reach, 1);
            leave = spots.empty() ? std::nullopt : std::optional<HazardPoint>(spots.front());
            target = leave;
        }
        if (!target)
        {
            if (kept && (Dist(bot, *kept) <= step || !HazardSegmentClear(bot, *kept, zones, reach)))
                kept.reset();
            if (!kept && inside.empty() && !HazardSegmentClear(bot, goal, zones, reach)
                && HazardDetourAllowed(track, bot, goal))
            {
                auto const waypoints = HazardDetour(bot, goal, zones, reach, step);
                if (!waypoints.empty())
                    kept = waypoints.front();
            }
            target = kept.value_or(goal);
        }
        float const left = Dist(bot, *target);
        if (left > 0.001f)
        {
            float const move = std::min(0.7f, left);
            bot.x += (target->x - bot.x) / left * move;
            bot.y += (target->y - bot.y) / left * move;
        }
    }
    return out;
}
}

// The owner's hallway: three lanes of slimes 12.5 yards apart, each set off at 1.5 yards. Walking straight sets
// them off; with the zones a follower crosses every lane between them, whenever it starts. The paths cross the
// middle of the lanes: a slime that appears on a bot standing at its pipe (a lane's start) is not foreseen.
LEEROY_TEST(Hazard_AFollowerCrossesTheLivingPoisonLanesWithoutSettingOneOff)
{
    int runs = 0, reached = 0, setOff = 0, straightSetOff = 0, yields = 0;
    float nearest = 1000.0f;
    for (float y : {-3140.0f, -3145.0f, -3150.0f})
        for (int offset = 0; offset < 40; ++offset)
        {
            float const start = 20.0f + static_cast<float>(offset) * 0.125f; // every phase of the 5 s waves
            HazardPoint const from{3105.0f, y, 293.3f};
            HazardPoint const goal{3192.0f, y - 3.0f, 293.3f};
            ++runs;
            Crossing const crossing = CrossHall(from, goal, start, true);
            reached += crossing.reached ? 1 : 0;
            setOff += crossing.nearest <= 1.5f ? 1 : 0;
            nearest = std::min(nearest, crossing.nearest);
            yields += crossing.yields;
            straightSetOff += CrossHall(from, goal, start, false).nearest <= 1.5f ? 1 : 0;
        }
    CHECK(straightSetOff > runs / 4); // the hall is a real hazard to a straight walk
    CHECK_EQ(setOff, 0);
    CHECK(nearest > 2.5f); // measured 3.04
    CHECK_EQ(yields, 0);   // a slime walking onto a bot never counts as the bot walking back in
    CHECK_EQ(reached, runs);
}

namespace
{
constexpr float kRunSpeed = 7.0f;

// A bot as playerbots moves it (MovementAction::MoveTo): each move sets out on one leg and commits to it for the
// leg's walk time less the react delay (0.1 s), at most MaxWaitForMove (5 s). Until then a move of the same or a
// lower priority waits (IsWaitingForLastMove), and a move to the same spot is a duplicate for 5 s.
struct Mover
{
    HazardPoint at;
    std::optional<HazardPoint> dest;
    float until = -1.0f;
    float issued = -10.0f;
    int priority = 0; // 1 follow, 2 avoid aoe

    bool Move(HazardPoint to, int moveAt, float now)
    {
        if (moveAt <= priority && now < until)
            return false;
        if (dest && Dist(*dest, to) < 0.01f && now < issued + 5.0f)
            return false;
        float const length = Dist(at, to);
        if (length < 0.01f)
            return false;
        dest = to;
        issued = now;
        priority = moveAt;
        until = now + std::clamp(length / kRunSpeed - 0.1f, 0.0f, 5.0f);
        return true;
    }

    void Step(float dt)
    {
        if (!dest)
            return;
        float const left = Dist(at, *dest);
        float const move = std::min(left, kRunSpeed * dt);
        if (left > 0.001f)
        {
            at.x += (dest->x - at.x) / left * move;
            at.y += (dest->y - at.y) / left * move;
        }
        if (left - move < 0.001f)
            dest.reset();
    }
};

struct HallRun
{
    int crossings = 0;
    int hits = 0;            // slimes the bot set off
    float nearest = 1000.0f; // the closest any slime's centre came to the bot's
};

// The dojo's Living Poison case (PR #452): the leader stands at one end of the hall, the bot follows him across,
// and once it is within 6 yards he is put at the other end; 70 s. A slime goes off (and is gone) when the bot's
// centre comes within 1.5 yards. Every 0.1 s the bot's AI first steps out of a zone it stands in (avoid aoe, at most
// once a second), else follows: the formation spot beside him moved off the zones, a detour waypoint kept until
// reached or the zones change (follow stops FollowDistance, 1.5, from the bot's edge; with `chaos`, the default formation,
// a spot up to 3.5 yards off and the stop that much wider, drawn afresh every 3 s), and, with `movingRules`, the moving-npc rules (HazardMovingLeg, and no pocket
// given up while a moving npc is the one in the way); without them, as #424 released it. `aware` false walks as stock.
HallRun DojoHall(float start, bool aware, bool movingRules, bool chaos = false)
{
    float const reach = 1.5f;
    float followStop = 1.5f; // Playerbots.FollowDistance
    float step = HazardDetourStep(followStop, reach);
    float dx = 0.0f, dy = 0.0f;
    std::mt19937 draws(static_cast<std::uint32_t>(start * 1000.0f));
    HazardPoint const ends[2]{{3108.0f, -3141.0f, 293.34f}, {3195.0f, -3148.0f, 293.34f}};
    Mover bot{ends[0]};
    int side = 1; // where he stands
    HallRun out;
    std::vector<HazardZone> zones;
    std::vector<std::uint64_t> gone;
    std::optional<HazardPoint> kept, leave;
    HazardDetourTrack track;
    HazardExitGuard guard;
    float lastAvoid = -10.0f;
    float const dt = 0.05f;
    for (int tick = 0; tick < 1400; ++tick)
    {
        float const t = start + static_cast<float>(tick) * dt;
        auto const now = static_cast<std::uint64_t>(t * 1000.0f);
        for (Poison const& p : PoisonsAt(t))
        {
            if (std::find(gone.begin(), gone.end(), p.id) != gone.end())
                continue;
            float const d = Dist(bot.at, p.at);
            out.nearest = std::min(out.nearest, d);
            if (d <= 1.5f)
            {
                ++out.hits;
                gone.push_back(p.id);
            }
        }
        HazardPoint const he = ends[side];
        if (Dist(bot.at, he) < 6.0f)
        {
            ++out.crossings;
            side = 1 - side;
            kept.reset();
        }
        if (aware && tick % 10 == 0)
        {
            zones.clear();
            for (HazardZone const& zone : PoisonZones(t))
                if (std::find(gone.begin(), gone.end(), zone.id) == gone.end())
                    zones.push_back(zone);
            kept.reset(); // a new version: the memoised waypoint is asked again
        }
        if (chaos && tick % 60 == 0) // ChaosFormation: a fresh offset every 3 s, half TooCloseDistance (5) each way
        {
            // mt19937's output is the same everywhere (a distribution's is not): urand(0, 10) by modulo
            dx = (static_cast<float>(draws() % 11) / 10.0f - 0.5f) * 5.0f;
            dy = (static_cast<float>(draws() % 11) / 10.0f - 0.5f) * 5.0f;
            followStop = 1.5f + std::hypot(dx, dy);
            step = HazardDetourStep(followStop, reach);
        }
        if (tick % 2 == 0)
        {
            float const toBot = Dist(he, bot.at);
            HazardPoint goal = toBot > 0.01f ? HazardPoint{he.x + (bot.at.x - he.x) / toBot * 1.5f + dx,
                he.y + (bot.at.y - he.y) / toBot * 1.5f + dy, he.z} : he;
            if (auto const spots = HazardSafeSpots(goal, bot.at, zones, reach, 1); !spots.empty())
                goal = spots.front();
            std::vector<HazardZone> inside;
            for (HazardZone const& zone : zones)
                if (InHazard(bot.at, zone, reach))
                    inside.push_back(zone);
            bool avoided = false;
            if (inside.empty())
            {
                leave.reset();
                guard.standing.clear();
            }
            else if (t - lastAvoid >= 1.0f && NoteHazardZoneEntry(guard, inside, now) == HazardEntry::Exit)
            {
                if (!leave || !HazardClear(*leave, zones, reach))
                {
                    auto const exits = HazardExitSpots(bot.at, goal, goal, zones, reach, 1);
                    leave = exits.empty() ? std::nullopt : std::optional<HazardPoint>(exits.front());
                }
                if (leave && bot.Move(*leave, 2, t))
                {
                    lastAvoid = t;
                    avoided = true;
                }
            }
            if (!avoided)
            {
                if (kept && (Dist(bot.at, *kept) <= step || !HazardSegmentClear(bot.at, *kept, zones, reach)))
                    kept.reset();
                if (!kept && inside.empty() && !HazardSegmentClear(bot.at, goal, zones, reach)
                    && ((movingRules && HazardFirstCrossingMoves(bot.at, goal, zones, reach)) || HazardDetourAllowed(track, bot.at, goal)))
                {
                    auto const waypoints = HazardDetour(bot.at, goal, zones, reach, step);
                    if (!waypoints.empty())
                        kept = waypoints.front();
                }
                std::optional<HazardPoint> const leg =
                    movingRules ? HazardMovingLeg(bot.at, goal, kept, zones, reach, step) : std::nullopt;
                HazardPoint const target = leg.value_or(kept.value_or(goal));
                if (Dist(bot.at, target) - reach > followStop) // FollowAction::isUseful, from the bot's edge
                    bot.Move(target, 1, t);
            }
        }
        bot.Step(dt);
    }
    return out;
}
}

// The dojo's hall crossing as playerbots drives a bot, legs committed as it commits them, over 200 phases of the
// waves. As #424 released it the zones still set slimes off: slimes stream through the lanes, so the detour soon
// gave a crossing up as a pocket and walked straight. With the moving-npc rules none goes off and no crossing stalls.
LEEROY_TEST(Hazard_TheDojoHallCrossingSetsNoSlimeOff)
{
    int runs = 0, hits = 0, withoutHits = 0, straightHits = 0, fewCrossings = 0;
    float nearest = 1000.0f;
    for (int offset = 0; offset < 200; ++offset)
    {
        float const start = 20.0f + static_cast<float>(offset) * 0.025f; // every phase of the 5 s waves
        ++runs;
        for (bool chaos : {false, true}) // near or arrow; chaos, playerbots' default
        {
            HallRun const run = DojoHall(start, true, true, chaos);
            hits += run.hits;
            nearest = std::min(nearest, run.nearest);
            fewCrossings += run.crossings < 2 ? 1 : 0;
        }
        withoutHits += DojoHall(start, true, false).hits;
        straightHits += DojoHall(start, false, false).hits;
    }
    CHECK(straightHits > runs);  // stock walks into them
    CHECK(withoutHits > 0);      // #424's rules alone (5)
    CHECK_EQ(hits, 0);
    CHECK(nearest > 2.0f);       // measured 2.37
    CHECK_EQ(fewCrossings, 0);
}
