// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmRelease.h"
#include "LeeroyRealmCommands.h"
#include "LeeroyRealmCleanup.h"
#include "TestHarness.h"
#include <set>
using namespace Leeroy::Realm;

LEEROY_TEST(RealmRelease_ReadbackSkipsOnlyProvablyUntouchedMembersForEveryCheck)
{
    for (auto check : LeaveReadbackChecks)
    {
        CHECK(JudgeLeaveReadback(check, {}, true) == LeaveReadback::Skip);
        for (auto proof : {LeaveChangeProof{true}, LeaveChangeProof{false,true},
            LeaveChangeProof{false,false,true}, LeaveChangeProof{false,false,false,true},
            LeaveChangeProof{false,false,false,false,true}}) // current step failed after mutation, no success receipt yet
            CHECK(JudgeLeaveReadback(check, proof, true) == LeaveReadback::Failed);
    }
    CHECK(JudgeLeaveReadback("", {}, false) == LeaveReadback::Ready);
    CHECK(JudgeLeaveReadback("future", {}, true) == LeaveReadback::Failed);
}
LEEROY_TEST(RealmRelease_TransientReadbacksWaitThenJudgeOwnership)
{
    for (auto check : {"teleport", "exit"})
        for (bool changed : {false, true})
        {
            CHECK(JudgeLeaveReadback(check, {changed}, false) == LeaveReadback::Pending);
            CHECK(JudgeLeaveReadback(check, {changed}, true) == (changed ? LeaveReadback::Failed : LeaveReadback::Skip));
        }
}
LEEROY_TEST(RealmRelease_Failed109MemberLeaveRetainsOnlyChangedBodies)
{
    std::vector<std::uint32_t> manifest;
    for (std::uint32_t guid=101; guid<210; ++guid) manifest.push_back(guid);
    std::set<std::uint32_t> held(manifest.begin(), manifest.end());
    auto const released = ReleaseUntouched(manifest, [](auto guid) {
        return MayHaveChanged({guid==102, guid==103, guid==104, false, guid==209});
    }, [&](auto guid) { held.erase(guid); });
    CHECK_EQ(released.size(), 105u);
    CHECK(held == std::set<std::uint32_t>({102,103,104,209}));
    RealmJob job{"leave", "leave_pvp", false, "cleanup_manifest_readback_changed", 209};
    job.released = released; job.readbackGuid = 102; job.check = "owner";
    auto wire = MakeRealmJob(job);
    CHECK_EQ(wire.at("readback_guid"), 102); CHECK_EQ(wire.at("guid"), 209);
    CHECK_EQ(wire.at("released").size(), 105u); CHECK_EQ(wire.at("check"), "owner");
}
LEEROY_TEST(RealmRelease_ForgetRefusalsNeverCallRelease)
{
    ForgetFacts safe{true,true,"leave_pvp",true};
    for (auto kind : {"leave_pvp", "rearm"})
    {
        safe.kind = kind;
        unsigned calls=0;
        CHECK(ApplyForget(safe, [&] { ++calls; }) == "released"); CHECK_EQ(calls,1u);
    }
    auto refused = [&](ForgetFacts facts, std::string_view expected) {
        unsigned calls=0;
        CHECK(ApplyForget(facts, [&] { ++calls; }) == expected); CHECK_EQ(calls,0u);
    };
    refused({}, "not_held");
    auto f=safe; f.retained=false; refused(f,"running");
    f=safe; f.kind="guild_grow"; refused(f,"kind");
    f=safe; f.ordinary=false; refused(f,"kind");
    f=safe; f.scopes=true; refused(f,"kind");
    f=safe; f.continuation=true; refused(f,"kind");
    f=safe; f.guildJoins=true; refused(f,"kind");
    f=safe; f.teleport=true; refused(f,"busy:teleport");
}
LEEROY_TEST(RealmRelease_ForgetWireRequiresDistinctBoundedTarget)
{
    std::string error;
    for (auto const& target : {std::string(""), std::string("bad id"), std::string(129,'a'), std::string("request")})
    {
        error.clear();
        CHECK(!ParseRealmForget({{"id","request"},{"origin_id",target}},error));
        CHECK_EQ(error,"bad field: origin_id");
    }
    error.clear(); CHECK(ParseRealmForget({{"id","request"},{"origin_id","old_root-1"}},error));
    for (auto outcome : {"released","not_held","running","kind","busy:teleport"})
    {
        auto wire=MakeRealmForgotten("request","original",outcome,std::string_view(outcome)=="released"?3:0);
        CHECK_EQ(wire.at("id"),"request"); CHECK_EQ(wire.at("origin_id"),"original"); CHECK_EQ(wire.at("outcome"),outcome);
    }
}
LEEROY_TEST(RealmRelease_GroupReceiptNamesActualBadMemberNotHealthyFirst)
{
    GroupCleanupReadback healthy{101,true,true,true,true,true,false,false,true,false};
    auto bad=healthy; bad.guid=102; bad.arrived=false;
    CHECK_EQ(GroupCleanupFault({101,102},{healthy,bad}),102u);
    bad=healthy; bad.guid=102; bad.sameLife=false;
    CHECK_EQ(GroupCleanupFault({101,102},{healthy,bad}),102u);
    bad=healthy; bad.guid=102; bad.teleporting=true; bad.expectedTeleport=true;
    CHECK_EQ(GroupCleanupFault({101,102},{healthy,bad}),102u);
    auto unproved=healthy; unproved.guid=103; unproved.lfgStateNone=false;
    CHECK_EQ(GroupCleanupFault({101,102,103},{healthy,bad,unproved}),103u); // failure outranks transient
    CHECK_EQ(GroupCleanupFault({101},{healthy}),0u);
}
LEEROY_TEST(RealmRelease_CleanupFaultsNameTheirMemberWithAFixedCode)
{
    CHECK(CleanupFaultCheck("").empty());
    CHECK(CleanupFaultCheck("cleanup_member_offline") == "offline");
    CHECK(CleanupFaultCheck("busy:body_unavailable") == "offline");
    CHECK(CleanupFaultCheck("wg_private_state_unavailable") == "queue");
    CHECK(CleanupFaultCheck("shared_lfg_proposal") == "lfg");
    CHECK(CleanupFaultCheck("shared_lfg_participants_unavailable") == "lfg");
    CHECK(CleanupFaultCheck("cleanup_member_busy") == "lfg");
    CHECK(CleanupFaultCheck("cleanup_member_not_admitted") == "lfg");
    for (auto reason : {"busy:training_cleanup", "busy:training_state", "busy:realm_owner", "busy:raid_owner",
        "busy:operation_identity", "not_bot", "not_actor", "realm_off"})
        CHECK(CleanupFaultCheck(reason) == "owner");
    // Every fault the recheck can report maps to a code the mind accepts beside readback_guid.
    for (auto reason : {"cleanup_member_offline", "wg_private_state_unavailable", "shared_lfg_proposal",
        "busy:training_cleanup", "anything_new"})
        CHECK(ReadbackCheck(CleanupFaultCheck(reason)));
}
LEEROY_TEST(RealmRelease_JobNeverCarriesAReadbackWithoutAFixedCode)
{
    RealmJob job{"leave", "leave_pvp", false, "cleanup_manifest_readback_changed", 209};
    job.readbackGuid = 102;
    for (auto check : {"", "private text", "Owner"})
    {
        job.check = check;
        auto wire = MakeRealmJob(job);
        CHECK(!wire.contains("readback_guid")); CHECK(!wire.contains("check"));
    }
    job.check = "queue";
    auto wire = MakeRealmJob(job);
    CHECK_EQ(wire.at("readback_guid"), 102); CHECK_EQ(wire.at("check"), "queue");
    job.ok = true; job.reason.clear();
    CHECK(!MakeRealmJob(job).contains("readback_guid"));
}
