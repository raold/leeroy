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
