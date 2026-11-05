// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoEndpoint.h"
#include "DojoExecutor.h"
#include "DojoObservation.h"
#include "DojoRuntime.h"
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
    settings.scenarioManifest = sConfigMgr->GetOption<std::string>("Dojo.Scenario.Manifest", "");
    settings.scenarioSha256 = sConfigMgr->GetOption<std::string>("Dojo.Scenario.Sha256", "");
    return settings;
}

#if DOJO_TRAINING_BUILD
// Armed: the training rules must hold in the running server, then the pinned manifest must load and every id in it
// must resolve. Any failure refuses the scenario; the worker stays up but no fixture moves.
void PrepareScenario(Dojo::WorkerState& state)
{
    if (state.settings.scenarioManifest.empty())
    {
        LOG_INFO("module.dojo", "mod-dojo: no scenario configured (Dojo.Scenario.Manifest is empty)");
        return;
    }
    std::string error;
    state.manifest = Dojo::LoadManifestFile(state.settings.scenarioManifest, state.settings.scenarioSha256, error);
    if (!state.manifest)
    {
        state.checks.push_back({false, "scenario manifest", error});
        LOG_ERROR("module.dojo", "mod-dojo: scenario refused: {}", error);
        return;
    }
    state.checks = Dojo::CheckTrainingRules(state.manifest->rules.prepSeconds);
    std::vector<Dojo::ServerCheck> const scenario = Dojo::ValidateScenario(*state.manifest);
    state.checks.insert(state.checks.end(), scenario.begin(), scenario.end());
    std::string const catalog = Dojo::Control::Instance().Prepare(*state.manifest);
    state.checks.push_back({catalog.empty(), "action catalog", catalog.empty() ? "built from the kit" : catalog});
    if (catalog.empty())
    {
        std::string const observation = Dojo::Observer::Instance().Prepare(*state.manifest, Dojo::Control::Instance().Catalog());
        state.checks.push_back({observation.empty(), "observation schema",
            observation.empty() ? "built from the catalog and the talent data" : observation});
    }
    std::size_t failed = 0;
    for (Dojo::ServerCheck const& c : state.checks)
        if (!c.ok)
        {
            ++failed;
            LOG_ERROR("module.dojo", "mod-dojo: FAIL {}: {}", c.check, c.detail);
        }
    state.scenarioReady = failed == 0;
    LOG_INFO("module.dojo", "mod-dojo: scenario {} sha256 {}: {} checks, {} failed{}", state.manifest->id,
        state.manifest->sha256, state.checks.size(), failed, state.scenarioReady ? "; ready" : "; refused");
    if (!state.scenarioReady)
        return;
    // The endpoint opens on the next world tick, on the world thread that polls it.
    Dojo::EndpointConfig endpoint;
    endpoint.instance = state.settings.instance;
    endpoint.scenario = state.manifest->id;
    endpoint.scenarioSha256 = state.manifest->sha256;
    for (Dojo::ActorSpec const& a : state.manifest->actors)
        endpoint.teams.push_back(a.team == "horde" ? 1 : 0);
    endpoint.matchMs = state.manifest->rules.matchSeconds * 1000;
    Dojo::Endpoint::Instance().Configure(std::move(endpoint));
}
#endif
}

void AddSC_dojo_commands();

// The mode is fixed at start: a reload is reported, never applied. Nothing here opens a socket or starts a thread; in a
// training build that is armed with a ready scenario the endpoint (DojoEndpoint.h) opens on the first world tick, and
// it and the dojo console commands move the scenario's fixture actors.
class DojoWorldScript : public WorldScript
{
public:
    DojoWorldScript() : WorldScript("DojoWorldScript", { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_STARTUP })
    {
    }

    void OnAfterConfigLoad(bool reload) override
    {
        Dojo::Settings const settings = ReadSettings();
        Dojo::WorkerState& state = Dojo::State();
        if (!reload)
        {
            state.settings = settings;
            state.plan = Dojo::PlanRuntime(settings, kTrainingBuild);
            return;
        }
        if (settings != state.settings)
            LOG_WARN("module.dojo", "mod-dojo: Dojo.* changed in a config reload; the dojo stays {} until the "
                "worldserver restarts", Dojo::StateName(state.plan.state));
    }

    void OnStartup() override
    {
        Dojo::WorkerState& state = Dojo::State();
        LOG_INFO("module.dojo", "{}", DOJO_VARIANT_TAG);
        std::string const line = Dojo::StatusLine(state.plan, kTrainingBuild);
        if (state.plan.state == Dojo::RuntimeState::Refused)
            LOG_ERROR("module.dojo", "{}", line);
        else
            LOG_INFO("module.dojo", "{}", line);
#if DOJO_TRAINING_BUILD
        if (state.plan.state == Dojo::RuntimeState::Armed)
            PrepareScenario(state);
#endif
    }
};

void AddSC_dojo_world()
{
    new DojoWorldScript();
    AddSC_dojo_commands();
}
