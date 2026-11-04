// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_SETTINGS_H
#define MOD_DOJO_DOJO_SETTINGS_H

#include <cstddef>
#include <string>
#include <string_view>

// What a worldserver does with its Dojo.* keys, decided once at start. No AzerothCore includes: the runtime reads the
// keys and logs StatusLine, and tests/TestSettings.cpp covers every outcome. Off and refused start nothing and control
// no bot. Armed means a training build accepted its training configuration; the endpoint and bot control arrive with
// the step driver and the controller hook, so in this build armed starts nothing either.
namespace Dojo
{
inline constexpr std::string_view kTrainingMarker = "leeroy-dojo-training";   // scripts/dojo/preflight.ps1's marker
inline constexpr std::size_t kMaxInstanceBytes = 64;

struct Settings
{
    bool enable = false;
    std::string marker;
    std::string instance;

    bool operator==(Settings const&) const = default;
};

enum class RuntimeState
{
    Off,        // Dojo.Enable = 0
    Refused,    // Dojo.Enable = 1 that this build or configuration cannot honour
    Armed,      // a training build with a training configuration
};

struct RuntimePlan
{
    RuntimeState state = RuntimeState::Off;
    std::string reason;   // why, for the start log; never echoes a key's value
};

// 1-64 of A-Z a-z 0-9 . _ : -, the preflight's instance pattern.
bool IsInstanceName(std::string_view name);

RuntimePlan PlanRuntime(Settings const& settings, bool trainingBuild);

std::string_view StateName(RuntimeState state);

// One line for the start log, e.g. "mod-dojo: off (Dojo.Enable = 0), disabled build; no endpoint, no bot control".
std::string StatusLine(RuntimePlan const& plan, bool trainingBuild);
}

#endif
