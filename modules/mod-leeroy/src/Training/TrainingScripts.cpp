// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingCommands.h"
#include "TrainingTap.h"
#include "Log.h"
#include "ScriptMgr.h"

void AddSC_leeroy_training_hooks();

class LeeroyTrainingWorldScript : public WorldScript
{
public:
    LeeroyTrainingWorldScript() : WorldScript("LeeroyTrainingWorldScript",
        { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_STARTUP, WORLDHOOK_ON_UPDATE, WORLDHOOK_ON_SHUTDOWN }) { }

    void OnAfterConfigLoad(bool /*reload*/) override { Leeroy::Training::TrainingTap::Instance().LoadConfig(); }

    void OnStartup() override
    {
        LeeroyRegisterCommand("telemetry", Leeroy::Training::HandleTelemetry);
        LeeroyRegisterCommand("get_stats", Leeroy::Training::HandleGetStats);
        auto& tap = Leeroy::Training::TrainingTap::Instance();
        tap.Start();
        LOG_INFO("module.leeroy", "mod-leeroy: training ground loaded (telemetry {}, training {})",
            tap.TelemetryOn() ? "on" : "off", tap.TrainingOn() ? "on" : "off");
    }

    void OnUpdate(uint32 diff) override
    {
        auto& tap = Leeroy::Training::TrainingTap::Instance();
        tap.Tick(diff);
        if (tap.TakeTrainingTurnedOff()) Leeroy::Training::TrainingOffCleanup();
    }

    void OnShutdown() override { Leeroy::Training::TrainingTap::Instance().Stop(); }
};

void AddSC_leeroy_training()
{
    new LeeroyTrainingWorldScript();
    AddSC_leeroy_training_hooks();
}
