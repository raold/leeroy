// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingCommands.h"
#include "TrainingTap.h"
#include "DummyTrialJob.h"
#include "LeeroyModule.h"
#include "LeeroyTrainingProtocol.h"
#include "UpdateTime.h"

namespace Leeroy::Training
{
namespace { bool cleanupPending = false; }
bool TrainingCleanupPending() { return cleanupPending; }
void RequireTrainingCleanup() { cleanupPending = true; }
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
    cleanupPending = true;
    EndDummyTrial("training_off");
    bool const runs = ClearTrainingRuns();
    bool const bots = RestoreTrainingBots();
    cleanupPending = !runs || !bots || DummyTrialRunning();
}
}
