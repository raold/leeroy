// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingRuns.h"
#include "LeeroyTrainingProtocol.h"
#include "TestHarness.h"

using namespace Leeroy::Training;

LEEROY_TEST(Training_PrimedDefinitionRemainsImmutableAfterCacheReset)
{
    CHECK(PrimeOwnedEntry("", "first", "") == PrimeDecision::Write);
    CHECK(PrimeOwnedEntry("first", "first", "first") == PrimeDecision::Same);
    CHECK(PrimeOwnedEntry("", "first", "first") == PrimeDecision::Write); // Same-definition repair.
    CHECK(PrimeOwnedEntry("", "replacement", "first") == PrimeDecision::Taken);
    CHECK(PrimeOwnedEntry("foreign", "first", "first") == PrimeDecision::Taken);
}

LEEROY_TEST(Training_RestoreRepairsOnlyItsRecordedImmutableCacheDefinition)
{
    std::map<std::string, std::string> cache;
    std::map<std::string, std::string, std::less<>> recorded{{"tg_owned", "validated definition"}, {"tg_empty", ""}};
    CHECK(!RestorePrimedEntry(cache, recorded, "tg_new"));
    CHECK(!RestorePrimedEntry(cache, recorded, "tg_empty"));
    CHECK(cache.empty());
    CHECK(RestorePrimedEntry(cache, recorded, "tg_owned"));
    CHECK_EQ(cache.at("tg_owned"), "validated definition");
    CHECK(RestorePrimedEntry(cache, recorded, "tg_owned"));
    cache.at("tg_owned").clear(); // Same Reset() behavior as playerbots, while cleanup's public gate is closed.
    CHECK(RestorePrimedEntry(cache, recorded, "tg_owned"));
    CHECK_EQ(cache.at("tg_owned"), "validated definition");
    cache.at("tg_owned") = "different external definition";
    CHECK(!RestorePrimedEntry(cache, recorded, "tg_owned"));
    CHECK_EQ(cache.at("tg_owned"), "different external definition");
    CHECK_EQ(recorded.at("tg_owned"), "validated definition");
}

LEEROY_TEST(Training_JobReceiptsRejectStaleTeardownAndConcurrentReplacement)
{
    TrainingRuns jobs;
    RunScope original{631, 41, 7, 100, 200};
    auto first = jobs.Register(101, original);
    CHECK(first != 0);
    CHECK_EQ(jobs.Register(101, original), 0);
    CHECK(!jobs.End(101, 0));
    CHECK(jobs.Find(101)->scope == original);
    CHECK(jobs.End(101, first));
    RunScope next{631, 42, 7, 100, 201};
    auto second = jobs.Register(101, next);
    CHECK(second != 0 && first != second);
    CHECK(!jobs.End(101, first));
    CHECK(jobs.Find(101)->scope == next);
    CHECK_EQ(jobs.Find(101)->token, second);
}

LEEROY_TEST(Training_JobReceiptAlwaysGetsANewTokenAndRejectsMissingIdentity)
{
    TrainingRuns jobs;
    RunScope scope{0, 0, 7, 100, 200}; // Non-instance map zero is a valid exact scope.
    auto control = jobs.NextToken();
    auto first = jobs.Register(101, scope);
    CHECK(first > control);
    CHECK(jobs.End(101, first));
    auto second = jobs.Register(101, scope);
    CHECK(second > first);
    CHECK(!jobs.End(101, control));
    CHECK(!jobs.End(101, first));
    CHECK_EQ(jobs.Find(101)->token, second);
    CHECK_EQ(jobs.Register(102, {0, 0, 7, 100, 0}), 0);
}

LEEROY_TEST(Training_JobScopeIncludesGroupInstanceAndConfiguredOwner)
{
    RunScope scope{631, 41, 7, 100, 200};
    auto altered = scope;
    altered.instance = 42;
    CHECK(!(scope == altered));
    altered = scope;
    altered.group = 201;
    CHECK(!(scope == altered));
    altered = scope;
    altered.human = 101;
    CHECK(!(scope == altered));
    altered = scope;
    altered.guild = 8;
    CHECK(!(scope == altered));
}

LEEROY_TEST(Training_AdmissionManifestIsBoundedUniqueAndDetached)
{
    std::vector<std::uint32_t> original{11003, 11001, 11002};
    auto captured = RunManifest::Capture(11001, original);
    CHECK(captured && captured->Known());
    if (!captured) return;
    original.clear(); original.push_back(11999);
    CHECK((captured->Members() == std::vector<std::uint32_t>{11001,11002,11003}));
    CHECK(captured->Owns(11001)); CHECK(captured->Owns(11002)); CHECK(captured->Owns(11003));
    CHECK(!captured->Owns(11999)); CHECK(!captured->Owns(0));
    CHECK(!RunManifest::Capture(0, {11001}));
    CHECK(!RunManifest::Capture(11001, {}));
    CHECK(!RunManifest::Capture(11001, {11002}));
    CHECK(!RunManifest::Capture(11001, {11001,0}));
    CHECK(!RunManifest::Capture(11001, {11001,11001}));
    std::vector<std::uint32_t> full;
    for (std::uint32_t i = 0; i < kMaxRunMembers; ++i) full.push_back(11001+i);
    CHECK(RunManifest::Capture(11001, full));
    full.push_back(11999);
    CHECK(!RunManifest::Capture(11001, full));
}

LEEROY_TEST(Training_OriginalBodiesRemainOwnedWhenLeaderOrGroupDisappears)
{
    auto captured = RunManifest::Capture(11001, {11001,11002});
    CHECK(captured.has_value());
    if (!captured) return;
    RunScope scope{631,41,12001,11998,200};
    // Missing online leader/group provides no live members. Admission proof
    // still excludes every original body throughout hold/override cleanup.
    CHECK(RunOwnsBody(*captured, scope, 11001));
    CHECK(RunOwnsBody(*captured, scope, 11002));
    CHECK(!RunOwnsBody(*captured, scope, 11003));
    // A later member of the same original group is also affected by live DC.
    CHECK(RunOwnsBody(*captured, scope, 11003, 200, true));
    CHECK(!RunOwnsBody(*captured, scope, 11003, 200, false));
    // A replacement group is not the original scope. It cannot clear original
    // ownership or make its unrelated members part of that old live group.
    CHECK(RunOwnsBody(*captured, scope, 11002, 201, false));
    CHECK(!RunOwnsBody(*captured, scope, 11003, 201, true));
    CHECK(!RunOwnsBody(*captured, scope, 11003, 0, true));
    CHECK(!RunOwnsBody(*captured, scope, 0, 200, true));
}

LEEROY_TEST(Training_JobOwnershipUsesOriginalManifestUntilExactTokenEnd)
{
    TrainingRuns jobs;
    RunScope scope{631,41,12001,11998,200};
    auto firstManifest = RunManifest::Capture(11001, {11001,11002});
    auto nextManifest = RunManifest::Capture(11001, {11001,11003});
    CHECK(firstManifest && nextManifest);
    if (!firstManifest || !nextManifest) return;
    auto first = jobs.Register(11001, scope, *firstManifest);
    CHECK(first != 0); CHECK(jobs.Owns(11001)); CHECK(jobs.Owns(11002)); CHECK(!jobs.Owns(11003));
    CHECK(!jobs.End(11001, 0)); CHECK(jobs.Owns(11002));
    CHECK_EQ(jobs.Register(11001, scope, *nextManifest), 0);
    CHECK(jobs.Owns(11002));
    CHECK(jobs.End(11001, first)); CHECK(!jobs.Owns(11002));
    scope.instance = 42; scope.group = 201;
    auto next = jobs.Register(11001, scope, *nextManifest);
    CHECK(next != 0 && next != first);
    CHECK(!jobs.End(11001, first));
    CHECK(jobs.Owns(11001)); CHECK(jobs.Owns(11003)); CHECK(!jobs.Owns(11002));
    CHECK_EQ(jobs.Find(11001)->token, next);
    CHECK(jobs.End(11001, next)); CHECK(!jobs.Owns(11001)); CHECK(!jobs.Owns(11003));
}

LEEROY_TEST(Training_UnknownLegacyManifestFailsClosedWithoutASecondRegistry)
{
    RunManifest legacy;
    RunScope scope{631,41,12001,11998,200};
    CHECK(!legacy.Known()); CHECK(legacy.Members().empty());
    CHECK(RunOwnsBody(legacy, scope, 11001)); CHECK(RunOwnsBody(legacy, scope, 11999));
    CHECK(!RunOwnsBody(legacy, scope, 0));
    TrainingRuns jobs;
    CHECK(!jobs.Owns(11999));
    auto token = jobs.Register(11001, scope); // Compatibility call: missing membership is unknown.
    CHECK(token != 0); CHECK(jobs.Owns(11999));
    CHECK(!jobs.End(11001, token + 1)); CHECK(jobs.Owns(11999));
    CHECK(jobs.End(11001, token)); CHECK(!jobs.Owns(11999));
    auto otherLeader = RunManifest::Capture(11002, {11002,11003});
    CHECK(otherLeader.has_value());
    if (otherLeader) CHECK_EQ(jobs.Register(11001, scope, *otherLeader), 0);
}
