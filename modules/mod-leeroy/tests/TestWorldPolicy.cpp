// This file is part of leeroy, released under the GNU GPL v2.
#include "LeeroyWorldPolicy.h"
#include "TestHarness.h"
using namespace Leeroy::World;

LEEROY_TEST(WorldGuildRequiresEveryMemberOnRandomBotAccounts)
{
    std::vector<MemberEvidence> members{{101,11,true,true,true,false},{102,12,true,true,true,false}};
    CHECK(EligibleGuild(3,21,members));
    members[1].randomAccount=false;
    CHECK(!EligibleGuild(3,21,members));
    members[1].randomAccount=true; members[1].sameGuild=false;
    CHECK(!EligibleGuild(3,21,members));
    members[1].sameGuild=true; members[1].exists=false;
    CHECK(!EligibleGuild(3,21,members));
}

LEEROY_TEST(WorldGuildExcludesOwnUnknownAndEmptyGuilds)
{
    std::vector<MemberEvidence> members{{101,11,true,true,true,false}};
    CHECK(!EligibleGuild(21,21,members));
    CHECK(!EligibleGuild(3,0,members));
    CHECK(!EligibleGuild(0,21,members));
    CHECK(!EligibleGuild(3,21,{}));
}

LEEROY_TEST(WorldMembersExcludeHumanAndEntireAuctionAccountWithoutLevelGate)
{
    MemberEvidence member{101,11,true,true,true,false};
    CHECK(EligibleMember(member,1));
    CHECK(!EligibleMember(member,101));
    CHECK(!EligibleMember(member,0));
    member.auctionAccount=true;
    CHECK(!EligibleMember(member,1));
    member.guid=102; // another character on that account is excluded too
    CHECK(!EligibleMember(member,1));
}
