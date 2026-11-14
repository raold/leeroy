// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyHazardPolicy.h"
#include "TestHarness.h"

#include <cmath>

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
    CHECK(!NoteHazardExit(guard, 7, 1000));
    CHECK(!HazardExitYielded(guard, 7, 1000));
    CHECK(!NoteHazardExit(guard, 7, 3000));
    CHECK(NoteHazardExit(guard, 7, 5000)); // a mover keeps walking it back in: let it through
    CHECK(HazardExitYielded(guard, 7, 5000));
    CHECK(HazardExitYielded(guard, 7, 5000 + kHazardYieldMs - 1));
    CHECK(!HazardExitYielded(guard, 8, 6000)); // another hazard is still stepped out of
    CHECK(!HazardExitYielded(guard, 7, 5000 + kHazardYieldMs));
    CHECK(!NoteHazardExit(guard, 7, 5000 + kHazardYieldMs + 1)); // a fresh window afterwards
}

LEEROY_TEST(Hazard_SlowOrDifferentEntriesNeverYield)
{
    HazardExitGuard guard;
    CHECK(!NoteHazardExit(guard, 7, 0));
    CHECK(!NoteHazardExit(guard, 7, kHazardExitWindowMs - 1));
    CHECK(!NoteHazardExit(guard, 7, 3 * kHazardExitWindowMs)); // the window ran out: counting again
    CHECK(!NoteHazardExit(guard, 8, 3 * kHazardExitWindowMs + 1));
    CHECK(!NoteHazardExit(guard, 7, 3 * kHazardExitWindowMs + 2));
    CHECK(!NoteHazardExit(guard, 8, 3 * kHazardExitWindowMs + 3));
    CHECK(!HazardExitYielded(guard, 7, 3 * kHazardExitWindowMs + 4));
}
