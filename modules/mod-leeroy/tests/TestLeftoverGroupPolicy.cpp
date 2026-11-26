// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyLeftoverGroupPolicy.h"
#include "TestHarness.h"
#include <vector>
using namespace Leeroy::Guild;
namespace
{
LeftoverMember Cast() { LeftoverMember m; m.cast = true; return m; }
std::vector<LeftoverMember> Raid() { return std::vector<LeftoverMember>(10, Cast()); }
}

LEEROY_TEST(LeftoverGroup_IdleCastRaidIsDisbanded)
{
    CHECK(DecideLeftoverGroup(Raid()) == LeftoverVerdict::Disband);
}

LEEROY_TEST(LeftoverGroup_OwnerOrNonCastOrReservedOrDrivenKeepsIt)
{
    auto raid = Raid(); raid[3].human = true;
    CHECK(DecideLeftoverGroup(raid) == LeftoverVerdict::KeepNotOwned);
    raid = Raid(); raid[0].cast = false;
    CHECK(DecideLeftoverGroup(raid) == LeftoverVerdict::KeepNotOwned);
    raid = Raid(); raid[1].reserved = true;
    CHECK(DecideLeftoverGroup(raid) == LeftoverVerdict::KeepNotOwned);
    raid = Raid(); raid[2].driven = true;
    CHECK(DecideLeftoverGroup(raid) == LeftoverVerdict::KeepNotOwned);
}

LEEROY_TEST(LeftoverGroup_AnyMemberInCombatKeepsIt)
{
    auto raid = Raid(); raid[7].inCombat = true;
    CHECK(DecideLeftoverGroup(raid) == LeftoverVerdict::KeepCombat);
    CHECK_EQ(std::string(LeftoverVerdictCode(LeftoverVerdict::KeepCombat)), std::string("combat"));
}

LEEROY_TEST(LeftoverGroup_AnyMemberOnInstanceMapKeepsIt)
{
    auto raid = Raid(); raid[9].inInstance = true;
    CHECK(DecideLeftoverGroup(raid) == LeftoverVerdict::KeepInstance);
    raid[0].inCombat = true; // combat is reported first when both hold
    CHECK(DecideLeftoverGroup(raid) == LeftoverVerdict::KeepCombat);
    CHECK_EQ(std::string(LeftoverVerdictCode(LeftoverVerdict::KeepInstance)), std::string("instance"));
}
