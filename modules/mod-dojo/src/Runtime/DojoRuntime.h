// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_RUNTIME_H
#define MOD_DOJO_DOJO_RUNTIME_H

#include "DojoManifest.h"
#include "DojoSettings.h"

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

// The runtime's start-time state and the checks that decide whether a training worker may run a scenario. Every
// function here runs on the world thread.
namespace Dojo
{
struct ServerCheck
{
    bool ok = false;
    std::string check;
    std::string detail;
};

struct WorkerState
{
    Settings settings;
    RuntimePlan plan;
    std::optional<Manifest> manifest;
    std::vector<ServerCheck> checks;   // training rules, then the scenario against the installed data
    bool scenarioReady = false;        // armed, rules normal, manifest loaded with its pinned hash and every id valid
};

WorkerState& State();

// The effective settings a training worker must run with (D02, D11): ordinary WotLK rates, no bot cheats, no
// random-bot population. Read from the running server, not the .conf.
std::vector<ServerCheck> CheckTrainingRules(std::uint32_t prepSeconds);

// Reads the manifest file, which must hash to sha256 (Dojo.Scenario.Sha256). Empty error on success.
std::optional<Manifest> LoadManifestFile(std::string const& path, std::string const& sha256, std::string& error);

// Every referenced id against the loaded stores; a failed lookup is a failed check, never a substitute.
std::vector<ServerCheck> ValidateScenario(Manifest const& manifest);

std::string FormatChecks(std::vector<ServerCheck> const& checks);
}

#endif
