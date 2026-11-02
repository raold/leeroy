// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingCleanup.h"
#include "LeeroyTrainingProtocol.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyRaidRecovery.h"
#include "LeeroyBotJournal.h"
#include "TestHarness.h"
#include <limits>
#include <stdexcept>

using namespace Leeroy::Training;
using json = nlohmann::json;

namespace
{
RunScope Scope() { return {631, 77, 10, 1, 987}; }
std::string Open(CleanupReceipts& receipts, std::uint64_t token = 1)
{ return receipts.Register("original_job", token, 101, Scope(), {102, 101}); }
}

LEEROY_TEST(Training_CleanupCannotCompleteBeforeSealedDrainedRestoreAndTeardown)
{
    CleanupReceipts receipts("incarnation_a");
    auto handle = Open(receipts);
    CHECK(!handle.empty());
    CHECK(receipts.Admits(handle, {101, 102}));
    CHECK(!receipts.Admits(handle, {103}));
    CHECK(!receipts.Admits(handle, {}));
    CHECK(!receipts.Restored(handle, true));
    CHECK(!receipts.Complete(handle, true, true));
    receipts.Seal(handle);
    CHECK(!receipts.Admits(handle, {101}));
    CHECK(!receipts.Restored(handle, false)); // An acknowledged queue entry can still execute.
    CHECK(!receipts.Complete(handle, true, true));
    CHECK(receipts.Restored(handle, true));
    CHECK(!receipts.Complete(handle, false, true));
    CHECK(!receipts.Complete(handle, true, false)); // Partial return is not native completion.
    CHECK(receipts.Complete(handle, true, true));
    CHECK(receipts.Complete(handle, true, true));
    CHECK(!receipts.Admits(handle, {101}));
    CHECK(receipts.Find(handle)->phase == CleanupReceipt::Phase::Complete);
}

LEEROY_TEST(Training_CleanupRevocationCannotAdoptReplacementBodiesOrReopen)
{
    CleanupReceipts receipts("incarnation_a");
    auto handle = Open(receipts);
    Leeroy::Guild::RaidBodyLease body{11, 12, 13};
    CHECK(body.Matches(11, 12, 13));
    CHECK(!body.Matches(11, 22, 13));
    body.Revoke();
    receipts.Revoke(handle);
    CHECK(!body.Matches(11, 12, 13)); // Address reuse after logout does not rescue the lease.
    CHECK(!receipts.Admits(handle, {101}));
    CHECK(!receipts.Restored(handle, true));
    CHECK(!receipts.Complete(handle, true, true));
    CHECK(Open(receipts).empty());
}

LEEROY_TEST(Training_ScopedControlsCannotDestroyTheirCleanupScope)
{
    CHECK(ScopedRaidControlAllowed("dc_pause", "resume"));
    for (auto action : {"disband", "dc_on", "dc_off", "ready_check"})
        CHECK(!ScopedRaidControlAllowed(action, "resume"));
    for (auto param : {"", "pause", "toggle", "resume "})
        CHECK(!ScopedRaidControlAllowed("dc_pause", param));
    for (auto action : {"formation", "co", "nc", "value", "restore"})
        CHECK(ScopedBotMutationAllowed(action));
    for (auto action : {"rti", "cast", "use", "do"})
        CHECK(!ScopedBotMutationAllowed(action));
}

LEEROY_TEST(Training_ScopedFormationReplacementKeepsTheFirstBaseline)
{
    BotJournal journal;
    std::string formation = "arrow";
    auto mutate = [&](std::string value)
    {
        journal.NoteValue("formation", formation);
        formation = std::move(value);
    };
    mutate("circle"); // value formation circle
    mutate("line"); // scoped formation alias uses the same value journal, never JournalValueReplaced.
    CHECK_EQ(formation, "line");
    CHECK_EQ(journal.Values().size(), 1u);
    formation = *journal.RestoreValue("formation");
    CHECK_EQ(formation, "arrow");
    CHECK(journal.Empty());
}

LEEROY_TEST(Training_OrdinaryCleanupCannotCompleteAfterPartialFinalReset)
{
    CleanupReceipts receipts("incarnation_a");
    auto handle = Open(receipts);
    receipts.Seal(handle);
    CHECK(receipts.Restored(handle, true));
    Leeroy::Guild::RaidBodyLease driver{501, 0, 601};
    CHECK(!driver.Matches(502, 0, 601));
    CHECK(!driver.Matches(501, 0, 602));
    Leeroy::Guild::RaidFinalReset reset;
    bool masterReleased = true;
    CHECK(!reset.TryRun([] { throw std::runtime_error("partial stock reset"); }));
    CHECK(masterReleased);
    CHECK(!receipts.Complete(handle, true, reset.Complete()));
    CHECK(reset.TryRun([] { })); // Independent receipt retries even with no master.
    CHECK(receipts.Complete(handle, true, reset.Complete()));
}

LEEROY_TEST(Training_OrdinaryProofReturnRetriesAfterTheTerminalDeadline)
{
    using Leeroy::Guild::RaidReturnReady;
    CHECK(!RaidReturnReady(true, true, 59999, 60000));
    CHECK(RaidReturnReady(true, true, 60000, 60000));
    CHECK(!RaidReturnReady(true, true, 64999, 65000));
    CHECK(RaidReturnReady(true, true, 65000, 65000));
    CHECK(!RaidReturnReady(true, false, 60001, 65000)); // Refused attempts also respect the retry interval.
    CHECK(RaidReturnReady(true, false, 65000, 65000));
    CHECK(RaidReturnReady(true, false, 0, 0)); // First attempt.
    CHECK(!RaidReturnReady(false, true, 65000, 65000));
}

LEEROY_TEST(Training_CleanupProcessIdentityAndEvictionNeverInventProof)
{
    CleanupReceipts first("incarnation_a", 1), restarted("incarnation_b");
    auto old = Open(first), newerProcess = Open(restarted);
    CHECK(old != newerProcess);
    CHECK(!restarted.Find(old));
    CHECK(!restarted.Admits(old, {101}));
    CHECK(Open(first, 2).empty()); // Never evict unresolved ownership to admit another job.
    first.Seal(old);
    CHECK(first.Restored(old, true));
    CHECK(first.Complete(old, true, true));
    auto newerJob = Open(first, 2);
    CHECK(!newerJob.empty());
    CHECK(!first.Find(old));
    CHECK(!first.Admits(old, {101}));
    CHECK(!first.Complete(old, true, true));
    CHECK(Open(first, 1).empty());
    CleanupReceipts missingNonce("");
    CHECK(Open(missingNonce).empty());
}

LEEROY_TEST(Training_CleanupSchemaCarriesImmutableLosslessNativeIdentity)
{
    CleanupReceipts receipts("incarnation_a");
    auto token = std::numeric_limits<std::uint64_t>::max();
    auto scope = Scope();
    scope.group = token - 1;
    auto handle = receipts.Register("acknowledged_job", token, 101, scope, {101, 102});
    auto event = MakeTrainingCleanup("query_1", handle, receipts.Find(handle));
    CHECK_EQ(event.at("type").get<std::string>(), "training_cleanup");
    CHECK_EQ(event.at("receipt_version").get<int>(), 1);
    CHECK_EQ(event.at("id").get<std::string>(), "query_1");
    CHECK_EQ(event.at("state").get<std::string>(), "open");
    CHECK_EQ(event.at("job_id").get<std::string>(), "acknowledged_job");
    CHECK_EQ(event.at("training_token").get<std::string>(), std::to_string(token));
    CHECK_EQ(event.at("group_id").get<std::string>(), std::to_string(token - 1));
    CHECK_EQ(event.at("instance_id").get<unsigned>(), 77);
    CHECK_EQ(event.at("members").size(), 2);
    receipts.Seal(handle);
    CHECK_EQ(MakeTrainingCleanup("query_2", handle, receipts.Find(handle)).at("state").get<std::string>(), "restoring");
    CHECK(receipts.Restored(handle, true));
    CHECK(receipts.Complete(handle, true, true));
    CHECK_EQ(MakeTrainingCleanup("query_3", handle, receipts.Find(handle)).at("state").get<std::string>(), "complete");
    auto unknown = MakeTrainingCleanup("query_4", "old_handle", nullptr);
    CHECK_EQ(unknown.at("state").get<std::string>(), "unknown");
    CHECK_EQ(unknown.at("training_token").get<std::string>(), "0");
    CHECK_EQ(unknown.at("job_id").get<std::string>(), "");
    CHECK(unknown.at("members").empty());
}

LEEROY_TEST(Training_CleanupFieldsAreOptionalForLegacyAndStrictWhenPresent)
{
    std::string error;
    json bot = {{"guids", {101u}}, {"action", "value"}, {"param", "formation circle"}};
    auto parsed = Leeroy::Guild::ParseBotDo(bot, error);
    CHECK(parsed && parsed->trainingRun.empty());
    bot["training_run"] = "tr_native_7";
    parsed = Leeroy::Guild::ParseBotDo(bot, error);
    CHECK(parsed && parsed->trainingRun == "tr_native_7");
    for (json invalid : {json(1), json("bad:handle"), json(std::string(129, 'a'))})
    {
        bot["training_run"] = invalid;
        CHECK(!Leeroy::Guild::ParseBotDo(bot, error));
        CHECK_EQ(error, "bad field: training_run");
    }
    json dc = {{"leader_guid", 101u}, {"op", "set"}, {"key", "WaitAtBoss"}, {"centi", 100},
               {"training_run", "tr_native_7"}};
    CHECK(ParseDcSetting(dc, error)->trainingRun == "tr_native_7");
    json raid = {{"member_guid", 101u}, {"leader_guid", 101u}, {"action", "dc_pause"}, {"param", "resume"},
                 {"training_run", "tr_native_7"}};
    CHECK(Leeroy::Guild::ParseRaidControl(raid, error)->trainingRun == "tr_native_7");
    CHECK(ParseGetTrainingCleanup({{"training_run", "tr_native_7"}}, error));
    CHECK(!ParseGetTrainingCleanup({{"training_run", ""}}, error));
    CHECK(!ParseGetTrainingCleanup(json::object(), error));
    Leeroy::Guild::JobEvent job;
    CHECK_EQ(Leeroy::Guild::MakeJobEvent(job).at("training_run").get<std::string>(), "");
    job.trainingRun = "tr_native_7";
    CHECK_EQ(Leeroy::Guild::MakeJobEvent(job).at("training_run").get<std::string>(), "tr_native_7");
}
