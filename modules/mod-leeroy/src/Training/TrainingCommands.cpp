// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingCommands.h"
#include "TrainingTap.h"
#include "DummyTrialJob.h"
#include "LeeroyModule.h"
#include "LeeroyTrainingFence.h"
#include "LeeroyTrainingProtocol.h"
#include "UpdateTime.h"
#include "OfflineRaidJob.h"
#include "Log.h"

#include <chrono>

namespace Leeroy::Training
{
namespace
{
TrainingFence fence; // World thread only.
CleanupReport report;
std::uint64_t SteadyMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
}
bool TrainingCleanupPending() { return fence.Fencing(SteadyMs()); }
bool TrainingCleanupUnresolved() { return fence.Pending(); }
void RequireTrainingCleanup() { fence.Raise(SteadyMs()); }
LeeroyCommandResult HandleTelemetry(nlohmann::json const& message)
{
    std::string error;
    auto command = ParseTelemetry(message, error);
    if (!command) return {false, error};
    if (!LeeroyModule::Instance().Enabled()) return {false, "guild_off"};
    auto& tap = TrainingTap::Instance();
    tap.SetMindArmed(command->record);
    tap.SetWatch(command->watch);
    return tap.TelemetryOn() ? LeeroyCommandResult{true, {}} : LeeroyCommandResult{false, "telemetry_off"};
}

LeeroyCommandResult HandleGetStats(nlohmann::json const& message)
{
    std::string error;
    if (!ParseGetStats(message, error)) return {false, error};
    if (!LeeroyModule::Instance().Enabled()) return {false, "guild_off"};
    auto& tap = TrainingTap::Instance();
    LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(), {{"type", "server_stats"},
        {"id", message.at("id")}, {"mean_ms", sWorldUpdateTime.GetAverageUpdateTime()},
        {"last_ms", sWorldUpdateTime.GetLastUpdateTime()}, {"telemetry", tap.TelemetryOn() && tap.MindArmed()},
        {"armed", tap.ArmedCount()}}), false);
    return {true, {}};
}

void TrainingOffCleanup()
{
    // Each adapter retains failures for its next bounded tick. No blanket DC clear or default reset of unowned values.
    auto const now = SteadyMs();
    std::string reason(TrainingBlockReason()); // Read before this pass raises the fence itself.
    if (reason.empty()) reason = "training_off";
    fence.Raise(now);
    EndDummyTrial(reason);
    bool const benchmark = Guild::BenchmarkRunning();
    Guild::EndBenchmark(reason);
    bool const offline = Guild::RestoreOfflineTraining();
    bool const runs = ClearTrainingRuns();
    bool const bots = RestoreTrainingBots();
    std::string adapters;
    auto add = [&adapters](std::string_view name, std::string const& detail = {})
    {
        adapters += adapters.empty() ? "" : ",";
        adapters += name;
        if (!detail.empty()) adapters += "(" + detail + ")";
    };
    if (DummyTrialRunning()) add("dummy_trial", DummyTrialCleanupDetail());
    if (Guild::BenchmarkRunning()) add("benchmark");
    if (!offline) add("offline_raid");
    if (Guild::OfflineTrainingCleanupPending()) add("offline_receipt");
    if (!runs) add("dc_runs");
    if (!bots) add("journals", std::to_string(TrainingJournalCount()));
    fence.Settle(!adapters.empty(), now);
    if (report.Due(adapters, now))
    {
        if (adapters.empty()) LOG_INFO("module.leeroy", "mod-leeroy: training cleanup verified");
        else LOG_WARN("module.leeroy", "mod-leeroy: training cleanup pending: {} after_ms={}", adapters,
            fence.Elapsed(now));
    }
    if (fence.TakeRelease(now))
        LOG_WARN("module.leeroy", "mod-leeroy: training cleanup fence released after_ms={} pending={}; only bodies "
            "still owned or journaled stay fenced, and training stays refused until cleanup is verified",
            fence.Elapsed(now), adapters);
    if (benchmark)
        LOG_INFO("module.leeroy", "mod-leeroy: training cleanup benchmark retained={}", Guild::BenchmarkRunning());
}
}
