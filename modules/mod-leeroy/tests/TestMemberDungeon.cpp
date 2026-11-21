// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMemberDungeon.h"
#include "TestHarness.h"

using namespace Leeroy::Guild;

LEEROY_TEST(MemberDungeonChecksActualRolesAndEveryParticipant)
{
    using enum GuildAddRole;
    std::vector<DungeonCandidate> members{{101,Tank,{}}, {102,Healer,{}}, {103,Damage,{}},
                                           {104,Damage,{}}, {105,Damage,{}}};
    CHECK(MemberDungeonAdmission(602, 101, members, false, false).empty());
    CHECK_EQ(MemberDungeonAdmission(603, 101, members, false, false), "unsupported_dungeon");
    CHECK_EQ(MemberDungeonAdmission(602, 101, members, true, false), "human_online");
    CHECK_EQ(MemberDungeonAdmission(602, 101, members, false, true), "job_busy");
    CHECK_EQ(MemberDungeonAdmission(602, 102, members, false, false), "wrong_composition");
    members.back().refusal = "busy:raid_owner";
    CHECK_EQ(MemberDungeonAdmission(602, 101, members, false, false), "busy:raid_owner");
    members.back().refusal.clear();
    members.back().role = Tank;
    CHECK_EQ(MemberDungeonAdmission(602, 101, members, false, false), "wrong_composition");
    members.pop_back();
    CHECK_EQ(MemberDungeonAdmission(602, 101, members, false, false), "wrong_composition");
}

LEEROY_TEST(MemberDungeonReceiptRequiresTheExactDurableRosterAndTypedState)
{
    nlohmann::json accepted = {{"version",1},{"map",602},{"difficulty",1},{"stage","accepted"},{"slot",true},
        {"settled",false},{"guild",21},{"human",1},{"leader",101},{"roster",{101,102,103,104,105}},
        {"bodies",nlohmann::json::array()},{"terminal",nullptr}};
    CHECK(ValidDungeonReceipt(accepted));
    CHECK(DungeonBeforeMutation(accepted));
    auto observing = accepted;
    observing["stage"] = "grouping"; observing["effect"] = "observe";
    CHECK(DungeonBeforeMutation(observing));
    observing["effect"] = "create_group";
    CHECK(!DungeonBeforeMutation(observing));
    observing["effect"] = "observe"; observing["group"] = 7;
    CHECK(!DungeonBeforeMutation(observing));
    auto invalid = accepted;
    invalid["roster"] = {101,101,103,104,105};
    CHECK(!ValidDungeonReceipt(invalid));
    invalid = accepted;
    invalid["leader"] = 102;
    CHECK(!ValidDungeonReceipt(invalid));
    invalid = accepted;
    invalid["bodies"] = {{"guid",101}};
    CHECK(!ValidDungeonReceipt(invalid));
    accepted["settled"] = "true";
    CHECK(!ValidDungeonReceipt(accepted));
}

LEEROY_TEST(MemberDungeonOnlyValidatedOverlappingOwnerRequestPreempts)
{
    std::vector<std::uint32_t> participants{101,102,103,104,105};
    CHECK(!MemberDungeonPreempts(false, {101}, participants));
    CHECK(!MemberDungeonPreempts(true, {}, participants));
    CHECK(!MemberDungeonPreempts(true, {106}, participants));
    CHECK(MemberDungeonPreempts(true, {107,101}, participants));
}
