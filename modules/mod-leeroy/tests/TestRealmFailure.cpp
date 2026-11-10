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

LEEROY_TEST(RealmFailure_AcceptsASkipOnlyForAnUntouchedOrdinaryLeaveMember)
{
    MemberSkipProof const clean{true, true, true};
    CHECK(AcceptsMemberSkip(clean));
    CHECK(!AcceptsMemberSkip({}));
    auto without = [&](auto change) { auto proof = clean; change(proof); return AcceptsMemberSkip(proof); };
    CHECK(!without([](auto& p) { p.leave = false; }));          // Only a leave skips a member.
    CHECK(!without([](auto& p) { p.stepUnchanged = false; }));  // Its own step changed it, or did not say.
    CHECK(!without([](auto& p) { p.exact = false; }));          // The body is not as admitted.
    CHECK(!without([](auto& p) { p.expectedScopes = true; }));  // The step expects a transition.
    CHECK(!without([](auto& p) { p.declaredScopes = true; }));  // An earlier shared cleanup touched it.
    CHECK(!without([](auto& p) { p.pendingScopes = true; }));   // A transition is awaited.
    CHECK(!without([](auto& p) { p.continuation = true; }));    // A native continuation owns it.
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
}
