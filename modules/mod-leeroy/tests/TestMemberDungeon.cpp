// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMemberDungeon.h"
#include "LeeroyTrainingRuns.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyStrings.h"
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
        {"bodies",nlohmann::json::array()},{"terminal",nullptr},{"group",0},{"instance",0},{"issued_ms",1}};
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
    for (auto key : {"group", "instance", "recovery_ms", "terminal_ms", "failed_ms", "actor", "issued_ms",
                     "driver", "kills", "wipes", "recovery_authorized", "effect"})
    {
        invalid = accepted;
        invalid[key] = nlohmann::json::array();
        CHECK(!ValidDungeonReceipt(invalid));
    }
    invalid = accepted;
    invalid["group"] = "reused";
    CHECK(DungeonKnownRoster(invalid)==DungeonKnownRoster(accepted));
    CHECK(!ValidDungeonReceipt(invalid));
    CHECK(ValidDungeonReceipt(accepted)); // An adjacent valid row remains independently recoverable.
    accepted["settled"] = "true";
    CHECK(!ValidDungeonReceipt(accepted));
}

LEEROY_TEST(MemberDungeonOwnerAdmissionExcludesOnlyItsRegisteredRun)
{
    using namespace Leeroy::Training;
    TrainingRuns registry;
    std::vector<std::uint32_t> roster{101,102,103,104,105};
    RunScope scope{602,47,21,1,30};
    auto token = registry.Register(101,scope,*RunManifest::Capture(101,roster));
    auto owns = [&](auto guid,auto owner,auto handle) {
        return registry.OwnsBesides(guid,owner,handle,[](auto const& job,auto g) { return job.manifest.Owns(g); });
    };
    CHECK(token && registry.Owns(104));
    GuildAddBody add;
    add.online=add.cast=add.grouped=add.elsewhere=true;
    add.fence="busy:training_owner";
    auto taken=add;
    ProjectDungeonAddBody(taken,!owns(104,101,token));
    CHECK(GuildAddSkipReason(taken).empty());
    std::string error;
    auto join=ParseStringDo(nlohmann::json::parse(R"({"guid":104,"kind":"join_group","target_guid":1,"emote":""})"),error);
    auto summon=ParseSummon(nlohmann::json::parse(R"({"map_id":631,"guids":[105,104],"where":"entrance","summon_human":false,"summoner_guid":105})"),error);
    CHECK(join && summon);
    if (!join || !summon) return;
    CHECK(MemberDungeonPreempts(!owns(join->guid,101,token),{join->guid},roster));
    CHECK(MemberDungeonPreempts(!owns(104,101,token),summon->guids,roster));
    CHECK(owns(104,101,token+1));
    CHECK(owns(104,102,token));
    CHECK(registry.Register(201,{602,49,21,1,40},*RunManifest::Capture(201,{201,104})));
    CHECK(!MemberDungeonPreempts(!owns(104,101,token),{104},roster));
    ProjectDungeonAddBody(add,!owns(104,101,token));
    CHECK(!GuildAddSkipReason(add).empty());
}

LEEROY_TEST(MemberDungeonEntryAndHandbackNeedCurrentBodyProof)
{
    CHECK(DungeonEntrySource(true,false,true));
    CHECK(!DungeonEntrySource(false,false,true)); // Grouped yesterday at the saved origin, moved outside since.
    CHECK(!DungeonEntrySource(true,true,true)); // Tank in a new outdoor fight.
    CHECK(!DungeonEntrySource(true,false,false));
    CHECK(!DungeonHandback(true,false,false)); // A matching older DB snapshot cannot certify a skipped live master.
    CHECK(!DungeonHandback(true,true,true));
    CHECK(DungeonHandback(true,true,false));
    CHECK(DungeonHandback(false,false,false));
}

LEEROY_TEST(MemberDungeonDisbandProofSurvivesReconstructionAndSlotRequiresAPositiveHolder)
{
    nlohmann::json data={{"group",90},{"instance",47},{"leader",101},{"roster",{101,102,103,104,105}},
                         {"bodies",{1,2,3,4,5}}};
    CHECK(!DungeonReturnAuthorized(data));
    AuthorizeDungeonReturn(data);
    auto restored=nlohmann::json::parse(data.dump()); // Group object is gone after native restart.
    CHECK(DungeonReturnAuthorized(restored));
    for (auto key : {"group","instance","leader"})
    {
        auto foreign=restored; foreign[key]=700;
        CHECK(!DungeonReturnAuthorized(foreign));
    }
    restored["roster"][1]=77;
    CHECK(!DungeonReturnAuthorized(restored));
    CHECK(DungeonMatchesGroup(data,90,101,{101,102,103,104,105}));
    CHECK(!DungeonMatchesGroup(data,90,201,{201,202})); // Reused the numeric group ID.
    CHECK(!DungeonMatchesGroup(data,90,101,{101,202}));
    CHECK(DungeonCanReleaseSlot(true,600000,DungeonMatchesGroup(data,90,201,{201,202})));
    CHECK(!DungeonCanReleaseSlot(true,599999,false));
    CHECK(!DungeonCanReleaseSlot(true,600000,true));
    CHECK(DungeonCanReleaseSlot(true,600000,false));
}

LEEROY_TEST(MemberDungeonOnlyMatchesItsExactLostWriteBeforeAdvancing)
{
    nlohmann::json initial={{"effect","accepted"}}, entered={{"effect","enter_leader"}}, cleanup={{"effect","return_roster"}};
    DungeonWriteProof insert{0,nullptr,initial};
    CHECK(insert.Observe(0,nullptr)==DungeonWriteRead::Unavailable);
    CHECK(insert.Observe(1,&initial)==DungeonWriteRead::Committed);
    DungeonWriteProof enter{1,initial,entered};
    CHECK(enter.Observe(0,nullptr)==DungeonWriteRead::Unavailable);
    CHECK(enter.Observe(2,&cleanup)==DungeonWriteRead::Conflict);
    CHECK(enter.Observe(1,&initial)==DungeonWriteRead::Previous);
    CHECK(enter.Observe(2,&entered)==DungeonWriteRead::Committed);
    DungeonWriteProof undo{2,entered,cleanup};
    CHECK(undo.Observe(3,&cleanup)==DungeonWriteRead::Committed);
}

LEEROY_TEST(MemberDungeonOnlyValidatedOverlappingOwnerRequestPreempts)
{
    std::vector<std::uint32_t> participants{101,102,103,104,105};
    CHECK(!MemberDungeonPreempts(false, {101}, participants));
    CHECK(!MemberDungeonPreempts(true, {}, participants));
    CHECK(!MemberDungeonPreempts(true, {106}, participants));
    CHECK(MemberDungeonPreempts(true, {107,101}, participants));
}
