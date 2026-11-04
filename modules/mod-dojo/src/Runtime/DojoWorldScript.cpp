// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoSettings.h"

#include "Config.h"
#include "Log.h"
#include "ScriptMgr.h"

#include <string>

// mod-dojo.cmake sets this on every file in src/Runtime from AC_ENABLE_DOJO.
#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#elif DOJO_TRAINING_BUILD != 0 && DOJO_TRAINING_BUILD != 1
#error "DOJO_TRAINING_BUILD must be 0 or 1"
#endif

// Exactly one of these is in a linked worldserver; scripts/dojo/build.ps1 looks for it.
#if DOJO_TRAINING_BUILD
#define DOJO_VARIANT_TAG "mod-dojo build variant: training (AC_ENABLE_DOJO=ON)"
#else
#define DOJO_VARIANT_TAG "mod-dojo build variant: disabled (AC_ENABLE_DOJO=OFF)"
#endif

namespace
{
constexpr bool kTrainingBuild = DOJO_TRAINING_BUILD != 0;

Dojo::Settings ReadSettings()
{
    Dojo::Settings settings;
    settings.enable = sConfigMgr->GetOption<bool>("Dojo.Enable", false);
    settings.marker = sConfigMgr->GetOption<std::string>("Dojo.Marker", "");
    settings.instance = sConfigMgr->GetOption<std::string>("Dojo.Instance", "");
    return settings;
}
}

// Configuration and status only. The mode is fixed at start: a reload is reported, never applied. Nothing here opens
// a socket, starts a thread or touches a bot, whatever the state.
class DojoWorldScript : public WorldScript
{
public:
    DojoWorldScript() : WorldScript("DojoWorldScript", { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_STARTUP })
    {
    }

    void OnAfterConfigLoad(bool reload) override
    {
        Dojo::Settings const settings = ReadSettings();
        if (!reload)
        {
            _settings = settings;
            _plan = Dojo::PlanRuntime(settings, kTrainingBuild);
            return;
        }
        if (settings != _settings)
            LOG_WARN("module.dojo", "mod-dojo: Dojo.* changed in a config reload; the dojo stays {} until the "
                "worldserver restarts", Dojo::StateName(_plan.state));
    }

    void OnStartup() override
    {
        LOG_INFO("module.dojo", "{}", DOJO_VARIANT_TAG);
        std::string const line = Dojo::StatusLine(_plan, kTrainingBuild);
        if (_plan.state == Dojo::RuntimeState::Refused)
            LOG_ERROR("module.dojo", "{}", line);
        else
            LOG_INFO("module.dojo", "{}", line);
    }

private:
    Dojo::Settings _settings;
    Dojo::RuntimePlan _plan;
};

void AddSC_dojo_world()
{
    new DojoWorldScript();
}
