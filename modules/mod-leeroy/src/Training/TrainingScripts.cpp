// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingCommands.h"
#include "TrainingTap.h"
#include "DummyTrialJob.h"
#include "GuildPorts.h"
#include "Log.h"
#include "Player.h"
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
        LeeroyRegisterCommand("get_vocab", Leeroy::Training::HandleGetVocab);
        LeeroyRegisterCommand("custom_strategy", Leeroy::Training::HandleCustomStrategy);
        LeeroyRegisterCommand("dc_setting", Leeroy::Training::HandleDcSetting);
        LeeroyRegisterCommand("dummy_trial", Leeroy::Training::HandleDummyTrial);
        auto& tap = Leeroy::Training::TrainingTap::Instance();
        tap.Start();
        LOG_INFO("module.leeroy", "mod-leeroy: training ground loaded (telemetry {}, training {})",
            tap.TelemetryOn() ? "on" : "off", tap.TrainingOn() ? "on" : "off");
    }

    void OnUpdate(uint32 diff) override
    {
        auto& tap = Leeroy::Training::TrainingTap::Instance();
        Leeroy::Training::TickDummyTrial(diff); // Snapshot/close trial counters before an off-transition recycles a slot.
        tap.Tick(diff);
        if (tap.TakeTrainingTurnedOff()) Leeroy::Training::TrainingOffCleanup();
        Leeroy::Training::TickVocab();
        Leeroy::Training::TickDcHolds(diff);
        if (diff >= _cleanupRemaining)
        {
            _cleanupRemaining = 1000;
            if (!Leeroy::Training::TrainingAllowed()) Leeroy::Training::TrainingOffCleanup();
        }
        else _cleanupRemaining -= diff;
    }

    void OnShutdown() override
    {
        Leeroy::Training::ShutdownDummyTrial();
        Leeroy::Training::TrainingOffCleanup();
        Leeroy::Training::ResetTrainingVocabulary();
        Leeroy::Training::TrainingTap::Instance().Stop();
    }
private:
    uint32 _cleanupRemaining = 0;
};

class LeeroyTrainingPlayerScript : public PlayerScript
{
public:
    LeeroyTrainingPlayerScript() : PlayerScript("LeeroyTrainingPlayerScript",
        { PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_LOGOUT }) { }
    void OnPlayerLogin(Player* player) override
    {
        if (player->GetGUID().GetCounter() == Leeroy::Guild::HumanGuid())
            Leeroy::Training::EndDummyTrial("human_login");
    }
    void OnPlayerLogout(Player* player) override
    {
        Leeroy::Training::DummyPlayerLogout(player->GetGUID().GetCounter());
        Leeroy::Training::JournalForget(player->GetGUID().GetCounter());
    }
};

void AddSC_leeroy_training()
{
    new LeeroyTrainingWorldScript();
    new LeeroyTrainingPlayerScript();
    AddSC_leeroy_training_hooks();
}
