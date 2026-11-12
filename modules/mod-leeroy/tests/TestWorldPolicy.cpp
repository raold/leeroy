// This file is part of leeroy, released under the GNU GPL v2.
#include "LeeroyWorldPolicy.h"
#include "TestHarness.h"
#include "LeeroyWorldProtocol.h"
#include "LeeroyWorldGuilds.h"
#include <map>
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

LEEROY_TEST(WorldCommandsRequireBothReloadableSwitches)
{
    CHECK(!Enabled(false,false)); CHECK(!Enabled(false,true));
    CHECK(!Enabled(true,false)); CHECK(Enabled(true,true));
}

LEEROY_TEST(WorldAuctionAccountsResolveWholeAccountsAndFailClosed)
{
    std::set<std::uint32_t> out;
    auto resolve = [](std::uint32_t guid) { return guid == 101 || guid == 102 ? 11u : 0u; };
    CHECK(AuctionAccounts("0, 101,102",resolve,out)); CHECK(out == std::set<std::uint32_t>{11});
    for (auto bad : {"", "101,", "101,,102", "101,999", "-1", "4294967296", "101x", " "})
    { out = {99}; CHECK(!AuctionAccounts(bad,resolve,out)); CHECK(out == std::set<std::uint32_t>{99}); }
    CHECK(AuctionAccounts("0",resolve,out)); CHECK(out.empty());
}

LEEROY_TEST(WorldRosterAndSpeakerShareMembershipEvidence)
{
    std::vector<MemberEvidence> rows{{103,13,true,true,true,false},{1,10,true,true,true,false},
        {102,12,true,true,true,true},{101,11,true,true,true,false}};
    auto picked = Roster(50,21,1,rows);
    CHECK_EQ(picked.size(),2u); CHECK_EQ(picked[0].guid,101u); CHECK_EQ(picked[1].guid,103u);
    CHECK(Roster(21,21,1,rows).empty()); CHECK(Roster(50,21,0,rows).empty());
    CHECK(WorldSpeaker(true,true,false,101,50,21,1,rows));
    CHECK(!WorldSpeaker(false,true,false,101,50,21,1,rows));
    CHECK(!WorldSpeaker(true,false,false,101,50,21,1,rows));
    CHECK(!WorldSpeaker(true,true,true,101,50,21,1,rows));
    for (auto guid : {0u,1u,102u,104u}) CHECK(!WorldSpeaker(true,true,false,guid,50,21,1,rows));
    rows[0].randomAccount=false;
    CHECK(Roster(50,21,1,rows).empty()); CHECK(!WorldSpeaker(true,true,false,101,50,21,1,rows));
}

LEEROY_TEST(WorldPagesAreBoundedCompleteAndUseRequestTokens)
{
    using nlohmann::json;
    std::string error;
    CHECK(RosterRequest(json{{"id","wire"},{"request","token-1"}},error) == "token-1");
    for (auto bad : {json{},json{{"request","bad token"}},json{{"request",42}}})
    { CHECK(!RosterRequest(bad,error)); CHECK_EQ(error,"bad field: request"); }
    CHECK_EQ(PageCount(0),1u); CHECK_EQ(PageCount(100),1u); CHECK_EQ(PageCount(101),2u); CHECK_EQ(PageCount(8192),82u);
    json member{{"guid",101},{"account",11},{"guild_id",50},{"guild_name","Test rivals"},
        {"name","Fixture"},{"race",2},{"class",1},{"gender",0},{"level",24},{"rank",3},
        {"online",true},{"map",1},{"zone",1637}};
    auto page=RosterPage("token",21,1,1,101,json::array({member}));
    CHECK(page); CHECK_EQ((*page)["pages"],2); CHECK_EQ((*page)["request"],"token");
    CHECK(!RosterPage("token",21,1,0,101,json::array({member})));
    CHECK(!RosterPage("token",21,1,2,101,json::array({member})));
    CHECK(!RosterPage("token",21,1,0,2,json::array({member,member})));
    CHECK(RosterPage("empty",21,1,0,0,json::array()));
    member["guild_id"]=21; CHECK(!RosterPage("token",21,1,0,1,json::array({member})));
}

namespace
{
LEEROY_TEST(WorldSnapshotValidatesTheLastPageBeforeReturningAnyPages)
{
    using nlohmann::json;
    json member{{"guid",101},{"account",11},{"guild_id",50},{"guild_name","Test rivals"},
        {"name","Fixture"},{"race",2},{"class",1},{"gender",0},{"level",24},{"rank",3},
        {"online",true},{"map",1},{"zone",1637}};
    auto rows=json::array();
    for (unsigned i=0; i<=kPageSize; ++i) { member["guid"]=101+i; rows.push_back(member); }
    auto pages=RosterPages("token",21,1,rows);
    CHECK(pages); CHECK_EQ(pages->size(),2u);
    CHECK_EQ((*pages)[0]["members"].size(),kPageSize);
    CHECK_EQ((*pages)[1]["members"].size(),1u);
    rows.back()["guild_id"]=21;
    CHECK(!RosterPages("token",21,1,rows));
    rows.back()["guild_id"]=50; rows.back()["guid"]=101; // duplicate across page boundaries
    CHECK(!RosterPages("token",21,1,rows));
    auto empty=RosterPages("empty",21,1,json::array());
    CHECK(empty); CHECK_EQ(empty->size(),1u);
    CHECK(!RosterPages("bad token",21,1,json::array()));
    CHECK(!RosterPages("token",21,1,json::object()));
}

struct FixtureGuild { unsigned id; unsigned GetId() const { return id; } };
class FixtureManager
{
protected:
    std::map<unsigned,FixtureGuild*> GuildStore;
public:
    void Add(FixtureGuild* g) { GuildStore[g ? g->id : 0] = g; }
};
}
LEEROY_TEST(WorldGuildAdapterReadsRealManagerWithoutDowncast)
{
    FixtureGuild a{50}, b{21}; FixtureManager manager;
    manager.Add(&a); manager.Add(nullptr); manager.Add(&b);
    auto rows=GuildSnapshot<FixtureManager>::All(manager);
    CHECK_EQ(rows.size(),2u); CHECK(rows[0] == &b); CHECK(rows[1] == &a);
}

namespace
{
nlohmann::json RosterMember(std::uint32_t guid)
{
    return nlohmann::json{{"guid",guid},{"account",guid+1000},{"guild_id",50},{"guild_name","Test rivals"},
        {"name","Fixture"},{"race",2},{"class",1},{"gender",0},{"level",80},{"rank",3},
        {"online",false},{"map",0},{"zone",0}};
}
}

LEEROY_TEST(WorldMemberProblemNamesTheRule)
{
    auto const good = RosterMember(101);
    CHECK(MemberProblem(good,21,1).empty()); CHECK(ValidMember(good,21,1));
    std::vector<std::pair<std::string,nlohmann::json>> breaks{
        {"guid",0},{"account",0},{"guild_id",0},{"online",1},{"race",12},{"class",0},{"gender",2},
        {"level",0},{"rank",10},{"name",""},{"guild_name",std::string(49,'a')},{"map",-1}};
    for (auto const& [key,value] : breaks)
    {
        auto m = good; m[key] = value;
        CHECK_EQ(std::string(MemberProblem(m,21,1)),key); CHECK(!ValidMember(m,21,1));
    }
    auto human = good; human["guid"] = 1; CHECK_EQ(std::string(MemberProblem(human,21,1)),"human");
    auto own = good; own["guild_id"] = 21; CHECK_EQ(std::string(MemberProblem(own,21,1)),"own_guild");
    auto shape = good; shape.erase("zone"); CHECK_EQ(std::string(MemberProblem(shape,21,1)),"shape");
    auto control = good; control["name"] = std::string("Fix\x01ture"); CHECK_EQ(std::string(MemberProblem(control,21,1)),"name");
}

LEEROY_TEST(WorldValidRowsSkipsOddMembersAndDuplicates)
{
    auto odd = RosterMember(102); odd["level"] = 0;
    auto rows = nlohmann::json::array({RosterMember(101),odd,RosterMember(101),RosterMember(103)});
    auto valid = ValidRows(rows,21,1);
    CHECK_EQ(valid.rows.size(),2u);
    CHECK_EQ(valid.rows[0]["guid"],101); CHECK_EQ(valid.rows[1]["guid"],103);
    CHECK_EQ(valid.skipped.size(),2u);
    CHECK(valid.skipped[0] == std::make_pair(std::uint32_t{102},std::string("level")));
    CHECK(valid.skipped[1] == std::make_pair(std::uint32_t{101},std::string("duplicate")));
    auto unreadable = RosterMember(104); unreadable["guid"] = "x";
    CHECK(ValidRows(nlohmann::json::array({unreadable}),21,1).skipped[0] == std::make_pair(std::uint32_t{0},std::string("guid")));
}

LEEROY_TEST(WorldRosterPagesSpanEveryPage)
{
    auto rows = nlohmann::json::array();
    for (std::uint32_t guid = 101; guid < 351; ++guid) rows.push_back(RosterMember(guid));
    auto pages = RosterPages("token",21,1,rows);
    CHECK(pages); CHECK_EQ(pages->size(),3u);
    CHECK_EQ((*pages)[2]["members"].size(),50u); CHECK_EQ((*pages)[0]["total"],250);
    rows[120]["rank"] = 10;
    CHECK(!RosterPages("token",21,1,rows));               // one odd row refuses the raw snapshot
    auto valid = ValidRows(rows,21,1);
    auto kept = RosterPages("token",21,1,valid.rows);     // what native now publishes instead
    CHECK(kept); CHECK_EQ((*kept)[0]["total"],249); CHECK_EQ(valid.skipped.size(),1u);
}
