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
