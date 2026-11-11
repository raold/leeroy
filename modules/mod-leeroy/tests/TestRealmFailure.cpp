// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmFailure.h"
#include "LeeroyRealmCommands.h"
#include "LeeroyRealmPolicy.h"
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
    // An allow-list: a job-wide fence or anything not on it keeps the old rule. busy:training_cleanup
    // is pending for every body at once; named a member's, it skipped each member after it and then
    // stranded the leave at its final readback (the #235 review's finding 1).
    for (auto reason : {"realm_off", "busy:operation_identity", "busy:realm_owner", "unsupported", "",
        "busy:training_cleanup", "lfg_cleanup_disabled", "busy:tick_budget", "busy:queue_full",
        "bad field: affected_bodies", "busy:duplicate_operation", "operation_transition_unknown",
        "native_cleanup_unproved", "some_future_reason", "busy:combat:extra"})
        CHECK(!MemberSkipReason(reason));
    // Every reason BusyReason can give, for every purpose, is a member's own.
    for (unsigned bits = 0; bits < (1u << 11); ++bits)
    {
        BodyState state{(bits & 1) != 0, (bits & 2) != 0, (bits & 4) != 0, (bits & 8) != 0, (bits & 16) != 0,
            (bits & 32) != 0, (bits & 64) != 0, (bits & 128) != 0, (bits & 256) != 0, (bits & 512) != 0,
            (bits & 1024) != 0};
        for (auto purpose : {Purpose::Move, Purpose::Status, Purpose::LeavePvp, Purpose::Rearm, Purpose::WgJoin,
            Purpose::Duel, Purpose::Craft, Purpose::Bank, Purpose::Equip, Purpose::Mail})
            if (auto why = BusyReason(state, purpose); !why.empty()) CHECK(MemberSkipReason("busy:" + why));
    }
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

LEEROY_TEST(RealmFailure_TicksFenceSkipsOnlyAnUntouchedMemberForItsOwnReason)
{
    // A leave's member still waiting its turn whose battleground ended under it.
    FenceFacts waiting;
    waiting.kind = "leave_pvp"; waiting.reason = "busy:body_scope_changed"; waiting.lease = true;
    CHECK(DecideFence(waiting) == FenceOutcome::Skip);
    auto with = [&](auto change) { auto facts = waiting; change(facts); return DecideFence(facts); };
    CHECK(with([](auto& f) { f.last = true; }) == FenceOutcome::ReadbackThenSkip); // the job's final readback first
    CHECK(with([](auto& f) { f.kind = "rearm"; f.last = true; }) == FenceOutcome::Skip); // a rearm has none
    CHECK(with([](auto& f) { f.kind = "rearm"; f.reason = "body_identity_changed"; }) == FenceOutcome::Skip);
    CHECK(with([](auto& f) { f.reason = "busy:raid_owner"; }) == FenceOutcome::Skip);
    // The old rule: finish, released with changed:false when nothing changed yet.
    CHECK(with([](auto& f) { f.lease = false; }) == FenceOutcome::OldRule);
    CHECK(with([](auto& f) { f.pending = true; }) == FenceOutcome::OldRule);    // its step ran or waited
    CHECK(with([](auto& f) { f.declared = true; }) == FenceOutcome::OldRule);   // a shared cleanup declared it
    CHECK(with([](auto& f) { f.touchedByCleanup = true; }) == FenceOutcome::OldRule);
    CHECK(with([](auto& f) { f.continuation = true; }) == FenceOutcome::OldRule);
    for (auto kind : {"realm_move", "duel", "guild_grow", "guild_fishing", "equip"})
        CHECK(with([&](auto& f) { f.kind = kind; }) == FenceOutcome::OldRule);
    for (auto reason : {"busy:operation_identity", "busy:realm_owner", "unsupported", "realm_off", ""})
        CHECK(with([&](auto& f) { f.reason = reason; }) == FenceOutcome::OldRule);
}

LEEROY_TEST(RealmFailure_ATrainingCleanupFenceMidLeaveOrRearmKeepsTheOldRule)
{
    // The #235 review's finding 1: a training cleanup turns pending while a large leave or rearm runs.
    // Every member after it would see busy:training_cleanup. Each keeps the old rule, so the job ends
    // at the first of them: released with changed:false when nothing changed yet, as before #235,
    // never skipping its way to a final readback that would retain the whole manifest.
    for (auto kind : {"leave_pvp", "rearm"})
        for (bool last : {false, true})
        {
            FenceFacts facts;
            facts.kind = kind; facts.reason = "busy:training_cleanup"; facts.lease = true; facts.last = last;
            CHECK(DecideFence(facts) == FenceOutcome::OldRule);
        }
    MemberSkipProof ran;
    ran.kind = true; ran.memberReason = MemberSkipReason("busy:training_cleanup"); ran.stepUnchanged = true; ran.exact = true;
    CHECK(!AcceptsMemberSkip(ran)); // nor at the member's own step (a rearm's Eligible, a leave's recheck)
}

LEEROY_TEST(RealmFailure_ADoneJobNamesItsMemberByTheAdmittedManifest)
{
    std::vector<std::size_t> const admitted{1, 3, 143};
    CHECK_EQ(DoneJobGuid(admitted[0], 101), 101u);
    CHECK_EQ(DoneJobGuid(admitted[1], 101), 0u); // a rearm of three that left two out at its ack: the mind sent three
    CHECK_EQ(DoneJobGuid(admitted[2], 1306), 0u);
}
