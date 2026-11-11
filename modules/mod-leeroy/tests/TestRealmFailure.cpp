// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmFailure.h"
#include "LeeroyRealmCommands.h"
#include "TestHarness.h"

using namespace Leeroy::Realm;

LEEROY_TEST(RealmFailure_RequiresEveryPartOfTheWholeJobProof)
{
    CHECK(!ProvesNoBodyChange({}));
    // The first member, or any number of explicitly unchanged earlier members.
    CHECK(ProvesNoBodyChange({true, true}));
    CHECK(!ProvesNoBodyChange({true, false})); // An earlier member moved, or its result was unproved.
    CHECK(!ProvesNoBodyChange({false, true})); // Unknown outcome of this step, including a pending mutation.
    CHECK(!ProvesNoBodyChange({true, true, true})); // Native still holds the operation.
    CHECK(!ProvesNoBodyChange({true, true, false, true})); // Declared transitions.
    CHECK(!ProvesNoBodyChange({true, true, false, false, true})); // Native continuation.
    CHECK(!ProvesNoBodyChange({true, true, false, false, false, true})); // Guild join attempted.
    CHECK(!ProvesNoBodyChange({true, true, false, false, false, false, true})); // Shared cleanup changed a body.
}

LEEROY_TEST(RealmFailure_PublishesOnlyExplicitFailedJobKnowledge)
{
    RealmJob job{"leave", "leave_pvp", false, "wg_private_state_unavailable", 101};
    CHECK(!MakeRealmJob(job).contains("changed")); // Older/unproved producers keep the old wire shape.
    job.changed = false;
    auto refused = MakeRealmJob(job);
    CHECK_EQ(refused.at("changed"), false);
    CHECK_EQ(refused.at("state"), "failed");
    CHECK_EQ(refused.at("reason"), "wg_private_state_unavailable");
    job.changed = true;
    CHECK_EQ(MakeRealmJob(job).at("changed"), true);
    job.ok = true;
    CHECK(!MakeRealmJob(job).contains("changed")); // Never a successful job's refusal proof.
}

LEEROY_TEST(RealmFailure_OnlyIndependentMembersAndTheirOwnReasonsSkip)
{
    CHECK(SkipsMembers("leave_pvp"));
    CHECK(SkipsMembers("rearm"));
    for (auto kind : {"duel", "realm_move", "guild_grow", "guild_fishing", "rpg_status", "equip", "set_profession",
        "transmog_set", "bot_do", ""})
        CHECK(!SkipsMembers(kind));
    // 2026-11-10/11: every overnight rearm of 143 refused whole for one member in combat or dead.
    for (auto reason : {"busy:combat", "busy:dead", "busy:offline", "busy:teleport", "busy:duel", "not_actor",
        "weapon_record_unavailable", "recorded_weapon_unavailable", "equipment_changed",
        "cannot_equip_recorded_weapon", "offhand_occupied", "busy:raid_owner", "busy:training_state",
        "busy:body_scope_changed", "body_identity_changed", "shared_group_leader", "entry_point_unavailable"})
        CHECK(MemberSkipReason(reason));
    for (auto reason : {"realm_off", "busy:operation_identity", "busy:realm_owner", "unsupported", ""})
        CHECK(!MemberSkipReason(reason));
    // His group's members (busy:raid_owner at the ack) no longer refuse a whole rearm; a leave keeps
    // its whole-command ack refusals.
    CHECK(LeavesOutAtAck("rearm"));
    for (auto kind : {"leave_pvp", "duel", "realm_move", "guild_grow", "equip", ""})
        CHECK(!LeavesOutAtAck(kind));
}

LEEROY_TEST(RealmFailure_AcceptsASkipOnlyForAnUntouchedMember)
{
    MemberSkipProof clean;
    clean.kind = true; clean.memberReason = true; clean.stepUnchanged = true; clean.exact = true;
    CHECK(AcceptsMemberSkip(clean));
    CHECK(!AcceptsMemberSkip({}));
    auto without = [&](auto change) { auto proof = clean; change(proof); return AcceptsMemberSkip(proof); };
    CHECK(!without([](auto& p) { p.kind = false; }));           // Only a leave or a rearm skips a member.
    CHECK(!without([](auto& p) { p.memberReason = false; }));   // A reason about the job itself.
    CHECK(!without([](auto& p) { p.stepUnchanged = false; }));  // Its own step changed it, or did not say.
    CHECK(!without([](auto& p) { p.exact = false; }));          // The body is not as admitted.
    CHECK(!without([](auto& p) { p.expectedScopes = true; }));  // The step expects a transition.
    CHECK(!without([](auto& p) { p.declaredScopes = true; }));  // An earlier shared cleanup touched it.
    CHECK(!without([](auto& p) { p.pendingScopes = true; }));   // A transition is awaited.
    CHECK(!without([](auto& p) { p.continuation = true; }));    // A native continuation owns it.
    CHECK(!without([](auto& p) { p.touchedByCleanup = true; })); // A shared cleanup changed it.
    CHECK(without([](auto& p) { p.pendingStep = true; }));      // Its own step waited (tick budget), unchanged.
}

LEEROY_TEST(RealmFailure_AcceptsAPreStepSkipOnlyForAMemberWhoseStepNeverRan)
{
    // Tick's own fence (its lease, its holder fences, its admission) refused it before its executor
    // ran: a battleground that ended under a member still waiting its turn, a bot that logged out.
    MemberSkipProof fenced;
    fenced.kind = true; fenced.memberReason = true; fenced.stepRan = false;
    CHECK(AcceptsMemberSkip(fenced)); // Neither its own step's proof nor an exact body is needed.
    auto without = [&](auto change) { auto proof = fenced; change(proof); return AcceptsMemberSkip(proof); };
    CHECK(!without([](auto& p) { p.pendingStep = true; }));     // Its step ran before; it may have begun.
    CHECK(!without([](auto& p) { p.pendingScopes = true; }));
    CHECK(!without([](auto& p) { p.declaredScopes = true; }));  // An earlier shared cleanup declared it.
    CHECK(!without([](auto& p) { p.touchedByCleanup = true; }));
    CHECK(!without([](auto& p) { p.continuation = true; }));
    CHECK(!without([](auto& p) { p.kind = false; }));
    CHECK(!without([](auto& p) { p.memberReason = false; }));   // busy:operation_identity, busy:realm_owner.
}

LEEROY_TEST(RealmFailure_TheJobEventNamesSkippedMembersOnlyWhenThereAreSome)
{
    RealmJob job{"leave", "leave_pvp", true, "", 0, 2};
    CHECK(!MakeRealmJob(job).contains("skipped")); // The old shape for every job without skips.
    job.skipped = {1306, 304};
    auto done = MakeRealmJob(job);
    CHECK_EQ(done.at("state"), "done");
    CHECK(done.at("skipped") == nlohmann::json::array({1306, 304}));
    CHECK(!done.contains("changed"));
    CHECK_EQ(done.at("value"), 2u);
    job.ok = false; job.reason = "lfg_member_cleanup_unproved"; job.guid = 303;
    auto failed = MakeRealmJob(job);
    CHECK_EQ(failed.at("state"), "failed");
    CHECK(failed.at("skipped") == nlohmann::json::array({1306, 304}));
    CHECK(!failed.contains("changed"));
    // A rearm whose every member was in combat or dead still ends done, naming them all.
    RealmJob rearm{"rearm", "rearm", true, "", 0, 0};
    rearm.skipped = {101, 102};
    auto all = MakeRealmJob(rearm);
    CHECK_EQ(all.at("kind"), "rearm");
    CHECK_EQ(all.at("state"), "done");
    CHECK(all.at("skipped") == nlohmann::json::array({101, 102}));
    CHECK_EQ(all.at("value"), 0u);
}
