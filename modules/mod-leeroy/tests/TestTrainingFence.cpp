// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingFence.h"
#include "LeeroyTrainingProtocol.h"
#include "LeeroyProtocol.h"
#include "TestHarness.h"
#include <fstream>
#include <string>
#include <nlohmann/json.hpp>

using namespace Leeroy::Training;

LEEROY_TEST(Training_RefusalNamesTheCauseInOrder)
{
    CHECK_EQ(TrainingRefusal(true, true, false, false), "");
    CHECK_EQ(TrainingRefusal(true, true, false, true), "cleanup_pending");
    CHECK_EQ(TrainingRefusal(true, false, false, true), "training_off");
    CHECK_EQ(TrainingRefusal(true, true, true, true), "training_off"); // An off transition is still training_off.
    CHECK_EQ(TrainingRefusal(false, false, true, true), "guild_off");
}

LEEROY_TEST(Training_FenceCoversEveryBodyOnlyForTheBound)
{
    TrainingFence fence;
    CHECK(!fence.Pending());
    CHECK(!fence.Fencing(0));
    fence.Raise(1000);
    CHECK(fence.Pending());
    CHECK(fence.Fencing(1000));
    fence.Raise(500000); // A later failure in the same episode does not renew the bound.
    fence.Settle(true, 600000);
    CHECK(fence.Fencing(1000 + kTrainingFenceReleaseMs - 1));
    CHECK(!fence.TakeRelease(1000 + kTrainingFenceReleaseMs - 1));
    CHECK(!fence.Fencing(1000 + kTrainingFenceReleaseMs));
    CHECK(fence.Pending()); // Training stays refused until a pass proves cleanup.
    CHECK(fence.TakeRelease(1000 + kTrainingFenceReleaseMs));
    CHECK(!fence.TakeRelease(1000 + kTrainingFenceReleaseMs + 5000)); // Logged once.
    CHECK_EQ(fence.Elapsed(1000 + kTrainingFenceReleaseMs), kTrainingFenceReleaseMs);
    fence.Settle(false, 2000000);
    CHECK(!fence.Pending());
    CHECK(!fence.Fencing(2000000));
    CHECK(!fence.TakeRelease(9000000));
    fence.Raise(3000000); // A new episode fences again for a full bound.
    CHECK(fence.Fencing(3000000 + kTrainingFenceReleaseMs - 1));
    CHECK(fence.TakeRelease(3000000 + kTrainingFenceReleaseMs));
}

LEEROY_TEST(Training_CleanupReportOnChangeAtMostOncePerMinute)
{
    CleanupReport report;
    CHECK(!report.Due("", 0)); // Nothing pending: nothing to say.
    CHECK(report.Due("dummy_trial", 1000));
    CHECK(!report.Due("dummy_trial", 2000)); // Unchanged.
    CHECK(!report.Due("dummy_trial,journals(1)", 30000)); // Changed, but within the minute.
    CHECK(report.Due("dummy_trial,journals(1)", 1000 + kTrainingReportMs));
    CHECK(report.Due("", 1000 + kTrainingReportMs + 1)); // The all-clear is never deferred.
    CHECK(!report.Due("", 1000 + kTrainingReportMs + 2));
    CHECK(report.Due("journals(2)", 1000 + 2 * kTrainingReportMs + 1));
}

LEEROY_TEST(Training_DummyCleanupReceiptShape)
{
    auto j = MakeDummyCleanup("abc", "released", {2984, 1924}, 600123);
    CHECK_EQ(j["type"], "dummy_cleanup");
    CHECK_EQ(j["id"], "abc");
    CHECK_EQ(j["state"], "released");
    CHECK(j["unfinished"] == nlohmann::json::array({2984, 1924}));
    CHECK_EQ(j["after_ms"], 600123);
    CHECK(MakeDummyCleanup("abc", "restored", {}, 0)["unfinished"] == nlohmann::json::array());
}

LEEROY_TEST(Training_DummyCleanupMatchesTheSharedFixture)
{
    std::ifstream file(LEEROY_TEST_FIXTURES "/training_protocol.jsonl");
    nlohmann::json expected;
    for (std::string line; std::getline(file, line);)
        if (!line.empty())
            if (auto row = nlohmann::json::parse(line); row.at("name") == "dummy_cleanup") expected = row.at("msg");
    CHECK(!expected.is_null());
    auto line = Leeroy::EncodeEvent(0, MakeDummyCleanup("g6", "released", {1101}, 600000));
    CHECK(nlohmann::json::parse(line) == expected);
}
