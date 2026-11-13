// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyGroupRezPolicy.h"
#include "TestHarness.h"
using namespace Leeroy::Guild;
namespace
{
constexpr std::uint64_t kRelease = 60000;
GroupRezBody Bot() { GroupRezBody b; b.bot = true; return b; }
GroupRezCandidate Rezzer(std::uint32_t guid, float distance)
{
    GroupRezCandidate c;
    c.guid = guid; c.distance = distance; c.castRange = 30.0f; c.los = true;
    return c;
}
}

LEEROY_TEST(GroupRez_RanksAreHighestFirstAndOnlyForRezClasses)
{
    CHECK_EQ(RezRanks(2)->front(), 48950u);
    CHECK_EQ(RezRanks(2)->back(), 7328u);
    CHECK_EQ(RezRanks(5)->front(), 48171u);
    CHECK_EQ(RezRanks(7)->front(), 49277u);
    CHECK(RezRanks(11) == nullptr); // druid: no out-of-combat rez in 3.3.5
    CHECK(RezRanks(1) == nullptr);
}

LEEROY_TEST(GroupRez_CastsFromTheNearestRezzerInRangeEvenWhenMounted)
{
    GroupRezMemory m;
    auto mounted = Rezzer(7, 12.0f);
    mounted.mounted = true;
    auto d = DecideGroupRez(Bot(), false, {Rezzer(9, 25.0f), mounted}, m, 0, kRelease);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 7u);
}

LEEROY_TEST(GroupRez_DismountsAMountedRezzerOutOfRangeAndLeavesAWalkerToPlayerbots)
{
    GroupRezMemory m;
    auto mounted = Rezzer(7, 45.0f);
    mounted.mounted = true;
    auto d = DecideGroupRez(Bot(), false, {mounted}, m, 0, kRelease);
    CHECK(d.step == GroupRezStep::Dismount);
    CHECK_EQ(d.caster, 7u);
    GroupRezMemory walker;
    CHECK(DecideGroupRez(Bot(), false, {Rezzer(8, 45.0f)}, walker, 0, kRelease).step == GroupRezStep::None);
}

LEEROY_TEST(GroupRez_NeverUsesAnAirborneMountedRezzerOrOneOutOfSightOrSearch)
{
    GroupRezMemory m;
    auto airborne = Rezzer(7, 5.0f);
    airborne.mounted = true; airborne.grounded = false;
    auto blind = Rezzer(8, 5.0f);
    blind.los = false;
    auto far = Rezzer(9, 58.0f);
    CHECK(DecideGroupRez(Bot(), false, {airborne, blind, far}, m, 0, kRelease).step == GroupRezStep::None);
    auto d = DecideGroupRez(Bot(), false, {airborne, Rezzer(10, 20.0f)}, m, 1, kRelease);
    CHECK(d.step == GroupRezStep::Cast);
    CHECK_EQ(d.caster, 10u);
}

LEEROY_TEST(GroupRez_RetriesACastOnlyAfterTheGap)
{
    GroupRezMemory m;
    CHECK(DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f)}, m, 1000, kRelease).step == GroupRezStep::Cast);
    CHECK(DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f)}, m, 1000 + kGroupRezRetryMs - 1, kRelease).step
        == GroupRezStep::None);
    CHECK(DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f)}, m, 1000 + kGroupRezRetryMs, kRelease).step
        == GroupRezStep::Cast);
}

LEEROY_TEST(GroupRez_CombatWaitsAndRestartsTheReleaseClock)
{
    GroupRezMemory m;
    CHECK(DecideGroupRez(Bot(), false, {}, m, 0, kRelease).step == GroupRezStep::None);
    CHECK(DecideGroupRez(Bot(), true, {Rezzer(7, 5.0f)}, m, 50000, kRelease).step == GroupRezStep::None);
    CHECK(!m.quietSince.has_value());
    CHECK(DecideGroupRez(Bot(), false, {}, m, 60000, kRelease).step == GroupRezStep::None);
    CHECK(DecideGroupRez(Bot(), false, {}, m, 60000 + kRelease - 1, kRelease).step == GroupRezStep::None);
    CHECK(DecideGroupRez(Bot(), false, {}, m, 60000 + kRelease, kRelease).step == GroupRezStep::Release);
}

LEEROY_TEST(GroupRez_ReleasesABotAfterAQuietMinuteEvenWithAFailingRezzer)
{
    GroupRezMemory m;
    CHECK(DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f)}, m, 0, kRelease).step == GroupRezStep::Cast);
    auto d = DecideGroupRez(Bot(), false, {Rezzer(7, 5.0f)}, m, kRelease, kRelease);
    CHECK(d.step == GroupRezStep::Release);
    CHECK_EQ(d.caster, 0u);
}

LEEROY_TEST(GroupRez_ARezUnderWayOrAwaitingAnAnswerHoldsTheReleaseWithoutRestartingIt)
{
    GroupRezMemory m;
    auto body = Bot();
    CHECK(DecideGroupRez(body, false, {}, m, 0, kRelease).step == GroupRezStep::None);
    body.rezzing = true;
    CHECK(DecideGroupRez(body, false, {Rezzer(7, 5.0f)}, m, kRelease + 5000, kRelease).step == GroupRezStep::None);
    body.rezzing = false; body.requested = true;
    CHECK(DecideGroupRez(body, false, {Rezzer(7, 5.0f)}, m, kRelease + 9000, kRelease).step == GroupRezStep::None);
    body.requested = false;
    CHECK(DecideGroupRez(body, false, {}, m, kRelease + 10000, kRelease).step == GroupRezStep::Release);
}

LEEROY_TEST(GroupRez_NeverReleasesTheOwnerOrInsideAnInstanceOrWhenReleaseIsOff)
{
    GroupRezBody owner;
    GroupRezMemory a;
    DecideGroupRez(owner, false, {}, a, 0, kRelease);
    CHECK(DecideGroupRez(owner, false, {}, a, 10 * kRelease, kRelease).step == GroupRezStep::None);
    auto d = DecideGroupRez(owner, false, {Rezzer(7, 5.0f)}, a, 10 * kRelease + 1, kRelease);
    CHECK(d.step == GroupRezStep::Cast); // he still gets a rez offer

    auto raid = Bot();
    raid.instance = true;
    GroupRezMemory b;
    DecideGroupRez(raid, false, {}, b, 0, kRelease);
    CHECK(DecideGroupRez(raid, false, {}, b, 10 * kRelease, kRelease).step == GroupRezStep::None);

    GroupRezMemory c;
    DecideGroupRez(Bot(), false, {}, c, 0, 0);
    CHECK(DecideGroupRez(Bot(), false, {}, c, 10 * kRelease, 0).step == GroupRezStep::None);
}

LEEROY_TEST(GroupRez_SelfResWaitsForTheBotButStillReleasesWhenItDoesNotUseIt)
{
    GroupRezMemory m;
    auto body = Bot();
    body.selfRes = true;
    CHECK(DecideGroupRez(body, false, {Rezzer(7, 5.0f)}, m, 0, kRelease).step == GroupRezStep::None);
    CHECK(DecideGroupRez(body, false, {Rezzer(7, 5.0f)}, m, kRelease, kRelease).step == GroupRezStep::Release);
}

LEEROY_TEST(GroupRez_AFencedBodyIsLeftAlone)
{
    GroupRezMemory m;
    auto body = Bot();
    body.fenced = true;
    CHECK(DecideGroupRez(body, false, {Rezzer(7, 5.0f)}, m, 0, kRelease).step == GroupRezStep::None);
    CHECK(DecideGroupRez(body, false, {Rezzer(7, 5.0f)}, m, 10 * kRelease, kRelease).step == GroupRezStep::None);
    CHECK(!m.quietSince.has_value());
}

LEEROY_TEST(GroupRez_TiesGoToTheLowestGuid)
{
    GroupRezMemory m;
    auto d = DecideGroupRez(Bot(), false, {Rezzer(9, 10.0f), Rezzer(4, 10.0f)}, m, 0, kRelease);
    CHECK_EQ(d.caster, 4u);
}
