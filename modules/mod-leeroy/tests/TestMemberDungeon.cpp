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

LEEROY_TEST(MemberDungeonRecognizesOnlyTheUntouchedFirstGroupActor)
{
    nlohmann::json body = {{"changed",false},{"moved",false},{"returned",false},{"abandoned",false}};
    nlohmann::json data = {{"stage","grouping"},{"effect","create_group"},{"actor",101},{"leader",101},
        {"group",0},{"instance",0},{"bodies",{body,body,body,body,body}}};
    CHECK(DungeonFirstGroupActor(data,101));
    CHECK(!DungeonBeforeMutation(data));
    CHECK(!DungeonFirstGroupActor(data,102));
    for (auto key : {"actor","leader","group","instance"})
    {
        auto other = data; other[key] = 47;
        CHECK(!DungeonFirstGroupActor(other,101));
    }
    for (auto key : {"moved","changed","returned","abandoned"})
    {
        auto changed = data; changed["bodies"][4][key] = true;
        CHECK(!DungeonFirstGroupActor(changed,101));
    }
    data["stage"] = "running";
    CHECK(!DungeonFirstGroupActor(data,101));
    data["stage"] = "grouping"; data["effect"] = "add_group";
    CHECK(!DungeonFirstGroupActor(data,101));
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

LEEROY_TEST(MemberDungeonRestartHolderMustBeInItsCopyOrAtItsSavedOutdoorOrigin)
{
    nlohmann::json row={{"group",90},{"leader",101},{"roster",{101,102,103,104,105}}};
    nlohmann::json body={{"map",1},{"x",4.0},{"y",5.0},{"z",6.0}};
    CHECK(DungeonMatchesGroup(row,90,101,{101,104})); // Same leader and subset, same driver, reused group ID.
    CHECK(DungeonLocationOf(body,47,602,99,0,0,0)==DungeonLocation::Foreign);
    CHECK(DungeonCanReleaseSlot(true,600000,DungeonLocationOf(body,47,602,99,0,0,0)!=DungeonLocation::Foreign));
    CHECK(DungeonLocationOf(body,47,602,47,0,0,0)==DungeonLocation::Copy);
    CHECK(DungeonLocationOf(body,47,1,0,4,5,6)==DungeonLocation::Origin);
    CHECK(DungeonLocationOf(body,47,1,8,4,5,6)==DungeonLocation::Foreign);
    CHECK(DungeonLocationOf(body,47,1,0,60,70,80)==DungeonLocation::Foreign);
}

LEEROY_TEST(MemberDungeonStatusRequiresIndependentColumnsForTheExactReceipt)
{
    nlohmann::json row={{"version",1},{"map",602},{"difficulty",1},{"stage","tearing_down"},{"slot",false},
        {"settled",true},{"guild",21},{"human",1},{"leader",101},{"roster",{101,102,103,104,105}},
        {"bodies",nlohmann::json::array()},{"group",0},{"instance",0},{"issued_ms",1}};
    row["terminal"]={{"type","job"},{"id","lost"},{"kind","member_dungeon"},{"state","failed"},
        {"reason","world_restart"},{"guids",row["roster"]},{"instance_id",0},{"kills",0},{"wipes",0}};
    CHECK(ValidDungeonReceipt(row));
    CHECK(!DungeonStoredEvidence("lost",8,1,0,row));
    CHECK(!DungeonStoredEvidence("lost",8,0,0,row));
    CHECK(!DungeonStoredEvidence("lost",8,1,1,row));
    CHECK(!DungeonStoredEvidence("neighbor",8,0,1,row));
    row["terminal"]["id"]="neighbor";
    CHECK(DungeonStoredEvidence("neighbor",9,0,1,row));
}

LEEROY_TEST(MemberDungeonDelayedCharacterSaveDoesNotRewriteTheReturnTransition)
{
    DungeonReturnProof proof;
    using namespace std::chrono;
    std::vector<std::string> writes;
    unsigned worldChecks=0, storeChecks=0;
    bool checkedWhileReturning=false;
    auto save=[&](auto effect) { writes.push_back(effect); return true; };
    for (unsigned ms=0; ms<65000; ms+=10)
    {
        bool ready = proof.Observe(steady_clock::time_point{}+milliseconds(ms),save,
            [&]{ ++worldChecks; return ms>=100; },[&]{
                ++storeChecks;
                if (ms<100) checkedWhileReturning=true;
                return ms>=62000;
            });
        CHECK(ready==(ms>=62100));
    }
    CHECK((writes==std::vector<std::string>{"return_roster","returned"}));
    CHECK_EQ(worldChecks,11u);
    CHECK(checkedWhileReturning);
    CHECK_EQ(storeChecks,64u);
}

LEEROY_TEST(MemberDungeonHandbacksBelongToOneStartAndSurviveOnlyAsVerifiedEvidence)
{
    using Json=nlohmann::json;
    Json row={{"version",1},{"map",602},{"difficulty",1},{"stage","tearing_down"},{"slot",true},
        {"settled",false},{"guild",21},{"human",1},{"leader",101},{"roster",{101,102,103,104,105}},
        {"bodies",Json::array()},{"group",90},{"instance",47},{"issued_ms",1},{"terminal",nullptr}};
    for (unsigned guid=101; guid<=105; ++guid)
        row["bodies"].push_back({{"guid",guid},{"map",1},{"x",10},{"y",2},{"z",8},{"o",0},
            {"raid",0},{"dungeon",0},{"leader",false},{"changed",true},{"moved",true},
            {"returned",false},{"abandoned",false}});
    auto next=row;
    next["bodies"][0]["returned"]=true; // World observation alone is insufficient.
    CHECK(!DungeonHandbackRecorded(next,"original",101));
    next["handbacks"]["101"]="original";
    CHECK(DungeonHandbackRecorded(next,"original",101));
    CHECK(!DungeonWriteAllowed("original",10,row,next,100));
    CHECK(!DungeonWriteAllowed("original",10,row,next,100,102));
    CHECK(DungeonWriteAllowed("original",10,row,next,100,101));
    CHECK(!DungeonStoredEvidence("other",11,1,0,next));
    CHECK(!DungeonWriteAllowed("original",11,next,row,101));
    for (auto field : {"returned","abandoned"})
    {
        auto skipped=next;
        skipped["bodies"][0][field] = std::string_view(field)=="abandoned";
        CHECK(!DungeonStoredEvidence("original",11,1,0,skipped));
    }
    auto terminal=Json{{"type","job"},{"id","original"},{"kind","member_dungeon"},{"state","failed"},
        {"reason","world_restart"},{"guids",row["roster"]},{"instance_id",47},{"kills",0},{"wipes",0}};
    CHECK(!DungeonWriteAllowed("original",11,next,DungeonTerminalUpdate(next,terminal,true,101),101));
    auto unknown=row;
    unknown["effect"]="unknown_operation";
    CHECK(!DungeonStoredEvidence("original",10,1,0,unknown));
}

LEEROY_TEST(MemberDungeonFailedTerminalMustBeVerifiedBeforeItsSlotClockStarts)
{
    using Json=nlohmann::json;
    Json base={{"version",1},{"map",602},{"difficulty",1},{"stage","tearing_down"},{"slot",true},
        {"settled",false},{"guild",21},{"human",1},{"leader",101},{"roster",{101,102,103,104,105}},
        {"bodies",Json::array()},{"group",0},{"instance",0},{"issued_ms",1},{"terminal",nullptr}};
    Json failure={{"type","job"},{"id","lost"},{"kind","member_dungeon"},{"state","failed"},
        {"reason","cleanup_failed:preempted"},{"guids",base["roster"]},{"instance_id",0},{"kills",0},{"wipes",0}};
    auto invalid=base; invalid["slot"]=false;
    CHECK(!DungeonWriteAllowed("lost",1,base,invalid,700000));
    Json verified=base, stored=base;
    for (int ms=60000; ms<=700000; ms+=1000)
    {
        auto next=DungeonTerminalUpdate(verified,failure,false,ms);
        CHECK(DungeonWriteAllowed("lost",1,verified,next,ms));
        CHECK(!next.contains("failure_verified_ms"));
        CHECK(!DungeonFailureAge(verified,ms)); // Write fails throughout the original ten-minute deadline.
    }
    stored=DungeonTerminalUpdate(verified,failure,false,710000);
    DungeonWriteProof write{1,verified,stored};
    CHECK(write.Observe(0,nullptr)==DungeonWriteRead::Unavailable); // Committed; first verification lost.
    CHECK(!DungeonFailureAge(verified,1400000));
    CHECK(write.Observe(2,&stored)==DungeonWriteRead::Committed);
    verified=stored;
    verified["failed_ms"]=60000; // A legacy timestamp from an unverified write cannot authorize release.
    CHECK(!DungeonFailureAge(verified,1400000));
    auto confirmed=DungeonTerminalUpdate(verified,nullptr,false,1400000);
    CHECK(confirmed["failure_verified_ms"]==1400000);
    CHECK(DungeonWriteAllowed("lost",2,verified,confirmed,1400000));
    auto backdated=confirmed; backdated["failure_verified_ms"]=710000;
    CHECK(!DungeonWriteAllowed("lost",2,verified,backdated,1400000));
    verified=confirmed;
    auto release=verified; release["slot"]=false;
    CHECK(!DungeonWriteAllowed("lost",3,verified,release,1999999));
    CHECK(DungeonWriteAllowed("lost",3,verified,release,2000000));
    CHECK(!DungeonCanReleaseSlot(true,*DungeonFailureAge(verified,2000000),true));
    CHECK(DungeonCanReleaseSlot(true,*DungeonFailureAge(verified,2000000),false));
    auto returned=DungeonTerminalUpdate(verified,nullptr,true,1400010);
    CHECK(returned["terminal"]==failure && DungeonWriteAllowed("lost",3,verified,returned,1400010));
    auto premature=stored; premature["failure_verified_ms"]=710000;
    CHECK(!DungeonWriteAllowed("lost",1,base,premature,710000));
    auto replaced=release; replaced["terminal"]["reason"]="stopped";
    CHECK(!DungeonWriteAllowed("lost",3,verified,replaced,2000000));
    auto changedClock=verified; changedClock["failure_verified_ms"]=1;
    CHECK(!DungeonWriteAllowed("lost",3,verified,changedClock,2000000));
    CHECK(release["terminal"]==failure && release["settled"]==false);
    CHECK(DungeonStoredEvidence("lost",4,0,0,release));
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
