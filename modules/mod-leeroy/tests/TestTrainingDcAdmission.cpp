// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingDcAdmission.h"
#include "LeeroyTrainingOverrides.h"
#include "LeeroyRealmContinuations.h"
#include "TestHarness.h"
#include <map>
#include <set>

using namespace Leeroy::Training;

LEEROY_TEST(Training_DcAdmissionRefusesHeldLeaderBeforeCreatingOwnership)
{
    std::map<std::uint32_t, std::string> holders{{11001, "realm-queued"}};
    auto owns = [&](std::uint32_t guid) { return holders.contains(guid); };
    CHECK(!CaptureUnownedDcRun(11001, {11001, 11002}, owns));
    // The selected leader is independently checked even with an invalid roster
    // that omits it; a roster projection cannot waive its holder.
    CHECK(!CaptureUnownedDcRun(11001, {11002}, owns));
    holders.clear();
    CHECK(CaptureUnownedDcRun(11001, {11001, 11002}, owns));
}

LEEROY_TEST(Training_DcAdmissionChecksOfflineAndNoncastCurrentSlots)
{
    std::set<std::uint32_t> held{11003};
    std::vector<std::uint32_t> queried;
    auto owns = [&](std::uint32_t guid) { queried.push_back(guid); return held.contains(guid); };
    // Only the leader is online/cast in this adapter input. The native caller
    // supplies GetMemberSlots, including this final offline/noncast body.
    CHECK(!CaptureUnownedDcRun(11001, {11001, 11002, 11003}, owns));
    CHECK(std::find(queried.begin(), queried.end(), 11003) != queried.end());
    held = {11002}; // A nonleader holder is sufficient regardless of slot order.
    CHECK(!CaptureUnownedDcRun(11001, {11003, 11002, 11001}, owns));
}

LEEROY_TEST(Training_DcAdmissionRejectsDetachedUnknownOwnerAfterRealmDisable)
{
    using namespace Leeroy::Realm;
    ContinuationLedger ledger("0123456789abcdef0123456789abcdef");
    std::string error;
    auto owner = ledger.Prepare("arena-queue", PvpKind::RatedArena, {11002}, 7, 30, 1, 1, error);
    CHECK(owner.has_value());
    if (!owner) return;
    CHECK(ledger.Detach(owner->token));
    ledger.Unknown(owner->token, "native_cleanup_pending");
    ledger.FenceAll("realm_off");
    auto retained = ledger.Get(owner->token);
    CHECK(retained && retained->stage == PvpStage::Unknown);
    auto owns = [&](std::uint32_t guid)
    {
        auto current = ledger.Get(owner->token);
        return current && std::find(current->members.begin(), current->members.end(), guid) != current->members.end();
    };
    CHECK(!CaptureUnownedDcRun(11001, {11001, 11002}, owns));
    CHECK_EQ(ledger.Size(), std::size_t{1});
    CHECK_EQ(ledger.Get(owner->token)->reason, std::string("native_cleanup_pending"));
}

LEEROY_TEST(Training_DcAdmissionIncludesGuildFishingHoldersAndCapturesUnheldRoster)
{
    using namespace Leeroy::Realm;
    ContinuationLedger ledger("0123456789abcdef0123456789abcdef");
    std::string error;
    auto owner = ledger.Prepare("guild-fishing", ContinuationKind::Fishing, {11002}, 7, 30, 1, 1, error,
        ActivityDomain::Guild);
    CHECK(owner.has_value());
    if (!owner) return;
    ledger.FenceDomain(ActivityDomain::Realm, "realm_off");
    auto owns = [&](std::uint32_t guid)
    {
        auto current = ledger.Get(owner->token);
        return current && std::find(current->members.begin(), current->members.end(), guid) != current->members.end();
    };
    CHECK(!CaptureUnownedDcRun(11001, {11001, 11002}, owns));
    CHECK_EQ(ledger.Size(), std::size_t{1});
    CHECK(!ledger.Get(owner->token)->applicationFenced);
    auto unheld = [](std::uint32_t) { return false; };
    auto manifest = CaptureUnownedDcRun(11001, {11003, 11001, 11002}, unheld);
    CHECK(manifest && manifest->Known());
    if (!manifest) return;
    CHECK((manifest->Members() == std::vector<std::uint32_t>{11001, 11002, 11003}));
    TrainingRuns jobs;
    RunScope scope{631, 41, 12001, 11998, 200};
    auto token = jobs.Register(11001, scope, *manifest);
    CHECK(token != 0);
    CHECK(jobs.Owns(11001)); CHECK(jobs.Owns(11002)); CHECK(jobs.Owns(11003));
    CHECK(!jobs.Owns(11004));
    // Invalid full-roster identity remains invalid even without Realm holders.
    CHECK(!CaptureUnownedDcRun(11001, {11002}, unheld));
    CHECK(!CaptureUnownedDcRun(11001, {11001, 11002, 11002}, unheld));
}

LEEROY_TEST(Training_DcAdmissionFencePreservesExistingExactCleanupEvidence)
{
    auto owns = [](std::uint32_t) { return false; };
    auto manifest = CaptureUnownedDcRun(11001, {11001, 11002}, owns);
    CHECK(manifest.has_value());
    if (!manifest) return;
    TrainingRuns jobs;
    auto token = jobs.Register(11001, {631, 41, 12001, 11998, 200}, *manifest);
    CHECK(token != 0);
    TrainingOverrides journal;
    CHECK(journal.NoteApplied("WaitAtBoss", {true, 0}, {true, 1}));
    CHECK(journal.NoteApplied("PartyMaxSpread", {false, 40}, {true, 50}));
    auto held = [](std::uint32_t guid) { return guid == 11002; };
    CHECK(!CaptureUnownedDcRun(11001, {11001, 11002}, held));
    CHECK_EQ(jobs.Find(11001)->token, token);
    CHECK(jobs.Owns(11002));
    CHECK(!jobs.End(11001, token + 1)); // Refused admission cannot authorize stale cleanup.
    auto restore = journal.Undo("WaitAtBoss", {true, 1});
    CHECK(restore.action == OverrideUndo::Restore); CHECK_EQ(restore.value, 0);
    CHECK(journal.Undo("PartyMaxSpread", {true, 50}).action == OverrideUndo::Remove);
    CHECK(journal.Undo("PartyMaxSpread", {true, 60}).action == OverrideUndo::PreserveExternal);
    journal.Forget("WaitAtBoss"); journal.Forget("PartyMaxSpread");
    CHECK(journal.Empty());
    CHECK(jobs.End(11001, token)); // Exact existing cleanup does not perform fresh admission.
    CHECK(!jobs.Owns(11001)); CHECK(!jobs.Owns(11002));
}
